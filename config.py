#!/usr/bin/env python3
"""
config.py — runtime configuration, read once from environment variables.

install.sh writes these into the systemd unit. Defaults are deliberately
"very polite, mirror everything" so running with no env set is safe.
"""
import os


def _i(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return int(default)


def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


# Which public channel to mirror (leading '@' optional). Set via TG_CHANNEL;
# the default is only a neutral fallback for a bare `python3 server.py` run.
CHANNEL = os.environ.get("TG_CHANNEL", "telegram").lstrip("@").strip() or "telegram"

# HTTP port the server listens on.
PORT = _i("PORT", 8080)

# --- fetch politeness (all delays in seconds) --------------------------------
POLL_INTERVAL = _i("POLL_INTERVAL", 90)                 # between "any new posts?" checks
PAGE_DELAY = (_f("PAGE_DELAY_MIN", 8), _f("PAGE_DELAY_MAX", 15))    # between t.me pages
MEDIA_DELAY = (_f("MEDIA_DELAY_MIN", 1.0), _f("MEDIA_DELAY_MAX", 2.5))  # between downloads
BACKFILL_PAGES_PER_CYCLE = _i("BACKFILL_PAGES_PER_CYCLE", 1)        # history pages per cycle

# How many posts to mirror in total. 0 = all history (default).
MAX_POSTS = _i("MAX_POSTS", 0)

# --- edit re-check (see recheck.py) ------------------------------------------
# Telegram posts stay editable after publishing and the web preview silently
# reflects edits, so recent posts are re-fetched on a geometric backoff:
# every RECHECK_FIRST_INTERVAL seconds at first, interval *= RECHECK_FACTOR
# after each unchanged check, until the post ages out of the edit window.
RECHECK_ENABLED = _i("RECHECK_ENABLED", 1)
RECHECK_WINDOW = _i("RECHECK_WINDOW", 86400)              # how long posts stay editable
RECHECK_FIRST_INTERVAL = _f("RECHECK_FIRST_INTERVAL", 900)  # first gap after publishing
RECHECK_FACTOR = _f("RECHECK_FACTOR", 2.0)                # geometric growth of the gap
RECHECK_MAX_PAGES = _i("RECHECK_MAX_PAGES", 4)            # t.me requests per re-check cycle

# Absolute public base URL (e.g. https://mirror.example.com) for canonical links,
# OG tags and sitemap.xml. Empty -> derive from the request's Host header.
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
