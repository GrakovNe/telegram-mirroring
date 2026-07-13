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
