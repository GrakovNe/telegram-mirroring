# telegram-mirroring

A self-hosted **web mirror of any public Telegram channel** — read its posts in
a browser instead of Telegram. A background worker fetches the public web preview
at a rate-limited pace and stores posts as local Markdown + downloaded media; an
HTTP server serves the feed and media from that local store, so rendering a post
never contacts Telegram. (Clicking a video or a post's date opens that post on Telegram.)

The UI is channel-agnostic (title, description and links come from the channel
itself) and localized in **English / Russian** based on the browser language.

## Install as a service (systemd)

```bash
sudo ./install.sh
```

Prompts for: **port**, **@channel**, **fetch politeness**, and **how many posts**
to mirror. Then it installs dependencies (python3, git), fetches the code from
git, and enables a systemd service `tgmirror-<channel>` that starts on boot and
restarts on failure.

Remove it with:

```bash
sudo ./uninstall.sh <channel>
```

## Run manually

```bash
TG_CHANNEL=<channel> PORT=8080 python3 server.py
```

Open `http://<host>:8080`. Pure Python standard library — no dependencies.

## Configuration (environment, read by `config.py`)

Defaults use conservative request intervals and mirror all history.

| Variable | Default | Meaning |
|---|---|---|
| `TG_CHANNEL` | `telegram` | channel to mirror (without `@`) |
| `PORT` | `8080` | HTTP port |
| `POLL_INTERVAL` | `90` | seconds between "any new posts?" checks |
| `PAGE_DELAY_MIN` / `MAX` | `8` / `15` | delay between t.me pages, seconds |
| `MEDIA_DELAY_MIN` / `MAX` | `1.0` / `2.5` | delay between media downloads, seconds |
| `BACKFILL_PAGES_PER_CYCLE` | `1` | history pages fetched per cycle |
| `MAX_POSTS` | `0` | how many posts to mirror (`0` = all history) |

## Architecture

```
config.py    runtime config, read once from environment variables.
scrape.py    the only module that talks to Telegram: fetch/parse the public
             web preview t.me/s/<channel> (no auth, no tokens).
store.py     local store: HTML<->Markdown, media download, post.md read/write,
             channel metadata, and a thread-safe in-memory index.
sync.py      background worker: periodic polling for new posts + incremental backfill.
server.py    HTTP: serves the page, /api/posts, /api/meta and /media/* only
             from the local store (HTTP/1.1 keep-alive).
index.html   frontend (inline CSS+JS).
locales.json UI strings for en / ru.
```

### On-disk store

```
data/
  posts/
    2026-07-09__793/         # one folder per post: YYYY-MM-DD__id (ASCII only)
      post.md                # frontmatter + Markdown body
      media/01.jpg …          # downloaded photos / video posters / preview image
  state.json                 # sync cursor {min_id, max_id, backfill_done}
  meta.json                  # cached channel branding (title, description)
```

`post.md` is the source of truth; the server renders `text_html` from its
Markdown body. The `data/` directory is generated at runtime and gitignored.

### Politeness (so Telegram won't ban the mirror)

- one lightweight request to `t.me` at a time, with a randomized gap between pages;
- new posts are polled once per `POLL_INTERVAL` (single request per cycle);
- history is backfilled a single page per cycle, in the background;
- media downloads are spaced out;
- incremental backoff on errors (retry delay grows linearly), a fixed 5-minute cool-down on HTTP 429.

### Videos

Telegram's public web preview does not expose the video file itself, so the
mirror stores the poster (thumbnail) locally and links out to Telegram for
playback. Full video mirroring would require the Telegram API (MTProto).

## Endpoints

- `GET /` — the page.
- `GET /api/posts[?before=<id>]` — feed from the store (12 per page, `next_before` cursor).
- `GET /api/meta` — channel branding (title, description, url).
- `GET /api/status` — sync state (post count, cursor, `backfill_done`).
- `GET /media/<folder>/media/<file>` — a media file from disk.
- `GET /locales.json` — UI strings.

Data source: Telegram's public web preview. License: MIT.
