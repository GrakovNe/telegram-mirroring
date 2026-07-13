#!/usr/bin/env python3
"""
server.py — serves the local mirror. The browser NEVER talks to Telegram:
posts come from the on-disk store (data/posts/*/post.md) and every image is
served from disk under /media/. A background worker (sync.py) keeps the store
fresh and gradually backfills history, politely.

Run:  python3 server.py     # listens on 0.0.0.0:8080
"""
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote
from pathlib import Path

import store
import sync
from config import PORT, CHANNEL

HERE = Path(__file__).resolve().parent
INDEX_HTML = HERE / "index.html"
LOCALES = HERE / "locales.json"
POSTS_DIR = store.POSTS.resolve()
PAGE_SIZE = 12

STORE = store.Store()


class Handler(BaseHTTPRequestHandler):
    # Persistent connections: a media-heavy feed page fires many /media requests,
    # so reuse the TCP connection. Safe because _send() always sets Content-Length.
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        p = urlparse(self.path)
        if p.path in ("/", "/index.html"):
            return self._send(200, INDEX_HTML.read_text("utf-8"),
                              "text/html; charset=utf-8")
        if p.path == "/locales.json":
            return self._send(200, LOCALES.read_text("utf-8"),
                              "application/json; charset=utf-8")
        if p.path == "/api/posts":
            return self._api_posts(p)
        if p.path == "/api/meta":
            meta = STORE.get_meta() or {
                "username": CHANNEL, "url": f"https://t.me/{CHANNEL}",
                "title": CHANNEL, "description": "",
            }
            return self._send(200, json.dumps(meta, ensure_ascii=False),
                              "application/json; charset=utf-8")
        if p.path == "/api/status":
            st = store.load_state()
            return self._send(200, json.dumps({
                "count": STORE.count(), "min_id": STORE.min_id(),
                "max_id": STORE.max_id(), "backfill_done": st.get("backfill_done", False),
            }, ensure_ascii=False), "application/json; charset=utf-8")
        if p.path.startswith("/media/"):
            return self._media(p.path)
        return self._send(404, "Not found", "text/plain; charset=utf-8")

    # HEAD shares GET's routing; _send() omits the body when command == "HEAD".
    do_HEAD = do_GET

    def _api_posts(self, p):
        qs = parse_qs(p.query)
        before = qs.get("before", [None])[0]
        try:
            before = int(before) if before else None
        except ValueError:
            before = None
        data = STORE.page(before, PAGE_SIZE)
        self._send(200, json.dumps(data, ensure_ascii=False),
                   "application/json; charset=utf-8")

    def _media(self, path):
        # /media/<folder>/media/<file> — served straight from disk, sandboxed.
        rel = unquote(path[len("/media/"):])
        target = (POSTS_DIR / rel).resolve()
        if not str(target).startswith(str(POSTS_DIR) + "/") or not target.is_file():
            return self._send(404, "Not found", "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype,
                   extra={"Cache-Control": "public, max-age=604800"})

    def log_message(self, *args):
        pass


def main():
    sync.start(STORE)   # background sync worker (non-blocking)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Telegram mirror (@{sync.scrape.CHANNEL}) on http://0.0.0.0:{PORT}  "
          f"({STORE.count()} posts cached)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
