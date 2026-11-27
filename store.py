#!/usr/bin/env python3
"""
store.py — the LOCAL mirror on disk.

Layout:
  data/
    posts/
      2026-07-09__793/
        post.md            # frontmatter + markdown body (canonical, human-readable)
        media/01.jpg …      # downloaded photos / video posters / preview image
    state.json             # sync cursor {min_id, max_id, backfill_done}

The server serves everything from here — the browser never touches Telegram.
post.md is the source of truth; the server parses it into the JSON the frontend
expects (text_html is rendered from the markdown body).
"""
import os
import re
import json
import time
import random
import hashlib
import threading
import html as htmllib
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from scrape import UA        # single source of truth for the User-Agent
from config import MEDIA_DELAY

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
POSTS = DATA / "posts"
STATE_FILE = DATA / "state.json"
META_FILE = DATA / "meta.json"

# Refuse to buffer a media file bigger than this. Telegram photos/posters are
# well under it; a runaway response must not exhaust RAM.
MAX_MEDIA_BYTES = 32 * 1024 * 1024

# Block-level markdown is stashed behind these markers so inline processing
# (emphasis, links) cannot rewrite characters inside a code block or quote.
_STASH = "\x01%d\x01"
_STASH_RE = re.compile(r"\x01(\d+)\x01")


# --------------------------------------------------------------------------
# HTML <-> Markdown
# --------------------------------------------------------------------------

def _unstash(s, stashed):
    """Substitute stash markers, repeatedly, so nested blocks resolve too."""
    for _ in range(8):
        new = _STASH_RE.sub(lambda m: stashed[int(m.group(1))], s)
        if new == s:
            break
        s = new
    return s


def html_to_md(text_html):
    """Telegram post HTML -> Markdown (for storage & readability)."""
    if not text_html:
        return ""
    stashed = []
    return _unstash(_html_to_md(text_html, stashed), stashed).strip()


def _html_to_md(s, stashed):
    """One pass of HTML -> Markdown. `stashed` collects block-level markdown
    (shared across recursion) so inline rules cannot touch its contents."""
    def stash(rendered):
        stashed.append(rendered)
        return _STASH % (len(stashed) - 1)

    def _pre(m):
        code = re.sub(r"<br\s*/?>", "\n", m.group(1), flags=re.I)
        code = htmllib.unescape(re.sub(r"<[^>]+>", "", code)).strip("\n")
        return stash("\n\n```\n" + code + "\n```\n\n")

    def _quote(m):
        inner = _html_to_md(m.group(1), stashed).strip()
        quoted = "\n".join(("> " + ln).rstrip() for ln in inner.split("\n"))
        return stash("\n\n" + quoted + "\n\n")

    # Block-level first: their content must survive inline conversion untouched.
    s = re.sub(r"<pre[^>]*>(.*?)</pre>", _pre, s, flags=re.S | re.I)
    s = re.sub(r"<blockquote[^>]*>(.*?)</blockquote>", _quote, s, flags=re.S | re.I)

    # unwrap emoji wrappers, keeping the actual glyph
    s = re.sub(r'<i class="emoji"[^>]*>\s*<b>(.*?)</b>\s*</i>', r'\1', s, flags=re.S | re.I)
    s = re.sub(r'<tg-emoji[^>]*>(.*?)</tg-emoji>', r'\1', s, flags=re.S | re.I)
    s = re.sub(r'<br\s*/?>', '\n', s, flags=re.I)
    # Spoiler markup has no Markdown equivalent; '||' is the convention Telegram
    # itself uses in plain text, and md_to_html turns it back into <tg-spoiler>.
    s = re.sub(r'<tg-spoiler[^>]*>(.*?)</tg-spoiler>', r'||\1||', s, flags=re.S | re.I)
    # links before inline styles so their text can still be styled.
    # Percent-encode parens in the URL and bracket-escape the text so md_to_html
    # can parse [text](url) unambiguously for URLs with '(' ')' and text with ']'.
    s = re.sub(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
               lambda m: "[%s](%s)" % (
                   m.group(2).replace("[", r"\[").replace("]", r"\]"),
                   m.group(1).replace('(', '%28').replace(')', '%29')),
               s, flags=re.S | re.I)
    s = re.sub(r'</?(?:b|strong)>', '**', s, flags=re.I)
    s = re.sub(r'</?(?:i|em)>', '_', s, flags=re.I)
    s = re.sub(r'</?(?:s|strike|del)>', '~~', s, flags=re.I)
    s = re.sub(r'<code>(.*?)</code>', r'`\1`', s, flags=re.S | re.I)
    s = re.sub(r'<[^>]+>', '', s)              # strip anything left (spans…)
    s = htmllib.unescape(s)
    s = re.sub(r'[ \t]+\n', '\n', s)
    return re.sub(r'\n{3,}', '\n\n', s)


# Link schemes we turn into real <a>; anything else stays literal text.
_SAFE_LINK = re.compile(r'^(?:https?:|mailto:|tg:|//|/)', re.I)


def _emphasis(s):
    """Apply spoiler/bold/strike/code/italic inline markdown to escaped text."""
    # '||text||' -> spoiler. Require non-space right inside the bars so a
    # literal "a || b" in prose is not mistaken for markup.
    s = re.sub(r'\|\|(?=\S)(.+?)(?<=\S)\|\|', r'<tg-spoiler>\1</tg-spoiler>', s, flags=re.S)
    s = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', s)
    s = re.sub(r'~~([^~]+)~~', r'<s>\1</s>', s)
    s = re.sub(r'`([^`]+)`', r'<code>\1</code>', s)
    s = re.sub(r'(?<![\w<"])_([^_<>"]+)_(?![\w>])', r'<i>\1</i>', s)
    return s


def _md_inline(s):
    s = htmllib.escape(s, quote=False)
    # Stash links as placeholders BEFORE emphasis runs, so bold/italic/code
    # substitutions can never rewrite characters inside an href (e.g. a URL
    # path segment like /_x_/ must not become /<i>x</i>/).
    links = []

    def _stash(m):
        text, url = m.group(1), m.group(2)
        if not _SAFE_LINK.match(url):
            return m.group(0)                  # unknown/unsafe scheme -> literal text
        # `url` is already HTML-escaped by the block escape() above; only guard
        # the attribute quote (re-escaping would double-encode '&').
        href = url.replace('"', '&quot;')
        text = text.replace(r"\[", "[").replace(r"\]", "]")
        links.append(f'<a href="{href}" target="_blank" rel="noopener nofollow">'
                     f'{_emphasis(text)}</a>')
        return f'\x00{len(links) - 1}\x00'

    s = re.sub(r'\[((?:[^\]\\]|\\.)+)\]\(([^)\s]+)\)', _stash, s)
    s = _emphasis(s)
    s = s.replace('\n', '<br>')
    return re.sub(r'\x00(\d+)\x00', lambda m: links[int(m.group(1))], s)


def md_to_html(md):
    """Markdown body -> HTML. Blank-line-separated blocks become <p> so the
    frontend controls paragraph spacing (first <p> is used as the title).
    ``` fences and > quotes become <pre>/<blockquote> instead."""
    if not md:
        return ""
    out = []
    lines = md.strip().split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            body, i = [], i + 1
            while i < len(lines) and not lines[i].startswith("```"):
                body.append(lines[i])
                i += 1
            out.append("<pre>" + htmllib.escape("\n".join(body), quote=False) + "</pre>")
            i += 1
        elif line.startswith(">"):
            quote, i = [], i
            while i < len(lines) and lines[i].startswith(">"):
                quote.append(lines[i][1:].lstrip())
                i += 1
            out.append("<blockquote>" + md_to_html("\n".join(quote)) + "</blockquote>")
        elif not line.strip():
            i += 1
        else:
            para = []
            while (i < len(lines) and lines[i].strip()
                   and not lines[i].startswith(("```", ">"))):
                para.append(lines[i])
                i += 1
            out.append(f"<p>{_md_inline(chr(10).join(para))}</p>")
    return "".join(out)


# --------------------------------------------------------------------------
# downloading (polite)
# --------------------------------------------------------------------------

def download(url, dest, referer=None, retries=3, timeout=30, max_bytes=MAX_MEDIA_BYTES):
    """Download url -> dest. Returns True on success. Backs off on failure.

    Reads in chunks and aborts over max_bytes so an oversized response cannot
    exhaust memory. The file is written to a temp path and renamed, so a failed
    download never leaves a truncated file behind under the post's media dir.
    """
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    for attempt in range(retries):
        tmp = None
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                declared = r.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise ValueError(f"media too large: {declared} bytes")
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_name(dest.name + ".part")
                size = 0
                with tmp.open("wb") as fh:
                    while True:
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > max_bytes:
                            raise ValueError(f"media too large: >{max_bytes} bytes")
                        fh.write(chunk)
            os.replace(tmp, dest)
            return True
        except Exception:
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass
            if attempt == retries - 1:
                return False
            time.sleep(3 * (attempt + 1))
    return False


def _ext(url, default=".jpg"):
    m = re.search(r'\.([a-zA-Z0-9]{2,4})(?:\?|$)', url.split('/')[-1])
    return "." + m.group(1).lower() if m else default


# --------------------------------------------------------------------------
# content fingerprinting (edit detection — see recheck.py)
# --------------------------------------------------------------------------

def _digest(payload):
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def content_hash(post):
    """Fingerprint of a post's CONTENT: text, media set, link-preview card.

    Deliberately excludes the post's own datetime and Telegram URL — those never
    change on an edit, and the datetime is the publish time, not the edit time.
    Telegram reuses media URLs for an unchanged photo, so a text-only edit leaves
    the media fingerprint alone."""
    pv = post.get("preview") or {}
    payload = json.dumps({
        "text": post.get("text_html", ""),
        "media": [{k: v for k, v in m.items() if k != "tg_url"}
                  for m in post.get("media", [])],
        "preview": {k: pv.get(k, "") for k in ("site", "title", "desc", "url", "image")},
    }, sort_keys=True, ensure_ascii=False)
    return _digest(payload)


def media_hash(post):
    """Fingerprint of the REMOTE media URLs, to skip re-downloading unchanged
    photos when only the text of a post was edited."""
    parts = [f"{m['type']}:{m.get('url') or m.get('thumb') or ''}"
             for m in post.get("media", [])]
    pv = post.get("preview") or {}
    if pv.get("image"):
        parts.append("preview:" + pv["image"])
    return _digest("|".join(parts))


# --------------------------------------------------------------------------
# writing / reading a post
# --------------------------------------------------------------------------

def _folder_name(pid, dt_iso):
    day = (dt_iso or "")[:10] or "0000-00-00"
    return f"{day}__{pid}"


def _post_md_path(post_id):
    """The post.md of a stored post by id, or None. Folder names are keyed by
    date, so the id has to be found by glob."""
    for md in POSTS.glob(f"*__{post_id}/post.md"):
        return md
    return None


def _read_frontmatter(path):
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.match(r"^---\n(.*?)\n---", raw, re.S)
    if not m:
        return None
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm


def stored_content_hash(post_id):
    """The content_hash recorded on disk for a post, or None if not stored.

    Posts mirrored before fingerprints existed return "" — the caller treats
    that as "unknown" and re-saves, which backfills the hash."""
    path = _post_md_path(post_id)
    if path is None:
        return None
    return (_read_frontmatter(path) or {}).get("content_hash", "")


def save_post(post, media_delay=MEDIA_DELAY, reuse_media=True, edited_at=""):
    """Persist one scraped post (with remote media) to disk. Returns a record.

    With reuse_media, an already-stored post whose REMOTE media fingerprint is
    unchanged keeps its downloaded files: an edit that only touches text costs
    zero downloads. `edited_at` is the wall-clock time we ourselves noticed a
    change (set by recheck.py) — Telegram's preview never exposes its own."""
    folder = POSTS / _folder_name(post["id"], post["datetime"])
    mdir = folder / "media"
    folder.mkdir(parents=True, exist_ok=True)

    new_media_hash = media_hash(post)
    old_md = folder / "post.md"
    old_fm = _read_frontmatter(old_md) if old_md.is_file() else None
    if (reuse_media and old_fm and old_fm.get("media_hash") == new_media_hash
            and old_fm.get("media_hash")):
        media_out, prev_out = _media_from_frontmatter(old_fm)
    else:
        media_out, prev_out = _download_media(post, mdir, media_delay)
        _prune_media(mdir, media_out, prev_out)

    body_md = html_to_md(post["text_html"])
    _write_md(old_md, post, media_out, prev_out, body_md,
              content_hash(post), new_media_hash, edited_at)
    return _record(post["id"], post["url"], post["datetime"], body_md,
                   folder.name, media_out, prev_out, edited_at)


def _download_media(post, mdir, media_delay):
    media_out = []
    for n, m in enumerate(post.get("media", []), start=1):
        if m["type"] == "photo":
            fn = f"{n:02d}{_ext(m['url'])}"
            ok = download(m["url"], mdir / fn, referer=post["url"])
            # keep the item even if the download failed (file=None) so the post
            # never becomes an empty card and the media count stays correct;
            # the frontend shows a placeholder for a missing local file.
            media_out.append({"type": "photo", "file": fn if ok else None})
            time.sleep(random.uniform(*media_delay))
        elif m["type"] == "video":
            entry = {"type": "video", "tg_url": m.get("tg_url", post["url"])}
            if m.get("thumb"):
                fn = f"{n:02d}{_ext(m['thumb'])}"
                if download(m["thumb"], mdir / fn, referer=post["url"]):
                    entry["file"] = fn
                time.sleep(random.uniform(*media_delay))
            media_out.append(entry)

    preview = post.get("preview")
    prev_out = None
    if preview:
        # collapse whitespace so these values stay single-line frontmatter
        # (a stray newline would otherwise inject a fake frontmatter key)
        prev_out = {k: _oneline(preview.get(k, "")) for k in ("site", "title", "desc", "url")}
        if preview.get("image"):
            fn = "preview" + _ext(preview["image"])
            if download(preview["image"], mdir / fn, referer=post["url"]):
                prev_out["image_file"] = fn
            time.sleep(random.uniform(*media_delay))

    return media_out, prev_out


def _media_from_frontmatter(fm):
    """Recover (media_out, prev_out) from stored frontmatter, so an unchanged
    media set can be re-saved without re-downloading anything."""
    media_out = []
    for tok in (fm.get("media") or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if ":photo" in tok:
            f = tok.split(":", 1)[0]
            media_out.append({"type": "photo", "file": None if f == "-" else f})
        elif ":video" in tok:
            parts = tok.split(":", 2)
            media_out.append({"type": "video",
                              "file": None if parts[0] == "-" else parts[0],
                              "tg_url": parts[2] if len(parts) > 2 else ""})
    prev_out = None
    if fm.get("preview_title") or fm.get("preview_url") or fm.get("preview_image"):
        prev_out = {k: fm.get("preview_" + k, "")
                    for k in ("site", "title", "desc", "url")}
        if fm.get("preview_image"):
            prev_out["image_file"] = fm["preview_image"]
    return media_out, prev_out


def _prune_media(mdir, media_out, prev_out):
    """Delete files in a post's media dir that the new media set no longer
    references (an edit that drops a photo must not leave it served forever)."""
    if not mdir.is_dir():
        return
    keep = {m["file"] for m in media_out if m.get("file")}
    if prev_out and prev_out.get("image_file"):
        keep.add(prev_out["image_file"])
    for f in mdir.iterdir():
        if f.is_file() and f.name not in keep and not f.name.endswith(".part"):
            try:
                f.unlink()
            except OSError:
                pass


def _oneline(s):
    """Collapse any whitespace (incl. newlines) to single spaces."""
    return re.sub(r"\s+", " ", s or "").strip()


def _write_md(path, post, media_out, prev_out, body_md, chash="", mhash="", edited=""):
    # media frontmatter token grammar (mirrored by _media_from_frontmatter):
    #   photo -> "<file>:photo"
    #   video -> "<file|->:video:<tg_url>"   ('-' means the poster failed to download)
    fm = ["---",
          f"id: {post['id']}",
          f"date: {post['datetime']}",
          f"url: {post['url']}"]
    if edited:
        fm.append(f"edited: {_oneline(edited)}")
    if chash:
        fm.append(f"content_hash: {chash}")
    if mhash:
        fm.append(f"media_hash: {mhash}")
    if media_out:
        parts = []
        for m in media_out:
            if m["type"] == "photo":
                parts.append(f"{m['file'] or '-'}:photo")
            else:
                parts.append(f"{m.get('file') or '-'}:video:{m['tg_url']}")
        fm.append("media: " + ", ".join(parts))
    if prev_out:
        fm.append(f"preview_url: {prev_out.get('url','')}")
        fm.append(f"preview_site: {prev_out.get('site','')}")
        fm.append(f"preview_title: {prev_out.get('title','')}")
        fm.append(f"preview_desc: {prev_out.get('desc','')}")
        if prev_out.get("image_file"):
            fm.append(f"preview_image: {prev_out['image_file']}")
    fm.append("---")
    path.write_text("\n".join(fm) + "\n\n" + body_md + "\n", encoding="utf-8")


def _record(pid, url, dt_iso, body_md, folder, media_out, prev_out, edited_at=""):
    """Build the JSON record the API serves for one post."""
    media = []
    for m in media_out:
        if m["type"] == "photo":
            # empty url when the local file is missing -> frontend shows a placeholder
            photo_url = f"/media/{folder}/media/{m['file']}" if m.get("file") else ""
            media.append({"type": "photo", "url": photo_url})
        else:
            e = {"type": "video", "url": m["tg_url"]}
            if m.get("file"):
                e["thumb"] = f"/media/{folder}/media/{m['file']}"
            media.append(e)
    preview = None
    if prev_out:
        preview = {"site": prev_out.get("site", ""), "title": prev_out.get("title", ""),
                   "desc": prev_out.get("desc", ""), "url": prev_out.get("url", "")}
        if prev_out.get("image_file"):
            preview["image"] = f"/media/{folder}/media/{prev_out['image_file']}"
    rec = {"id": pid, "url": url, "datetime": dt_iso,
           "text_html": md_to_html(body_md), "media": media, "preview": preview}
    if edited_at:
        rec["edited_at"] = edited_at
    return rec


def parse_post_md(path):
    """Read a stored post.md back into a serving record."""
    raw = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", raw, re.S)
    if not m:
        return None
    fm_text, body = m.group(1), m.group(2).strip()
    fm = {}
    for line in fm_text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    folder = path.parent.name

    media_out, prev_out = _media_from_frontmatter(fm)

    try:
        pid = int(fm.get("id", "0"))
    except ValueError:
        return None
    return _record(pid, fm.get("url", ""), fm.get("date", ""), body,
                   folder, media_out, prev_out, fm.get("edited", ""))


# --------------------------------------------------------------------------
# in-memory index (thread-safe)
# --------------------------------------------------------------------------

class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.by_id = {}
        self.meta = load_meta()
        POSTS.mkdir(parents=True, exist_ok=True)
        self.load()

    def set_meta(self, meta):
        with self.lock:
            self.meta = meta
        save_meta(meta)

    def get_meta(self):
        with self.lock:
            return dict(self.meta) if self.meta else None

    def load(self):
        with self.lock:
            self.by_id.clear()
            for md in POSTS.glob("*/post.md"):
                rec = parse_post_md(md)
                if rec:
                    self.by_id[rec["id"]] = rec

    def add(self, rec):
        with self.lock:
            self.by_id[rec["id"]] = rec

    def has(self, pid):
        with self.lock:
            return pid in self.by_id

    def count(self):
        with self.lock:
            return len(self.by_id)

    def min_id(self):
        with self.lock:
            return min(self.by_id) if self.by_id else None

    def max_id(self):
        with self.lock:
            return max(self.by_id) if self.by_id else None

    def get(self, pid):
        with self.lock:
            return self.by_id.get(pid)

    def page(self, before=None, limit=12):
        with self.lock:
            ids = sorted(self.by_id, reverse=True)
            if before is not None:
                ids = [i for i in ids if i < before]
            chunk = ids[:limit]
            posts = [self.by_id[i] for i in chunk]
            next_before = chunk[-1] if len(chunk) == limit and len(ids) > limit else None
            return {"posts": posts, "next_before": next_before}

    def ids_desc(self, limit=None):
        """Newest-first post ids — used to build the sitemap."""
        with self.lock:
            ids = sorted(self.by_id, reverse=True)
            return ids[:limit] if limit else ids


# --------------------------------------------------------------------------
# sync state cursor
# --------------------------------------------------------------------------

def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"min_id": None, "max_id": None, "backfill_done": False}


def load_meta():
    try:
        return json.loads(META_FILE.read_text())
    except Exception:
        return None


def save_meta(meta):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = META_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    os.replace(tmp, META_FILE)


def save_state(state):
    # atomic write: another thread (the /api/status handler) may read this file
    # concurrently, so write to a temp file and rename it into place.
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    os.replace(tmp, STATE_FILE)
