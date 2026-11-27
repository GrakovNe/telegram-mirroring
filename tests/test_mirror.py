#!/usr/bin/env python3
"""Tests for the mirror's local logic: Markdown round-trip, content fingerprints,
re-saving an edited post, the edit-watch backoff, and server-side rendering.

Nothing here touches the network — Telegram fetches and media downloads are
replaced with canned data. Run from the repo root:

    python3 -m unittest discover -s tests
"""
import json
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import render
import recheck
import scrape
import store
import sync

REPO_ROOT = Path(__file__).resolve().parent.parent


class TempStoreMixin(unittest.TestCase):
    """Redirects the on-disk store into a throwaway directory."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tgmirror-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._orig = (store.DATA, store.POSTS, store.STATE_FILE,
                      store.META_FILE, recheck.SCHEDULE_FILE)
        store.DATA = self.tmp
        store.POSTS = self.tmp / "posts"
        store.STATE_FILE = self.tmp / "state.json"
        store.META_FILE = self.tmp / "meta.json"
        recheck.SCHEDULE_FILE = self.tmp / "recheck.json"
        self.addCleanup(self._restore)

    def _restore(self):
        (store.DATA, store.POSTS, store.STATE_FILE,
         store.META_FILE, recheck.SCHEDULE_FILE) = self._orig

    def make_post(self, pid=101, text="<p>orig</p>", dt="2026-11-27T10:00:00+00:00",
                  media=None):
        return {
            "id": pid,
            "url": f"https://t.me/{scrape.CHANNEL}/{pid}",
            "datetime": dt,
            "text_html": text,
            "media": [{"type": "photo", "url": "https://x/a.jpg"}]
                     if media is None else media,
            "preview": None,
        }


class MarkdownRoundTrip(TempStoreMixin):
    def test_spoiler(self):
        md = store.html_to_md("Hello <tg-spoiler>secret</tg-spoiler> world")
        self.assertEqual(md, "Hello ||secret|| world")
        self.assertIn("<tg-spoiler>secret</tg-spoiler>", store.md_to_html(md))

    def test_literal_pipes_stay_literal(self):
        self.assertEqual(store.md_to_html("a || b"), "<p>a || b</p>")

    def test_inline_styles(self):
        md = store.html_to_md("a <b>b</b> <i>i</i> <s>s</s> <code>c</code>")
        self.assertEqual(md, "a **b** _i_ ~~s~~ `c`")
        out = store.md_to_html(md)
        for tag in ("<b>b</b>", "<i>i</i>", "<s>s</s>", "<code>c</code>"):
            self.assertIn(tag, out)

    def test_line_breaks(self):
        md = store.html_to_md("line1<br>line2")
        self.assertEqual(md, "line1\nline2")
        self.assertEqual(store.md_to_html(md), "<p>line1<br>line2</p>")

    def test_code_block_survives_round_trip(self):
        src = "def f():\n    return 1"
        out = store.md_to_html(
            store.html_to_md("<pre>" + src.replace("\n", "<br>") + "</pre>"))
        self.assertEqual(out, "<pre>" + src + "</pre>")

    def test_blockquote(self):
        md = store.html_to_md("q: <blockquote>quoted <b>text</b></blockquote> tail")
        self.assertIn("> quoted **text**", md)
        out = store.md_to_html(md)
        self.assertIn("<blockquote>", out)
        self.assertIn("<b>text</b>", out)

    def test_link_with_parens_and_brackets(self):
        md = store.html_to_md('<a href="https://ex.com/a_(b)">link [x]</a>')
        self.assertEqual(md, "[link \\[x\\]](https://ex.com/a_%28b%29)")
        out = store.md_to_html(md)
        self.assertIn('href="https://ex.com/a_%28b%29"', out)
        self.assertIn(">link [x]</a>", out)

    def test_underscores_in_url_are_not_italics(self):
        out = store.md_to_html("see [doc](https://ex.com/_x_/y) ok")
        self.assertIn("/_x_/y", out)
        self.assertNotIn("<i>x</i>", out)

    def test_emoji_and_spans(self):
        self.assertEqual(store.html_to_md('x <i class="emoji"> <b>👍</b> </i> y'),
                         "x 👍 y")
        self.assertEqual(store.html_to_md('<span class="x">plain</span>'), "plain")


class Fingerprints(TempStoreMixin):
    def setUp(self):
        super().setUp()
        self.post = self.make_post(
            text="<p>hi</p>", media=[{"type": "photo", "url": "https://x/1.jpg"}])

    def test_stable_and_content_sensitive(self):
        self.assertEqual(store.content_hash(self.post),
                         store.content_hash(json.loads(json.dumps(self.post))))
        changed = json.loads(json.dumps(self.post))
        changed["text_html"] = "<p>hi!</p>"
        self.assertNotEqual(store.content_hash(self.post), store.content_hash(changed))

    def test_ignores_url_and_datetime(self):
        other = json.loads(json.dumps(self.post))
        other["url"] = "https://t.me/x/9"
        other["datetime"] = "2020-01-01T00:00:00+00:00"
        self.assertEqual(store.content_hash(self.post), store.content_hash(other))

    def test_media_hash_tracks_remote_urls_only(self):
        other = json.loads(json.dumps(self.post))
        other["text_html"] = "<p>edited</p>"
        self.assertEqual(store.media_hash(self.post), store.media_hash(other))
        swapped = json.loads(json.dumps(self.post))
        swapped["media"][0]["url"] = "https://x/2.jpg"
        self.assertNotEqual(store.media_hash(self.post), store.media_hash(swapped))


class SavingEditedPosts(TempStoreMixin):
    def setUp(self):
        super().setUp()
        self.downloaded = []
        self._orig_download = store.download
        self.addCleanup(setattr, store, "download", self._orig_download)

        def fake_download(url, dest, referer=None, **kw):
            self.downloaded.append(url)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"xx")
            return True

        store.download = fake_download

    def save(self, post, **kw):
        # media_delay would otherwise make the suite sleep between downloads
        return store.save_post(post, media_delay=(0, 0), **kw)


    def test_first_save_downloads_media(self):
        rec = self.save(self.make_post())
        self.assertEqual(self.downloaded, ["https://x/a.jpg"])
        self.assertTrue(
            rec["media"][0]["url"].startswith("/media/2026-11-27__101/media/"))

    def test_text_edit_reuses_media_and_records_edited_at(self):
        self.save(self.make_post())
        edited = self.make_post(text="<p>fixed</p>")
        self.downloaded.clear()

        rec = self.save(edited, edited_at="2026-11-27T12:00:00+00:00")

        self.assertEqual(self.downloaded, [],
                         "unchanged media must not be re-downloaded")
        self.assertIn("fixed", rec["text_html"])
        self.assertNotIn("orig", rec["text_html"])
        self.assertEqual(rec["edited_at"], "2026-11-27T12:00:00+00:00")

        reloaded = store.parse_post_md(store._post_md_path(101))
        self.assertEqual(reloaded["edited_at"], "2026-11-27T12:00:00+00:00")
        fm = store._read_frontmatter(store._post_md_path(101))
        self.assertEqual(fm["content_hash"], store.content_hash(edited))
        self.assertEqual(fm["media_hash"], store.media_hash(edited))

    def test_media_edit_redownloads_into_the_same_folder(self):
        self.save(self.make_post())
        self.downloaded.clear()
        self.save(self.make_post(media=[{"type": "photo", "url": "https://x/b.jpg"}]))
        self.assertEqual(self.downloaded, ["https://x/b.jpg"])
        files = sorted(p.name for p in (store.POSTS / "2026-11-27__101" / "media").iterdir())
        self.assertEqual(files, ["01.jpg"])

    def test_failed_media_is_retried_on_the_next_save(self):
        """A post whose download once failed must not be frozen in that state:
        media reuse is only allowed for a set that is fully on disk."""
        def failing_download(url, dest, referer=None, **kw):
            self.downloaded.append(url)
            return False

        def succeeding_download(url, dest, referer=None, **kw):
            self.downloaded.append(url)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"xx")
            return True

        store.download = failing_download
        self.save(self.make_post())
        self.assertEqual(self.downloaded, ["https://x/a.jpg"])

        # text edit, media still broken -> media must be attempted again
        self.downloaded.clear()
        store.download = succeeding_download
        self.save(self.make_post(text="<p>fixed</p>"),
                  edited_at="2026-11-27T12:00:00+00:00")
        self.assertEqual(self.downloaded, ["https://x/a.jpg"])

        # now that it succeeded, a later text edit must reuse it
        self.downloaded.clear()
        self.save(self.make_post(text="<p>fixed again</p>"))
        self.assertEqual(self.downloaded, [])

    def test_stored_content_hash(self):
        post = self.make_post()
        self.save(post)
        self.assertEqual(store.stored_content_hash(101), store.content_hash(post))
        self.assertIsNone(store.stored_content_hash(999))


class BackoffMath(TempStoreMixin):
    def test_intervals_and_elapsed_time(self):
        first, factor = recheck.RECHECK_FIRST_INTERVAL, recheck.RECHECK_FACTOR
        self.assertAlmostEqual(recheck.interval_at(3), first * factor ** 3)
        self.assertAlmostEqual(recheck.elapsed_after(3),
                               first * (factor ** 3 - 1) / (factor - 1))

    def test_checks_by_follows_the_schedule(self):
        pub = 1_000_000.0
        self.assertEqual(recheck.checks_by(pub, pub), 0)
        for n in range(6):
            self.assertEqual(recheck.checks_by(pub + recheck.elapsed_after(n), pub), n)
            self.assertEqual(
                recheck.checks_by(pub + recheck.elapsed_after(n + 1) - 1, pub), n)

    def test_deadlines_move_further_away(self):
        self.assertGreater(recheck.next_due(1_000_000.0, 1),
                           recheck.next_due(1_000_000.0, 0))

    def test_clustering(self):
        self.assertEqual(recheck.cluster(["10", "11", "12", "40"]),
                         [[10, 11, 12], [40]])


class EditWatchCycle(TempStoreMixin):
    def setUp(self):
        super().setUp()
        self.st = store.Store()
        self.now = time.time()
        self.iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                 time.gmtime(self.now - 3600))
        self.st.add(store.save_post(
            self.make_post(500, text="watched", dt=self.iso, media=[])))

    def page(self, text):
        return (f'<div>js-widget_message_wrap<div data-post="{scrape.CHANNEL}/500">'
                f'<time datetime="{self.iso}"></time>'
                f'<div class="tgme_widget_message_text">{text}</div></div></div>')

    def start_watching(self):
        schedule = recheck.load_schedule()
        recheck.watch(self.st, schedule, self.now)
        recheck.save_schedule(schedule)

    def force_due(self):
        schedule = recheck.load_schedule()
        schedule["500"]["due"] = self.now - 1
        recheck.save_schedule(schedule)

    def test_watch_is_idempotent_and_starts_in_the_future(self):
        schedule = recheck.load_schedule()
        self.assertGreaterEqual(recheck.watch(self.st, schedule, self.now), 1)
        self.assertIn("500", schedule)
        self.assertGreater(schedule["500"]["due"], self.now)
        self.assertEqual(recheck.watch(self.st, schedule, self.now), 0)

    def test_unchanged_post_grows_its_backoff(self):
        self.start_watching()
        self.force_due()
        result = recheck.run_cycle(self.st, fetch_page=lambda pid: self.page("watched"),
                                   now=self.now, sleep=lambda: None)
        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["edited"], [])
        self.assertGreater(recheck.load_schedule()["500"]["due"],
                           self.now + recheck.RECHECK_FIRST_INTERVAL)

    def test_edit_is_persisted_in_place(self):
        self.start_watching()
        self.force_due()
        result = recheck.run_cycle(self.st, fetch_page=lambda pid: self.page("EDITED"),
                                   now=self.now, sleep=lambda: None)
        self.assertEqual(result["edited"], [500])
        self.assertIn("EDITED", self.st.get(500)["text_html"])
        self.assertTrue(self.st.get(500)["edited_at"])
        self.assertEqual([p.name for p in store.POSTS.iterdir()], ["2026-11-27__500"])

    def test_vanished_post_is_marked_done(self):
        self.start_watching()
        for i in range(recheck.MAX_MISSES):
            self.force_due()
            recheck.run_cycle(self.st, fetch_page=lambda pid: "<html></html>",
                              now=self.now + i, sleep=lambda: None)
        self.assertTrue(recheck.load_schedule()["500"].get("done"))

    def test_requests_within_a_cycle_are_spaced(self):
        """A cycle that fetches several groups must not hit t.me back to back."""
        for pid in (500, 501, 502, 520):
            self.st.add(store.save_post(
                self.make_post(pid, dt=self.iso, media=[]), media_delay=(0, 0)))
        schedule = recheck.load_schedule()
        for pid in (500, 501, 502, 520):
            schedule[str(pid)] = {"due": self.now - 1, "checks": 0, "misses": 0}
        recheck.save_schedule(schedule)

        pauses = []
        recheck.run_cycle(self.st, fetch_page=lambda pid: self.page("watched"),
                          now=self.now, sleep=lambda: pauses.append(1))
        groups = recheck.cluster([str(i) for i in (500, 501, 502, 520)])
        self.assertGreater(len(groups), 1, "test needs more than one group")
        self.assertEqual(len(pauses), len(groups) - 1)

    def test_post_older_than_the_window_is_dropped(self):
        old = time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                            time.gmtime(self.now - recheck.RECHECK_WINDOW - 100))
        self.st.add(store.save_post(self.make_post(501, dt=old, media=[])))
        schedule = recheck.load_schedule()
        schedule["501"] = {"due": self.now - 5, "checks": 0, "misses": 0}
        recheck.save_schedule(schedule)
        recheck.run_cycle(self.st, fetch_page=lambda pid: "<html></html>",
                          now=self.now, sleep=lambda: None)
        self.assertNotIn("501", recheck.load_schedule())


class BackfillTermination(TempStoreMixin):
    def setUp(self):
        super().setUp()
        self.st = store.Store()
        self.st.add(store.save_post(self.make_post(media=[])))
        self._orig_fetch, self._orig_sleep = sync._fetch, sync._sleep
        self.addCleanup(setattr, sync, "_fetch", self._orig_fetch)
        self.addCleanup(setattr, sync, "_sleep", self._orig_sleep)
        sync._sleep = lambda rng: None

    def test_one_empty_page_does_not_finish_the_backfill(self):
        sync._fetch = lambda before=None: []
        state = {"min_id": 100, "max_id": 200, "backfill_done": False}
        sync.backfill(self.st, state)
        self.assertFalse(state["backfill_done"])

    def test_repeated_empty_pages_finish_it(self):
        sync._fetch = lambda before=None: []
        state = {"min_id": 5, "max_id": 200, "backfill_done": False}
        for _ in range(sync.BACKFILL_EMPTY_PAGES):
            sync.backfill(self.st, state)
        self.assertTrue(state["backfill_done"])

    def test_page_that_reaches_no_further_back_finishes_it(self):
        sync._fetch = lambda before=None: [self.make_post(media=[])]
        state = {"min_id": 100, "max_id": 200, "backfill_done": False}
        sync.backfill(self.st, state)
        self.assertTrue(state["backfill_done"])

    def test_page_from_the_past_keeps_going(self):
        sync._fetch = lambda before=None: [
            self.make_post(90, dt="2026-01-01T00:00:00+00:00", media=[])]
        state = {"min_id": 100, "max_id": 200, "backfill_done": False}
        sync.backfill(self.st, state)
        self.assertFalse(state["backfill_done"])
        self.assertEqual(state["min_id"], 90)


class ServerSideRendering(TempStoreMixin):
    def setUp(self):
        super().setUp()
        self.template = (REPO_ROOT / "index.html").read_text("utf-8")
        self.meta = {"username": "chan", "url": "https://t.me/chan",
                     "title": "Chan", "description": "A channel"}
        self.post = {
            "id": 42, "url": "https://t.me/chan/42",
            "datetime": "2026-11-27T16:33:04+00:00", "edited_at": "",
            "text_html": "<p>Title line</p><p>Body <b>bold</b></p>",
            "media": [{"type": "photo", "url": "/media/2026-11-27__42/media/01.jpg"},
                      {"type": "photo", "url": "/media/2026-11-27__42/media/02.jpg"}],
            "preview": None,
        }

    def head(self, html):
        """Everything up to </head> — where the one-of-each tags must live."""
        return html.split("</head>")[0]

    def test_feed_replaces_every_marker_exactly_once(self):
        html = render.render_feed(self.template, [self.post], 41, self.meta,
                                  "https://m.example")
        head = self.head(html)
        self.assertNotIn("<!--SSR:", html)
        self.assertEqual(head.count("<title>"), 1)
        self.assertEqual(head.count('name="description"'), 1)
        self.assertEqual(head.count('rel="canonical"'), 1)
        self.assertIn('<article class="post" id="post-42"', html)
        self.assertIn("Title line", html)
        self.assertIn("Body <b>bold</b>", html)
        self.assertIn("window.__SSR__", html)

    def test_post_page_is_self_contained(self):
        html = render.render_post(self.template, self.post, self.meta, "https://m.example")
        self.assertNotIn("<!--SSR:", html)
        self.assertEqual(self.head(html).count("<title>"), 1)
        self.assertIn('<link rel="canonical" href="https://m.example/post/42">', html)
        self.assertIn('og:type" content="article"', html)
        self.assertIn('<h1 class="post-title">Title line</h1>', html)
        ld = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>',
                                  html, re.S).group(1))
        self.assertEqual(ld["@type"], "SocialMediaPosting")
        self.assertEqual(ld["datePublished"], "2026-11-27T16:33:04+00:00")
        self.assertEqual(ld["dateModified"], "2026-11-27T16:33:04+00:00")

    def test_card_markup_matches_the_client_contract(self):
        """Hydration depends on these hooks existing in the server markup."""
        card, _, _ = render.post_html(self.post, "https://m.example")
        self.assertIn('id="post-42"', card)
        self.assertIn('data-id="42"', card)
        self.assertIn('data-media=', card)
        self.assertIn('data-carousel="1"', card)
        self.assertIn('class="count-badge"', card)

    def test_edited_marker_appears_only_when_set(self):
        card, _, _ = render.post_html(self.post, "https://m.example")
        self.assertNotIn("post-edited", card)
        card, _, _ = render.post_html(
            dict(self.post, edited_at="2026-11-27T18:00:00+00:00"), "https://m.example")
        self.assertIn('class="post-edited"', card)

    def test_language_follows_accept_language(self):
        ru = render.render_feed(self.template, [self.post], None, self.meta,
                                "https://m.example", "ru-RU,ru;q=0.9")
        self.assertIn('<html lang="ru">', ru)
        en = render.render_feed(self.template, [self.post], None, self.meta,
                                "https://m.example", "en-US,en;q=0.9")
        self.assertIn('<html lang="en">', en)

    def test_sitemap_and_rss(self):
        sitemap = render.render_sitemap([(42, "2026-11-27T16:33:04+00:00")],
                                        "https://m.example")
        self.assertIn("<loc>https://m.example/post/42</loc>", sitemap)
        self.assertIn("<lastmod>2026-11-27</lastmod>", sitemap)
        rss = render.render_rss([self.post], self.meta, "https://m.example")
        self.assertIn("<title>Title line</title>", rss)
        self.assertIn("https://m.example/post/42", rss)

    def test_no_absolute_base_means_no_sitemap_or_rss(self):
        self.assertIsNone(render.render_sitemap([(42, "")], ""))
        self.assertIsNone(render.render_rss([self.post], self.meta, ""))

    def test_host_header_must_look_like_a_host(self):
        self.assertEqual(render.base_url("mirror.example:8080"),
                         "http://mirror.example:8080")
        self.assertEqual(render.base_url("evil.com/x?"), "")

    def test_json_ld_cannot_break_out_of_its_script_element(self):
        """Media URLs are scraped out of CSS url(...) and are not sanitized, so
        they are the realistic way a '<' reaches the JSON-LD blob. json.dumps
        leaves '<' alone, which would close the script element early."""
        hostile = dict(self.post, media=[
            {"type": "photo",
             "url": "https://x/a.jpg</script><img src=x onerror=alert(1)>"}])
        html = render.render_post(self.template, hostile, self.meta, "https://m.example")
        ld = re.search(r'<script type="application/ld\+json">(.*?)</script>',
                       html, re.S).group(1)
        self.assertNotIn("<", ld)
        self.assertNotIn(">", ld)
        # the payload survives as inert data, it is not silently dropped
        self.assertIn("onerror=alert(1)", json.loads(ld)["image"])

    def test_og_site_name_is_the_channel_not_the_page_title(self):
        html = render.render_post(self.template, self.post, self.meta, "https://m.example")
        self.assertIn('og:site_name" content="Chan"', html)
        self.assertIn('og:title" content="Title line · Chan"', html)

    def test_text_is_escaped(self):
        xss = dict(self.post, text_html="<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>")
        html = render.render_post(self.template, xss, self.meta, "https://m.example")
        self.assertNotIn("<script>alert(1)</script>", html)


if __name__ == "__main__":
    unittest.main()
