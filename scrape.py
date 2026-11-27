#!/usr/bin/env python3
"""
scrape.py — fetch and parse the PUBLIC Telegram web preview (t.me/s/<channel>).

This is the only module that talks to Telegram. It returns posts with REMOTE
media URLs; downloading/localizing happens in store.py. No auth, no tokens —
just the same page a logged-out browser would see.
"""
import re
import html as htmllib
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urlsplit

from config import CHANNEL

BASE = f"https://t.me/s/{CHANNEL}"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "a", "br", "code", "pre", "blockquote", "span", "tg-spoiler",
}
# Subtree discarded entirely, not just the tag.
DROP_CONTENT_TAGS = {"script", "style", "iframe", "object", "embed"}
# Kept transparently: the glyph inside is what we want, the wrapper is not.
EMOJI_WRAPPER_TAGS = {"tg-emoji"}
VOID_TAGS = {"br"}
# Href schemes we keep; anything else (javascript:, data:, vbscript:) is dropped.
ALLOWED_SCHEMES = {"http", "https", "mailto", "tg"}


def fetch_html(before=None, timeout=20):
    url = BASE + (f"?before={before}" if before else "")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch_post_html(pid, timeout=20):
    """Fetch the single-post preview page t.me/s/<channel>/<id>.

    Telegram answers with a window of ~15 posts around <id>, so one request can
    re-check many posts at once (see recheck.py)."""
    req = urllib.request.Request(f"{BASE}/{pid}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _safe_href(raw):
    """Return an href worth emitting, or None to unwrap the anchor."""
    if raw is None:
        return None
    # Browsers strip ASCII whitespace and NUL from a URL before resolving its
    # scheme, so "java\nscript:" must not pass as a scheme-less relative link.
    url = re.sub(r"[\x00-\x20\x7f]+", "", raw)
    if not url:
        return None
    scheme = urlsplit(url).scheme.lower()
    if url.startswith("//"):
        return url
    if not scheme:
        return url                      # relative link
    return url if scheme in ALLOWED_SCHEMES else None


class _Frame:
    __slots__ = ("tag", "emit", "dropped")

    def __init__(self, tag, emit, dropped=False):
        self.tag = tag        # tag as it appeared in the source
        self.emit = emit      # tag to emit (None = unwrap / drop)
        self.dropped = dropped


class _Sanitizer(HTMLParser):
    """Allowlist sanitizer that rebuilds the fragment from parsed events.

    Rebuilding from events (rather than regex-rewriting the source) is what makes
    this robust: an attribute can only reach the output if we name it explicitly,
    so unquoted handlers, entity-obfuscated schemes and stray markup cannot
    smuggle themselves through. Allowed tags are emitted bare, except <a href>.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.stack = []

    def _dropping(self):
        return any(f.dropped for f in self.stack)

    def _open(self, tag, attrs, self_closing):
        if tag in DROP_CONTENT_TAGS:
            if not self_closing:
                self.stack.append(_Frame(tag, None, dropped=True))
            return
        if tag in EMOJI_WRAPPER_TAGS:
            if not self_closing:
                self.stack.append(_Frame(tag, None))
            return
        if tag not in ALLOWED_TAGS:
            if not self_closing:
                self.stack.append(_Frame(tag, None))   # unwrap, keep children
            return
        if tag == "br":
            self.out.append("<br>")
            return
        if tag == "a":
            href = _safe_href(dict(attrs).get("href"))
            if href is None:
                self.stack.append(_Frame(tag, None))   # unsafe href -> plain text
                return
            self.out.append(f'<a href="{htmllib.escape(href, quote=True)}">')
        else:
            self.out.append(f"<{tag}>")
        if not self_closing:
            self.stack.append(_Frame(tag, tag))

    def handle_starttag(self, tag, attrs):
        self._open(tag, attrs, self_closing=False)

    def handle_startendtag(self, tag, attrs):
        self._open(tag, attrs, self_closing=True)

    def handle_endtag(self, tag):
        # Close the innermost matching opener; anything left open inside it is
        # closed here too, so the output is always balanced.
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i].tag == tag:
                for frame in reversed(self.stack[i:]):
                    if frame.emit:
                        self.out.append(f"</{frame.emit}>")
                del self.stack[i:]
                return

    def handle_data(self, data):
        if not self._dropping():
            self.out.append(htmllib.escape(data, quote=False))

    def result(self):
        self.close()
        for frame in reversed(self.stack):
            if frame.emit:
                self.out.append(f"</{frame.emit}>")
        self.stack = []
        return "".join(self.out)


def sanitize(text_html):
    """Allowlist sanitizer for a post's text HTML (keeps inline formatting)."""
    if not text_html:
        return ""
    p = _Sanitizer()
    p.feed(text_html)
    return p.result()


# Captures the URL inside `background-image:url(...)`. The URL may contain an
# HTML-encoded '&amp;' (e.g. og:image query params), so capture lazily up to the
# real closing delimiter — a quote (raw or encoded) or ')' — rather than stopping
# at the first '&'. htmllib.unescape() then restores '&amp;' -> '&'.
_BG_URL = r"""background-image:url\((?:&#39;|&#34;|&quot;|['"])?(.+?)(?=&#39;|&#34;|&quot;|['"]|\))"""


def _bg_urls(chunk, cls):
    out = []
    for m in re.finditer(re.escape(cls) + r'[^>]*?' + _BG_URL, chunk):
        out.append(htmllib.unescape(m.group(1)))
    return out


def _parse_media(chunk, pid):
    """Media items in DOM order (photos and videos interleaved as posted)."""
    media = []
    tg_url = f"https://t.me/{CHANNEL}/{pid}"
    for m in re.finditer(
            r'tgme_widget_message_(photo_wrap|video_thumb)[^>]*?' + _BG_URL, chunk):
        url = htmllib.unescape(m.group(2))
        if m.group(1) == "photo_wrap":
            media.append({"type": "photo", "url": url})
        else:
            media.append({"type": "video", "thumb": url, "tg_url": tg_url})
    # a video with no visible thumbnail still counts as one media item
    if not media and "tgme_widget_message_video" in chunk:
        media.append({"type": "video", "thumb": "", "tg_url": tg_url})
    return media


def _parse_preview(chunk):
    """The link-preview card of a post, or None if there isn't one."""
    lp = re.search(r'tgme_widget_message_link_preview.*?(?=</a>)', chunk, re.S)
    if not lp:
        return None
    block = lp.group(0)

    def grab(pat):
        m = re.search(pat, block, re.S)
        return htmllib.unescape(re.sub("<[^>]+>", "", m.group(1)).strip()) if m else ""

    img = _bg_urls(block, "link_preview_image")
    href = re.search(r'href="([^"]+)"', block)
    preview = {
        "site": grab(r'link_preview_site_name[^>]*>(.*?)<'),
        "title": grab(r'link_preview_title[^>]*>(.*?)</div>'),
        "desc": grab(r'link_preview_description[^>]*>(.*?)</div>'),
        "image": img[0] if img else "",
        "url": htmllib.unescape(href.group(1)) if href else "",
    }
    if not (preview["title"] or preview["desc"] or preview["image"]):
        return None
    return preview


_POST_ID_RE = re.compile(rf'data-post="{re.escape(CHANNEL)}/(\d+)"')


def parse_posts(page_html):
    """Parse a t.me/s page into posts (DOM order = oldest -> newest)."""
    chunks = page_html.split('js-widget_message_wrap')[1:]
    posts = []
    for chunk in chunks:
        mid = _POST_ID_RE.search(chunk)
        if not mid:
            continue
        pid = int(mid.group(1))

        dt = re.search(r'datetime="([^"]+)"', chunk)
        dt_iso = dt.group(1) if dt else ""

        txt = re.search(
            r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', chunk, re.S)
        text_html = sanitize(txt.group(1)) if txt else ""

        media = _parse_media(chunk, pid)
        preview = _parse_preview(chunk)

        if not text_html and not media and not preview:
            continue

        posts.append({
            "id": pid,
            "url": f"https://t.me/{CHANNEL}/{pid}",
            "datetime": dt_iso,
            "text_html": text_html,
            "media": media,
            "preview": preview,
        })
    return posts


def page_min_id(posts):
    return min((p["id"] for p in posts), default=None) if posts else None


def parse_channel_meta(page_html):
    """Channel branding (title/description) scraped from the t.me/s header."""
    title = re.search(r'tgme_channel_info_header_title[^>]*>\s*<span[^>]*>([^<]+)', page_html)
    desc = re.search(r'tgme_channel_info_description[^>]*>(.*?)</div>', page_html, re.S)
    desc_text = ""
    if desc:
        desc_text = htmllib.unescape(re.sub(r"<[^>]+>", " ", desc.group(1))).strip()
        desc_text = re.sub(r"\s+", " ", desc_text)
    return {
        "username": CHANNEL,
        "url": f"https://t.me/{CHANNEL}",
        "title": htmllib.unescape(title.group(1)).strip() if title else CHANNEL,
        "description": desc_text,
    }
