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

from config import CHANNEL

BASE = f"https://t.me/s/{CHANNEL}"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "a", "br", "code", "pre", "blockquote", "span", "tg-spoiler", "tg-emoji",
}


def fetch_html(before=None, timeout=20):
    url = BASE + (f"?before={before}" if before else "")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def sanitize(text_html):
    """Allowlist sanitizer for a post's text HTML (keeps inline formatting)."""
    if not text_html:
        return ""
    text_html = re.sub(r"<(script|style)\b.*?</\1>", "", text_html, flags=re.S | re.I)
    text_html = re.sub(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*')", "", text_html, flags=re.I)
    text_html = re.sub(r"javascript:", "#", text_html, flags=re.I)

    def repl(m):
        whole = m.group(0)
        closing = whole.startswith("</")
        tag = m.group(1).lower()
        if tag not in ALLOWED_TAGS:
            return ""
        if tag == "a" and not closing:
            href = re.search(r'href\s*=\s*"([^"]*)"', whole, flags=re.I)
            url = href.group(1) if href else "#"
            return f'<a href="{htmllib.escape(url, quote=True)}">'
        if tag == "a" and closing:
            return "</a>"
        return whole

    return re.sub(r"</?([a-zA-Z0-9-]+)[^>]*>", repl, text_html)


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


def parse_posts(page_html):
    """Parse a t.me/s page into posts (DOM order = oldest -> newest)."""
    chunks = page_html.split('js-widget_message_wrap')[1:]
    posts = []
    for chunk in chunks:
        mid = re.search(rf'data-post="{CHANNEL}/(\d+)"', chunk)
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
