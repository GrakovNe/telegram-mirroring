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
import threading
import html as htmllib
import urllib.request
from pathlib import Path

from scrape import UA        # single source of truth for the User-Agent
from config import MEDIA_DELAY

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
POSTS = DATA / "posts"
STATE_FILE = DATA / "state.json"
META_FILE = DATA / "meta.json"


# --------------------------------------------------------------------------
# HTML <-> Markdown
# --------------------------------------------------------------------------

def html_to_md(text_html):
    """Telegram post HTML -> Markdown (for storage & readability)."""
    if not text_html:
        return ""
    s = text_html
    # unwrap emoji wrappers, keeping the actual glyph
    s = re.sub(r'<i class="emoji"[^>]*>\s*<b>(.*?)</b>\s*</i>', r'\1', s, flags=re.S | re.I)
    s = re.sub(r'<tg-emoji[^>]*>(.*?)</tg-emoji>', r'\1', s, flags=re.S | re.I)
    s = re.sub(r'<br\s*/?>', '\n', s, flags=re.I)
    # links before inline styles so their text can still be styled.
    # Percent-encode parens in the URL so md_to_html can parse [text](url)
    # unambiguously even for URLs that contain '(' or ')'.
    s = re.sub(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
               lambda m: "[%s](%s)" % (m.group(2),
                                       m.group(1).replace('(', '%28').replace(')', '%29')),
               s, flags=re.S | re.I)
    s = re.sub(r'</?(?:b|strong)>', '**', s, flags=re.I)
    s = re.sub(r'</?(?:i|em)>', '_', s, flags=re.I)
    s = re.sub(r'</?(?:s|strike|del)>', '~~', s, flags=re.I)
    s = re.sub(r'<code>(.*?)</code>', r'`\1`', s, flags=re.S | re.I)
    s = re.sub(r'<[^>]+>', '', s)              # strip anything left (spans, spoiler, pre…)
    s = htmllib.unescape(s)
    s = re.sub(r'[ \t]+\n', '\n', s)
    s = re.sub(r'\n{3,}', '\n\n', s).strip()
    return s


# Link schemes we turn into real <a>; anything else stays literal text.
_SAFE_LINK = re.compile(r'^(?:https?:|mailto:|tg:|//|/)', re.I)


def _emphasis(s):
    """Apply bold/strike/code/italic inline markdown to escaped text."""
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
        links.append(f'<a href="{href}" target="_blank" rel="noopener nofollow">'
                     f'{_emphasis(text)}</a>')
        return f'\x00{len(links) - 1}\x00'

    s = re.sub(r'\[([^\]]+)\]\(([^)\s]+)\)', _stash, s)
    s = _emphasis(s)
    s = s.replace('\n', '<br>')
    return re.sub(r'\x00(\d+)\x00', lambda m: links[int(m.group(1))], s)


def md_to_html(md):
    """Markdown body -> HTML. Blank-line-separated blocks become <p> so the
    frontend controls paragraph spacing (first <p> is used as the title)."""
    if not md:
        return ""
    blocks = re.split(r'\n{2,}', md.strip())
    return "".join(f"<p>{_md_inline(b)}</p>" for b in blocks if b.strip())


# --------------------------------------------------------------------------
# downloading (polite)
# --------------------------------------------------------------------------

def download(url, dest, referer=None, retries=3, timeout=30):
    """Download url -> dest. Returns True on success. Backs off on failure."""
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            return True
        except Exception:
            if attempt == retries - 1:
                return False
            time.sleep(3 * (attempt + 1))
    return False


def _ext(url, default=".jpg"):
    m = re.search(r'\.([a-zA-Z0-9]{2,4})(?:\?|$)', url.split('/')[-1])
    return "." + m.group(1).lower() if m else default


# --------------------------------------------------------------------------
# writing / reading a post
# --------------------------------------------------------------------------

def _folder_name(pid, dt_iso):
    day = (dt_iso or "")[:10] or "0000-00-00"
    return f"{day}__{pid}"


def save_post(post, media_delay=MEDIA_DELAY):
    """Persist one scraped post (with remote media) to disk. Returns a record."""
    folder = POSTS / _folder_name(post["id"], post["datetime"])
    mdir = folder / "media"
    folder.mkdir(parents=True, exist_ok=True)

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

    body_md = html_to_md(post["text_html"])
    _write_md(folder / "post.md", post, media_out, prev_out, body_md)
    return _record(post["id"], post["url"], post["datetime"], body_md,
                   folder.name, media_out, prev_out)


def _oneline(s):
    """Collapse any whitespace (incl. newlines) to single spaces."""
    return re.sub(r"\s+", " ", s or "").strip()


def _write_md(path, post, media_out, prev_out, body_md):
    # media frontmatter token grammar (mirrored by parse_post_md):
    #   photo -> "<file>:photo"
    #   video -> "<file|->:video:<tg_url>"   ('-' means the poster failed to download)
    fm = ["---",
          f"id: {post['id']}",
          f"date: {post['datetime']}",
          f"url: {post['url']}"]
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


def _record(pid, url, dt_iso, body_md, folder, media_out, prev_out):
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
    return {"id": pid, "url": url, "datetime": dt_iso,
            "text_html": md_to_html(body_md), "media": media, "preview": preview}


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

    media_out, prev_out = [], None
    if fm.get("media"):
        for tok in fm["media"].split(","):
            tok = tok.strip()
            if ":photo" in tok:
                f = tok.split(":", 1)[0]
                media_out.append({"type": "photo", "file": None if f == "-" else f})
            elif ":video" in tok:
                parts = tok.split(":", 2)   # file:video:tg_url(with colons)
                media_out.append({"type": "video",
                                  "file": None if parts[0] == "-" else parts[0],
                                  "tg_url": parts[2] if len(parts) > 2 else ""})
    if fm.get("preview_title") or fm.get("preview_url") or fm.get("preview_image"):
        prev_out = {"site": fm.get("preview_site", ""), "title": fm.get("preview_title", ""),
                    "desc": fm.get("preview_desc", ""), "url": fm.get("preview_url", "")}
        if fm.get("preview_image"):
            prev_out["image_file"] = fm["preview_image"]

    try:
        pid = int(fm.get("id", "0"))
    except ValueError:
        return None
    return _record(pid, fm.get("url", ""), fm.get("date", ""), body,
                   folder, media_out, prev_out)


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

    def page(self, before=None, limit=12):
        with self.lock:
            ids = sorted(self.by_id, reverse=True)
            if before is not None:
                ids = [i for i in ids if i < before]
            chunk = ids[:limit]
            posts = [self.by_id[i] for i in chunk]
            next_before = chunk[-1] if len(chunk) == limit and len(ids) > limit else None
            return {"posts": posts, "next_before": next_before}


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
