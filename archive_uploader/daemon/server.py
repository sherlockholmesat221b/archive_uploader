"""Zero-dependency HTTP server: JSON API + single-page dashboard."""
from __future__ import annotations

import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .scheduler import Scheduler

PAGE = Path(__file__).with_name("dashboard.html")


def make_server(sch: Scheduler, host="127.0.0.1", port=8765, token: str = ""):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        # ---- helpers
        def _send(self, obj, code=200, ctype="application/json"):
            body = obj if isinstance(obj, bytes) else json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _auth(self, qs) -> bool:
            if not token:
                return True
            return self.headers.get("X-Token") == token or qs.get("token", [""])[0] == token

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return {}

        # ---- routes
        def do_GET(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            if u.path in ("/", "/index.html"):
                return self._send(PAGE.read_bytes(), ctype="text/html; charset=utf-8")
            if not u.path.startswith("/api/"):
                return self._send({"error": "not found"}, 404)
            if not self._auth(qs):
                return self._send({"error": "unauthorized"}, 401)
            if u.path == "/api/state":
                return self._send(sch.snapshot())
            m = re.fullmatch(r"/api/job/(\d+)", u.path)
            if m:
                jid = int(m.group(1))
                return self._send({"job": sch.db.get_job(jid), "parts": sch.db.parts(jid),
                                   "children": sch.db.q(
                                       "SELECT id,kind,ref,title,status,priority FROM jobs "
                                       "WHERE parent_id=? ORDER BY id", (jid,))})
            if u.path == "/api/stats":
                stage = qs.get("stage", ["upload"])[0]
                rng = float(qs.get("range", ["86400"])[0])
                since = 0 if rng <= 0 else time.time() - rng
                span = rng if rng > 0 else 30 * 86400
                bucket = int(qs.get("bucket", [0])[0]) or max(int(sch.settings["tick_s"]), int(span / 100))
                return self._send({
                    "stage": stage, "bucket": bucket,
                    "series": sch.db.series(stage, since, bucket),
                    "hod": sch.db.cycle(stage, since, "hod"),
                    "dow": sch.db.cycle(stage, since, "dow"),
                    "totals": sch.db.totals(since)})
            if u.path == "/api/events":
                return self._send(sch.db.events(int(qs.get("limit", ["100"])[0])))
            if u.path == "/api/manifest":
                from .. import manifest
                return self._send(manifest.all_rows(int(qs.get("limit", ["500"])[0])))
            return self._send({"error": "not found"}, 404)

        def do_POST(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            if not self._auth(qs):
                return self._send({"error": "unauthorized"}, 401)
            b = self._body()
            if u.path == "/api/jobs":
                refs = b.get("refs") or []
                if isinstance(refs, str):
                    refs = refs.splitlines()
                opts = {k: b[k] for k in ("opus", "mega", "window", "release_type", "quality",
                                          "mega_account", "mega_link", "mega_root") if k in b}
                return self._send(sch.add(refs, int(b.get("priority", 2)), opts))
            m = re.fullmatch(r"/api/jobs/(\d+)/(priority|cancel|retry|remove)", u.path)
            if m:
                jid, act = int(m.group(1)), m.group(2)
                if act == "priority":
                    sch.set_priority(jid, int(b["priority"]))
                elif act == "cancel":
                    sch.cancel(jid)
                elif act == "retry":
                    sch.retry(jid)
                elif act == "remove":
                    return self._send({"ok": sch.remove(jid)})
                return self._send({"ok": True})
            if u.path == "/api/pause":
                sch.set_pause(b.get("lane", "all"), bool(b.get("on", True)))
                return self._send({"ok": True})
            if u.path == "/api/break":
                sch.set_break(b.get("lane", "upload"), float(b.get("seconds", 0)))
                return self._send({"ok": True})
            if u.path == "/api/settings":
                sch.update_settings(b)
                return self._send({"ok": True})
            return self._send({"error": "not found"}, 404)

    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    return srv
