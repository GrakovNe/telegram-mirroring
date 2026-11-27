#!/usr/bin/env python3
"""
recheck.py — notice edits to already-mirrored posts.

Telegram lets an author edit a post after publishing (in practice for about a
day) and the public web preview silently starts serving the new text. The sync
loop only ever looks for posts it has never seen, so without this module an edit
would never reach the mirror.

Frequency follows a geometric backoff anchored to the post's PUBLISH time:
gaps of FIRST, FIRST*FACTOR, FIRST*FACTOR^2 ... so checks are dense right after
publishing (when nearly all edits happen) and thin out towards the end of the
window. Because the schedule is derived from the publish time rather than from
"when did we last look", it is restart-safe: a mirror that was down for hours
resumes at the right point instead of hammering Telegram at first-check cadence.

Cost control: t.me/s/<channel>/<id> answers with a window of ~15 posts around
<id>, so due posts are grouped into id-clusters and one request re-checks many
posts. Requests use the same retry helper as the sync loop, and the cycle runs
on the sync thread, so the "one t.me request at a time" rule still holds.

Detection is by content fingerprint (store.content_hash), not by Telegram's
"edited" label: that label disappears again after a while, and the datetime
attribute stays the publish time, so neither reveals a later edit.
"""
import datetime
import json
import math
import os
import random
import time

import scrape
import store
from config import (RECHECK_ENABLED, RECHECK_WINDOW, RECHECK_FIRST_INTERVAL,
                    RECHECK_FACTOR, RECHECK_MAX_PAGES, PAGE_DELAY)

SCHEDULE_FILE = store.DATA / "recheck.json"

# t.me/s/<channel>/<id> returns a window of ~15 posts; group due ids so one
# fetch covers the whole group.
WINDOW_SPAN = 12
# Stop watching a post after this many consecutive checks where the page that
# should have contained it did not (deleted post, or one we cannot preview).
MAX_MISSES = 3


# --------------------------------------------------------------------------
# backoff math (all anchored to the publish time)
# --------------------------------------------------------------------------

def interval_at(checks):
    """Gap after the n-th check: FIRST, FIRST*FACTOR, FIRST*FACTOR^2, ..."""
    return RECHECK_FIRST_INTERVAL * (RECHECK_FACTOR ** checks)


def elapsed_after(checks):
    """Total time since publish once `checks` checks have been made."""
    f = RECHECK_FACTOR
    if f == 1:
        return RECHECK_FIRST_INTERVAL * checks
    return RECHECK_FIRST_INTERVAL * (f ** checks - 1) / (f - 1)


def checks_by(now, published):
    """How many checks the schedule says have already come due by `now`."""
    elapsed = max(0.0, now - published)
    if elapsed <= 0:
        return 0
    f = RECHECK_FACTOR
    if f == 1:
        return int(elapsed // RECHECK_FIRST_INTERVAL)
    n = math.log(elapsed * (f - 1) / RECHECK_FIRST_INTERVAL + 1, f)
    return max(0, int(math.floor(n)))


def next_due(published, checks):
    """Publish-anchored deadline of the (checks+1)-th check."""
    return published + elapsed_after(checks + 1)


# --------------------------------------------------------------------------
# schedule persistence: {post_id: {"due": float, "checks": int, "misses": int}}
# --------------------------------------------------------------------------

def load_schedule():
    try:
        data = json.loads(SCHEDULE_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_schedule(schedule):
    # Atomic: /api/status may read this file concurrently.
    store.DATA.mkdir(parents=True, exist_ok=True)
    tmp = SCHEDULE_FILE.with_name("recheck.json.tmp")
    tmp.write_text(json.dumps(schedule, ensure_ascii=False, indent=2))
    os.replace(tmp, SCHEDULE_FILE)


def epoch(dt_iso):
    """Unix time of an ISO datetime like 2026-11-27T16:33:04+00:00 (0 if unset)."""
    if not dt_iso:
        return 0.0
    try:
        return datetime.datetime.fromisoformat(dt_iso).timestamp()
    except ValueError:
        return 0.0


def iso(epoch_seconds):
    return datetime.datetime.fromtimestamp(epoch_seconds,
                                          datetime.timezone.utc).isoformat()


def watch(st, schedule, now=None):
    """Ensure every mirrored post inside the edit window has a schedule entry.

    Entries are derived from the publish time, so this is idempotent: calling it
    after a restart, or after a backfill pulled in history, adds nothing for
    posts that are already scheduled and nothing for posts too old to edit."""
    now = time.time() if now is None else now
    horizon = now - RECHECK_WINDOW
    added = 0
    for pid in st.ids_desc():                  # newest first
        published = epoch((st.get(pid) or {}).get("datetime", ""))
        if published < horizon:
            break                              # everything older is out of the window
        key = str(pid)
        if key in schedule:
            continue
        checks = checks_by(now, published)
        schedule[key] = {"due": next_due(published, checks),
                         "checks": checks, "misses": 0}
        added += 1
    return added


def prune(schedule, st, now=None):
    """Drop watched posts that aged out of the edit window or disappeared."""
    now = time.time() if now is None else now
    horizon = now - RECHECK_WINDOW
    for key in list(schedule):
        rec = st.get(int(key))
        if rec is None or epoch(rec.get("datetime", "")) < horizon:
            del schedule[key]
    return schedule


def due_ids(schedule, now=None):
    """Ids whose next check is overdue, most overdue first."""
    now = time.time() if now is None else now
    keys = [k for k, v in schedule.items()
            if not v.get("done") and v.get("due", 0) <= now]
    return sorted(keys, key=lambda k: (schedule[k].get("due", 0), int(k)))


def cluster(ids):
    """Group id strings into runs no wider than WINDOW_SPAN, so one
    t.me/s/<id> fetch (a ~15-post window) covers each group."""
    nums = sorted(int(i) for i in ids)
    groups, current = [], []
    for n in nums:
        if current and n - current[-1] > WINDOW_SPAN:
            groups.append(current)
            current = []
        current.append(n)
    if current:
        groups.append(current)
    return groups


# --------------------------------------------------------------------------
# the cycle
# --------------------------------------------------------------------------

def _pause():
    """Same spacing the sync loop keeps between t.me pages."""
    time.sleep(random.uniform(*PAGE_DELAY))


def run_cycle(st, fetch_page=scrape.fetch_post_html, now=None, sleep=_pause):
    """Re-check due posts for edits. `fetch_page(pid)` returns page HTML or None.

    `sleep` separates the requests of a multi-group cycle: without it a cycle
    with RECHECK_MAX_PAGES groups would hit t.me that many times back to back.

    Returns {"checked": n, "edited": [ids]} for logging and tests."""
    if not RECHECK_ENABLED:
        return {"checked": 0, "edited": []}

    now = time.time() if now is None else now
    schedule = prune(load_schedule(), st, now)
    watch(st, schedule, now)
    due = due_ids(schedule, now)
    if not due:
        save_schedule(schedule)
        return {"checked": 0, "edited": []}

    checked, edited = 0, []
    groups = cluster(due)[:RECHECK_MAX_PAGES]
    for n, group in enumerate(groups):
        if n:
            sleep()
        html = fetch_page(group[-1])          # one window covers the whole group
        if html is None:
            continue                          # network trouble: retry on a later cycle
        posts = {p["id"]: p for p in scrape.parse_posts(html)}
        for pid in group:
            entry = schedule.get(str(pid))
            if entry is None:
                continue
            post = posts.get(pid)
            if post is None:
                entry["misses"] = entry.get("misses", 0) + 1
                if entry["misses"] >= MAX_MISSES:
                    # Marked rather than deleted: watch() would otherwise
                    # re-add it every cycle and re-check a deleted post forever.
                    entry["done"] = True
                continue
            entry["misses"] = 0
            checked += 1
            new_hash = store.content_hash(post)
            published = epoch(post.get("datetime", "")) or now
            # A stale entry (mirror was down) must not re-check at first-check
            # cadence: trust the publish-anchored count when it is ahead.
            entry["checks"] = max(entry.get("checks", 0), checks_by(now, published))
            if new_hash == store.stored_content_hash(pid):
                entry["checks"] += 1
                entry["due"] = next_due(published, entry["checks"])
                continue
            # Content changed: re-persist. Unchanged media is reused, not
            # re-downloaded, so a text-only edit costs no media requests.
            rec = store.save_post(post, edited_at=iso(now))
            st.add(rec)
            edited.append(pid)
            # An edited post can be edited again; keep its original schedule so
            # the backoff does not restart and amplify request volume.
            entry["due"] = next_due(published, entry["checks"] + 1)

    save_schedule(schedule)
    return {"checked": checked, "edited": edited}
