#!/usr/bin/env python3
"""
sync.py — background mirror worker.

Two jobs, both rate-limited so Telegram doesn't ban us:
  1. poll newest page often  -> low lag for fresh posts
  2. backfill older history   -> gradually, a couple pages per cycle

Politeness:
  * one t.me page request at a time, with a randomized delay between them
  * exponential backoff on errors, long sleep on HTTP 429
  * media downloads are spaced out too (handled in store.save_post)
"""
import time
import random
import threading
import urllib.error

import scrape
import store
from config import POLL_INTERVAL, PAGE_DELAY, BACKFILL_PAGES_PER_CYCLE, MAX_POSTS

# Delays (POLL_INTERVAL, PAGE_DELAY, ...) come from config/env; defaults are
# deliberately gentle: one light request to Telegram at a time, well spaced.
BACKOFF_429 = 300           # 5 min cool-down if Telegram ever rate-limits us


def _sleep(rng):
    time.sleep(random.uniform(*rng))


def _fetch_html(before=None):
    """Fetch one t.me/s page, handling rate limits. Returns HTML or None."""
    for attempt in range(4):
        try:
            return scrape.fetch_html(before)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(BACKOFF_429)
            else:
                time.sleep(5 * (attempt + 1))
        except Exception:
            time.sleep(5 * (attempt + 1))
    return None


def _fetch(before=None):
    """Fetch+parse one page. Returns posts or None."""
    html = _fetch_html(before)
    return scrape.parse_posts(html) if html is not None else None


_MAX_POST_ID = 10 ** 12   # larger than any real Telegram post id; used as "+inf"


def _store_missing(st, posts):
    """Persist every post from `posts` we don't already have. Returns the count."""
    added = 0
    for p in sorted(posts, key=lambda p: p["id"]):
        if not st.has(p["id"]):
            st.add(store.save_post(p))
            added += 1
    return added


def sync_new(st, state):
    """Fetch the newest page and store new posts. Low-lag path.

    If more than one page of posts appeared since the last poll, the older of
    them are no longer on the newest page — walk backward to fill that gap so no
    post is silently skipped."""
    prev_max = st.max_id()
    html = _fetch_html(None)
    if html is None:
        return 0
    st.set_meta(scrape.parse_channel_meta(html))   # refresh channel branding
    posts = scrape.parse_posts(html)
    if not posts:
        return 0
    added = _store_missing(st, posts)

    if prev_max is not None:
        before = scrape.page_min_id(posts)
        while before is not None and before > prev_max + 1:
            older = _fetch(before)
            if not older:
                break
            added += _store_missing(st, older)
            nxt = scrape.page_min_id(older)
            if nxt is None or nxt >= before:
                break
            before = nxt
            _sleep(PAGE_DELAY)

    state["max_id"] = st.max_id()
    if state.get("min_id") is None:
        state["min_id"] = st.min_id()
    store.save_state(state)
    return added


def backfill(st, state, pages=BACKFILL_PAGES_PER_CYCLE):
    """Walk older history a few pages at a time until exhausted."""
    if state.get("backfill_done"):
        return 0
    # honour a total-posts cap (0 = unlimited): stop backfilling once reached.
    if MAX_POSTS and st.count() >= MAX_POSTS:
        state["backfill_done"] = True
        store.save_state(state)
        return 0
    added = 0
    for _ in range(pages):
        before = state.get("min_id")
        posts = _fetch(before)
        if posts is None:
            break
        fresh = [p for p in posts if not st.has(p["id"])]
        page_min = scrape.page_min_id(posts)
        # No new posts and the page reaches no further back than our cursor =>
        # we've hit the bottom of the channel's history.
        reached_bottom = before is not None and not fresh and (page_min or 0) >= before
        if not posts or reached_bottom:
            state["backfill_done"] = True
            store.save_state(state)
            break
        added += _store_missing(st, posts)
        state["min_id"] = min(state.get("min_id") or _MAX_POST_ID, page_min or _MAX_POST_ID)
        store.save_state(state)
        _sleep(PAGE_DELAY)
    return added


def run_loop(st):
    """Forever: keep newest fresh, keep backfilling until history is complete."""
    state = store.load_state()
    while True:
        try:
            sync_new(st, state)
            if not state.get("backfill_done"):
                _sleep(PAGE_DELAY)
                backfill(st, state)
        except Exception as e:
            print("sync error:", e)
        time.sleep(POLL_INTERVAL)


def start(st):
    """Start the background worker. Non-blocking: the server serves the on-disk
    cache immediately while the worker refreshes and backfills."""
    t = threading.Thread(target=run_loop, args=(st,), daemon=True)
    t.start()
    return t
