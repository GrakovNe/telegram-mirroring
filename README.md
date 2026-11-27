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

Prompts for: **port**, **@channel**, **fetch politeness**, **how many posts** to
mirror, **edit re-checking**, and the **public URL** (needed for search engines).
Then it installs dependencies (python3, git), fetches the code from git, and
enables a systemd service `tgmirror-<channel>` that starts on boot and restarts
on failure.

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
| `RECHECK_ENABLED` | `1` | re-check recent posts for edits (`0` = off) |
| `RECHECK_WINDOW` | `86400` | how long a Telegram post stays editable, seconds |
| `RECHECK_FIRST_INTERVAL` | `900` | first re-check gap after publishing, seconds |
| `RECHECK_FACTOR` | `2.0` | how much the gap doubles after each unchanged check |
| `RECHECK_MAX_PAGES` | `4` | max t.me requests per re-check cycle |
| `PUBLIC_URL` | *(empty)* | absolute base URL for canonical/OG/sitemap/RSS |

## Architecture

```
config.py    runtime config, read once from environment variables.
scrape.py    the only module that talks to Telegram: fetch/parse the public
             web preview t.me/s/<channel> (no auth, no tokens).
store.py     local store: HTML<->Markdown, media download, post.md read/write,
             channel metadata, and a thread-safe in-memory index.
sync.py      background worker: periodic polling for new posts + incremental backfill.
recheck.py   re-fetches recent posts on a geometric backoff to catch edits.
render.py    server-side rendering: feed page, post pages, sitemap.xml, RSS,
             robots.txt, Open Graph and JSON-LD.
server.py    HTTP: serves server-rendered pages, /api/posts, /api/meta and
             /media/* only from the local store (HTTP/1.1 keep-alive).
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
  recheck.json               # edit-watch schedule {id: {due, checks, misses}}
```

`post.md` is the source of truth; the server renders `text_html` from its
Markdown body. The `data/` directory is generated at runtime and gitignored.

### Politeness (so Telegram won't ban the mirror)

- one lightweight request to `t.me` at a time, with a randomized gap between pages;
- new posts are polled once per `POLL_INTERVAL` (single request per cycle);
- history is backfilled a single page per cycle, in the background;
- media downloads are spaced out;
- incremental backoff on errors (retry delay grows linearly), a fixed 5-minute cool-down on HTTP 429;
- the re-checker batches due posts into id-clusters, because one
  `t.me/s/<channel>/<id>` response already contains a window of ~15 posts —
  a single request re-checks the whole cluster (at most `RECHECK_MAX_PAGES`
  per cycle, and it runs on the same thread as the sync loop).

### Catching edits

Telegram lets an author edit a post for roughly a day, and the web preview
silently starts serving the new text. `recheck.py` keeps a schedule of recent
posts and re-fetches them on a **geometric backoff anchored to the publish
time**: gaps of 15 min, 30 min, 1 h, 2 h, 4 h … until the post ages out of the
window. Deriving the schedule from the publish time (rather than "when we last
looked") makes it restart-safe — a mirror that was offline for hours resumes at
the right point instead of hammering Telegram at first-check cadence.

A change is detected by content fingerprint (`store.content_hash`), not by
Telegram's "edited" label, which disappears again after a while. When a post
changed, it is re-persisted in place — same folder, `edited_at` recorded in the
frontmatter — and media whose remote URL is unchanged is reused rather than
re-downloaded. Reuse applies only when the stored set is complete (`media_ok: 1`
in the frontmatter), so a post whose download once failed is retried instead of
staying broken. A post that stops appearing in its own window `MAX_MISSES` times
is treated as deleted and dropped.

### Search engines

The feed and every post are rendered server-side, so a crawler sees real text
without running JavaScript. Set `PUBLIC_URL` to the address the mirror is
reachable at (e.g. `https://mirror.example.com`) — it is what canonical links,
Open Graph tags, `sitemap.xml` and `RSS` are built from. Without it, absolute
links are derived from the request's `Host` header and `sitemap.xml` / `RSS`
return 404.

### Videos

Telegram's public web preview does not expose the video file itself, so the
mirror stores the poster (thumbnail) locally and links out to Telegram for
playback. Full video mirroring would require the Telegram API (MTProto).

## Tests

```bash
python3 -m unittest discover -s tests
```

Covers the Markdown round-trip, content fingerprints, re-saving an edited post
(media reuse, `edited_at`), the re-check backoff math and cycle, backfill
termination, and the server-rendered markup. No network access.

## Endpoints

- `GET /` — the page, with the first feed page already rendered in the HTML.
- `GET /post/<id>` — a single post as its own page (canonical URL, Open Graph,
  `SocialMediaPosting` JSON-LD). `404` if the post is not mirrored yet.
- `GET /sitemap.xml` — every mirrored post (`404` unless `PUBLIC_URL` is set).
- `GET /rss` — the 50 newest posts as RSS (`404` unless `PUBLIC_URL` is set).
- `GET /robots.txt` — allows everything except `/api/`, points at the sitemap.
- `GET /api/posts[?before=<id>]` — feed from the store (12 per page, `next_before` cursor).
- `GET /api/meta` — channel branding (title, description, url).
- `GET /api/status` — sync state (post count, cursor, `backfill_done`).
- `GET /media/<folder>/media/<file>` — a media file from disk.
- `GET /locales.json` — UI strings.

Data source: Telegram's public web preview. License: MIT.
