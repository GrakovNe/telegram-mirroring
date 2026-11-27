#!/usr/bin/env python3
"""
render.py — server-side HTML so the mirror is actually indexable.

The frontend is a JS app: it fetches /api/posts and builds the feed at runtime,
which means a crawler that does not run JavaScript sees an empty <main> and
nothing to index. This module renders the same cards in Python from the local
store, so:

  * `GET /`            -> the first page of posts, in HTML
  * `GET /post/<id>`   -> one post, in HTML (a stable, shareable, indexable URL)
  * `GET /sitemap.xml` -> every mirrored post
  * `GET /robots.txt`  -> points at the sitemap

The client still hydrates the page: SSR markup is the first page, and infinite
scroll fetches the rest from /api/posts exactly as before.

The card markup here mirrors renderPost() in index.html. Keep the two in sync —
tests/test_mirror.py asserts the structural invariants that make hydration work.
"""
import html
import json
import re
from datetime import datetime, timezone

from config import CHANNEL, PUBLIC_URL

CAL_ICON = ('<svg viewBox="0 0 24 24" aria-hidden="true">'
            '<rect x="3" y="4.5" width="18" height="16" rx="2.5"/>'
            '<path d="M3 9h18M8 2.5v4M16 2.5v4"/></svg>')
STACK_ICON = ('<svg viewBox="0 0 24 24" aria-hidden="true">'
              '<path d="M4 6h12v12H4z" opacity=".55"/>'
              '<path d="M8 3h13v13h-2V5H8z"/></svg>')


def esc(value):
    return html.escape(str(value or ""), quote=True)


def base_url(host=None):
    """Absolute base URL for canonical/OG/sitemap links.

    PUBLIC_URL wins; otherwise the request's Host is used, but only if it looks
    like a hostname — a spoofed Host header must not end up in a canonical tag."""
    if PUBLIC_URL:
        return PUBLIC_URL
    if host and re.fullmatch(r"[A-Za-z0-9.\-]+(?::\d{1,5})?", host):
        return f"http://{host}"
    return ""


def fmt_date(iso_dt):
    """'2026-11-27T16:33:04+00:00' -> '27 November 2026, 16:33' (UTC wall clock).

    Mirrors fmtDate() in index.html, which deliberately keeps the post's own
    UTC time instead of converting to the reader's zone."""
    if not iso_dt:
        return ""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})", iso_dt)
    if not m:
        return ""
    year, month, day, hh, mm = m.groups()
    try:
        dt = datetime(int(year), int(month), int(day), tzinfo=timezone.utc)
        label = dt.strftime("%d %B %Y").lstrip("0")
    except ValueError:
        return ""
    return f"{label}, {hh}:{mm}"


def _top_level_blocks(text_html):
    """Top-level blocks of a rendered post: <p>, <pre>, <blockquote>.

    The first is the card title, the rest is the collapsible body — the same
    split renderPost() makes in the browser."""
    out, depth, buf = [], 0, []
    for m in re.finditer(r"<(/?)(p|pre|blockquote)\b[^>]*>", text_html, re.I):
        closing, tag = bool(m.group(1)), m.group(2).lower()
        if not closing:
            if depth == 0:
                buf = [m.start()]
            depth += 1
        else:
            depth = max(0, depth - 1)
            if depth == 0 and buf:
                out.append(text_html[buf[0]:m.end()])
                buf = []
    return out


def _headline(text_html):
    """Plain text of a post's first block — its title everywhere a title is
    needed: the card, the page <title>, JSON-LD, and the RSS item."""
    blocks = _top_level_blocks(text_html)
    source = blocks[0] if blocks else text_html
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", source)).strip()


def _title_markup(block):
    """A title block with its outer <p> unwrapped (matches the client)."""
    m = re.match(r"^<p>(.*)</p>$", block, re.S | re.I)
    return m.group(1) if m else block


def _media_html(post):
    media = post.get("media") or []
    if not media:
        return ""
    first = media[0]
    if first.get("type") == "video":
        inner = ""
        if first.get("thumb"):
            inner += f'<img src="{esc(first["thumb"])}" alt="" loading="lazy">'
        inner += '<div class="play"></div>'
        frame = (f'<div class="video">{inner}</div>')
        href = esc(first.get("url") or post.get("url"))
        hero = (f'<div class="hero"><div class="hero-frame" data-video="{href}">'
                f"{frame}</div></div>")
    else:
        src = first.get("url") or ""
        img = (f'<img class="photo img-fade loaded" src="{esc(src)}" alt="" loading="lazy">'
               if src else '<div class="media-broken"></div>')
        hero = (f'<div class="hero"><div class="hero-frame" data-carousel="1">'
                f"{img}</div></div>")
    if len(media) > 1:
        hero = hero.replace('</div></div>',
                            f'<div class="count-badge">{STACK_ICON}{len(media)}</div>'
                            '</div></div>', 1)
    return hero


def _preview_html(post):
    pv = post.get("preview")
    if not pv:
        return ""
    href = pv.get("url") or ""
    if not re.match(r"^https?:", href, re.I):
        href = post.get("url", "")
    parts = []
    for cls, key in (("site", "site"), ("title", "title"), ("desc", "desc")):
        if pv.get(key):
            parts.append(f'<div class="{cls}">{esc(pv[key])}</div>')
    if pv.get("image"):
        parts.append(f'<img src="{esc(pv["image"])}" alt="" loading="lazy">')
    return (f'<a class="preview" href="{esc(href)}" target="_blank" '
            f'rel="noopener nofollow">{"".join(parts)}</a>')


def post_html(post, base="", single=False):
    """One <article> — the same shape renderPost() builds in the browser.

    Text blocks are rendered fully expanded (no `collapsed` class); the client
    adds the collapse + "Read more" enhancement after load, so a crawler and a
    user with JS disabled both get the complete post."""
    blocks = _top_level_blocks(post.get("text_html", ""))
    title_block = blocks[0] if blocks else ""
    body_blocks = blocks[1:]

    edited = ('<span class="post-edited">edited</span>' if post.get("edited_at") else "")
    head = (f'<div class="post-head">{edited}'
            f'<a class="post-date" href="{esc(post.get("url"))}" target="_blank" '
            f'rel="noopener">{CAL_ICON}<span>{esc(fmt_date(post.get("datetime")))}</span></a></div>')

    canonical = f'{base}/post/{post["id"]}'
    title_text = _headline(post.get("text_html", "")) or f"Post {post['id']}"

    # data-media lets the client hydrate the carousel from SSR markup without
    # re-fetching the post.
    media_json = esc(json.dumps(post.get("media") or [], ensure_ascii=False))
    out = [f'<article class="post" id="post-{post["id"]}" data-id="{post["id"]}" '
           f'data-media="{media_json}">']
    out.append(head)
    out.append(_media_html(post))
    if title_block:
        tag = "h1" if single else "div"
        cls = "post-title" if single else "post-title"
        out.append(f'<{tag} class="{cls}">{_title_markup(title_block)}</{tag}>')
    if body_blocks:
        out.append(f'<div class="post-text">{"".join(body_blocks)}</div>')
    out.append(_preview_html(post))
    out.append("</article>")
    return "".join(out), title_text, canonical


def _ld_json(post, meta, base):
    """schema.org SocialMediaPosting for one post page."""
    data = {
        "@context": "https://schema.org",
        "@type": "SocialMediaPosting",
        "headline": _headline(post.get("text_html", ""))[:110],
        "url": f'{base}/post/{post["id"]}',
        "datePublished": post.get("datetime", ""),
        "dateModified": post.get("edited_at") or post.get("datetime", ""),
        "isPartOf": {"@type": "WebSite", "name": (meta or {}).get("title", CHANNEL),
                     "url": base or None},
        "mainEntityOfPage": {"@type": "WebPage", "@id": f'{base}/post/{post["id"]}'},
    }
    thumbs = [m["thumb"] for m in (post.get("media") or []) if m.get("thumb")]
    photos = [m["url"] for m in (post.get("media") or []) if m.get("url")]
    images = [u for u in (thumbs + photos) if u]
    if images:
        data["image"] = images[0] if len(images) == 1 else images[:3]
    return json.dumps(data, ensure_ascii=False)


def _head(title, description, canonical, base, lang, extra="", og_type="website"):
    """Tags injected at <!--SSR:HEAD-->; <title> and the description meta are
    replaced in place so the page keeps exactly one of each."""
    tags = [f'<link rel="canonical" href="{esc(canonical)}">']
    if base:
        tags += [
            f'<meta property="og:type" content="{esc(og_type)}">',
            f'<meta property="og:site_name" content="{esc(title[:100])}">',
            f'<meta property="og:title" content="{esc(title[:100])}">',
            f'<meta property="og:description" content="{esc(description[:200])}">',
            f'<meta property="og:url" content="{esc(canonical)}">',
            f'<meta property="og:locale" content="{"ru_RU" if lang == "ru" else "en_US"}">',
            '<meta name="twitter:card" content="summary">',
            f'<meta name="twitter:title" content="{esc(title[:100])}">',
            f'<meta name="twitter:description" content="{esc(description[:200])}">',
            f'<link rel="alternate" type="application/rss+xml" title="{esc(title)}" href="/rss">',
        ]
    if extra:
        tags.append(extra)
    return "\n".join(tags)


def _pick_lang(accept_language):
    """'ru' when Accept-Language prefers Russian, else 'en' (matches the client)."""
    if not accept_language:
        return "en"
    for part in accept_language.lower().split(","):
        tag = part.strip().split(";")[0].strip()
        if tag.startswith("ru"):
            return "ru"
        if tag:
            return "en"
    return "en"


def _shell(template, head, lang, body, state, footer_title, footer_url,
           title, description):
    """Fill the index.html shell. Markers are single-occurrence HTML comments;
    <title> and the description meta are rewritten in place — the static file
    carries placeholders, and a page must not ship two of either."""
    out = template
    out = re.sub(r"<title>.*?</title>", "<title>" + esc(title) + "</title>",
                 out, count=1, flags=re.S)
    out = re.sub(r'<meta name="description"[^>]*>',
                 '<meta name="description" id="meta-desc" '
                 'content="' + esc(description[:300]) + '">',
                 out, count=1)
    out = out.replace("<!--SSR:HEAD-->", head)
    out = out.replace('<html lang="ru">', f'<html lang="{lang}">')
    out = out.replace("<!--SSR:BODY-->", body)
    out = out.replace("<!--SSR:STATE-->",
                      "<script>window.__SSR__ = "
                      + json.dumps(state, ensure_ascii=False) + ";</script>")
    year = datetime.now(timezone.utc).year
    out = out.replace("<!--SSR:FOOTER-->",
                      f'© {year} · <a href="{esc(footer_url)}" '
                      f'target="_blank" rel="noopener">{esc(footer_title)}</a>')
    # The wordmark is filled by JS; render it too so the site name is in the
    # HTML a crawler (or a browser without JS) sees.
    out = out.replace("<!--SSR:WORDMARK-->", esc(footer_title))
    return out


def render_feed(template, posts, next_before, meta, base, accept_language=None):
    """`GET /` — the feed with its first page already in the HTML."""
    lang = _pick_lang(accept_language)
    title = (meta or {}).get("title") or CHANNEL
    description = (meta or {}).get("description") or ""
    if not description and posts:      # fall back to the newest post's text
        description = _plain(posts[0].get("text_html", ""))
    body = "".join(post_html(post, base)[0] for post in posts)
    state = {"nextBefore": next_before, "ids": [p["id"] for p in posts]}
    head = _head(title, description, f"{base}/", base, lang)
    return _shell(template, head, lang, body, state,
                  title, (meta or {}).get("url") or f"https://t.me/{CHANNEL}",
                  title, description)


def _plain(text_html):
    """Visible text of a post, whitespace-collapsed (for meta descriptions)."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text_html or "")).strip()


def render_post(template, post, meta, base, accept_language=None):
    """`GET /post/<id>` — a single post as its own indexable page."""
    lang = _pick_lang(accept_language)
    channel_title = (meta or {}).get("title") or CHANNEL
    card, title_text, canonical = post_html(post, base, single=True)
    description = _plain(post.get("text_html", ""))
    ld = _ld_json(post, meta, base)
    extra = (f'<script type="application/ld+json">{ld}</script>\n'
             f'<link rel="prev" href="/">\n')
    head = _head(f"{title_text} · {channel_title}", description, canonical,
                 base, lang, extra, og_type="article")
    nav = ('<nav class="post-nav"><a href="/">← All posts</a>'
           f'<a href="{esc(post.get("url"))}" target="_blank" rel="noopener">'
           'Open in Telegram ↗</a></nav>')
    body = nav + card
    state = {"nextBefore": None, "ids": [post["id"]], "single": True}
    return _shell(template, head, lang, body, state,
                  channel_title, (meta or {}).get("url") or f"https://t.me/{CHANNEL}",
                  f"{title_text} · {channel_title}", description)


def render_sitemap(entries, base):
    """sitemap.xml. `entries` is an iterable of (post_id, datetime_iso)."""
    if not base:
        return None
    urls = [f'  <url><loc>{esc(base)}/</loc></url>']
    for pid, dt in entries:
        lastmod = f"<lastmod>{esc(dt[:10])}</lastmod>" if dt[:10] else ""
        urls.append(f'  <url><loc>{esc(base)}/post/{pid}</loc>{lastmod}</url>')
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "\n".join(urls) + "\n</urlset>\n")


def render_robots(base):
    robots = ["User-agent: *", "Allow: /", "Disallow: /api/", ""]
    if base:
        robots.append(f"Sitemap: {base}/sitemap.xml")
    return "\n".join(robots) + "\n"


def render_rss(posts, meta, base):
    """RSS 2.0 feed — cheap extra surface for indexers and feed readers."""
    if not base:
        return None
    title = (meta or {}).get("title") or CHANNEL
    desc = (meta or {}).get("description") or ""
    items = []
    for post in posts:
        text = post.get("text_html", "")
        plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()
        headline = _headline(text)
        link = f"{base}/post/{post['id']}"
        items.append(
            "    <item>\n"
            f"      <title>{esc(headline[:120] or 'Post %s' % post['id'])}</title>\n"
            f"      <link>{esc(link)}</link>\n"
            f"      <guid isPermaLink=\"true\">{esc(link)}</guid>\n"
            f"      <pubDate>{esc(rfc822(post.get('datetime', '')))}</pubDate>\n"
            f"      <description>{esc(plain[:500])}</description>\n"
            "    </item>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<rss version="2.0"><channel>\n'
            f"  <title>{esc(title)}</title>\n"
            f"  <link>{esc(base)}/</link>\n"
            f"  <description>{esc(desc)}</description>\n"
            + "\n".join(items) + "\n</channel></rss>\n")


def rfc822(iso_dt):
    try:
        dt = datetime.fromisoformat(iso_dt)
    except (ValueError, TypeError):
        return ""
    return dt.astimezone(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
