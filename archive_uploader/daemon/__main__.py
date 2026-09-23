"""
python -m archive_uploader.daemon serve [--demo] [--port 8765] [--host 127.0.0.1] [--token T]
python -m archive_uploader.daemon add REF... [-p now|high|normal|low] [--mega] [--no-opus] [--window N]
python -m archive_uploader.daemon now REF...          # shortcut for add -p now
python -m archive_uploader.daemon status
python -m archive_uploader.daemon pause|resume [lane]
python -m archive_uploader.daemon break MINUTES [lane]   # default lane: upload
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

PRIO = {"now": 0, "high": 1, "normal": 2, "low": 3}


def _call(a, path, body=None):
    url = f"http://{a.host}:{a.port}/api{path}"
    req = urllib.request.Request(url, method="GET" if body is None else "POST",
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-Token": a.token or ""})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    except OSError as e:
        sys.exit(f"cannot reach the daemon at {a.host}:{a.port} ({e}). Is `serve` running?")


def serve(a):
    from .db import DB
    from .scheduler import Scheduler
    from .server import make_server
    if a.demo:
        from .fake import FakeStages
        stages = FakeStages(speed=a.speed, parts_per_album=lambda ref: 1 + (sum(map(ord, ref)) % 4))
    else:
        from .stages import QobuzStages
        stages = QobuzStages()
    sch = Scheduler(DB(a.db), stages, {"stage_path": os.path.dirname(os.path.abspath(a.db)) or "."})
    import archive_uploader  # noqa: F401 -- force full init before worker threads start
    sch.start()
    srv = make_server(sch, a.host, a.port, a.token or "")
    print(f"dashboard: http://{a.host}:{a.port}/" + (f"?token={a.token}" if a.token else ""), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("stopping (running tasks finish their current unit)…")
        sch.stop()


def main(argv=None):
    p = argparse.ArgumentParser(prog="archive_uploader.daemon")
    common = argparse.ArgumentParser(add_help=False)      # accepted before OR after the subcommand
    for par, sup in ((p, False), (common, True)):
        d = (lambda v: argparse.SUPPRESS) if sup else (lambda v: v)
        par.add_argument("--host", default=d("127.0.0.1"))
        par.add_argument("--port", type=int, default=d(8765))
        par.add_argument("--token", default=d(os.environ.get("AU_TOKEN", "")))
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", parents=[common])
    s.add_argument("--db", default="queue.db")
    s.add_argument("--demo", action="store_true", help="simulated stages (no Qobuz/IA)")
    s.add_argument("--speed", type=float, default=1.0)

    for name in ("add", "now"):
        s = sub.add_parser(name, parents=[common])
        s.add_argument("refs", nargs="+")
        s.add_argument("-p", "--priority", default="now" if name == "now" else "normal", choices=PRIO)
        s.add_argument("--mega", action="store_true")
        s.add_argument("--no-opus", action="store_true")
        s.add_argument("--window", type=int)
    sub.add_parser("status", parents=[common])
    for name in ("retry", "cancel", "remove"):
        s_ = sub.add_parser(name, parents=[common])
        s_.add_argument("job_id", type=int)
    for name in ("pause", "resume"):
        s = sub.add_parser(name, parents=[common])
        s.add_argument("lane", nargs="?", default="all")
    s = sub.add_parser("break", parents=[common])
    s.add_argument("minutes", type=float)
    s.add_argument("lane", nargs="?", default="upload")

    a = p.parse_args(argv)
    if a.cmd == "serve":
        return serve(a)
    if a.cmd in ("add", "now"):
        body = {"refs": a.refs, "priority": PRIO[a.priority], "mega": a.mega, "opus": not a.no_opus}
        if a.window:
            body["window"] = a.window
        for r in _call(a, "/jobs", body):
            print(("dup " if r.get("duplicate") else "ok  ") + f"#{r['id']} {r.get('kind', '')} {r['ref']}")
    elif a.cmd == "status":
        st = _call(a, "/state")
        for l, L in st["lanes"].items():
            print(f"{l:9} {L['mbps_1m']:6.1f} MB/s  running {len(L['running'])}/{L['cap']}"
                  + ("  PAUSED" if L["paused"] else "")
                  + (f"  break {int(L['break_until'] - st['now'])}s" if L["break_until"] > st["now"] else ""))
        for j in st["jobs"]:
            if j["status"] in ("done", "cancelled"):
                continue
            print(f"#{j['id']:<4} p{j['priority']} {j['status']:10} {j['kind']:6} "
                  f"{(j['title'] or j['ref'])[:44]:44} {j['parts_done']}/{j['parts_total']} parts  {j['where']}")
    elif a.cmd in ("retry", "cancel", "remove"):
        r = _call(a, f"/jobs/{a.job_id}/{a.cmd}", {})
        print(r)
    elif a.cmd in ("pause", "resume"):
        _call(a, "/pause", {"lane": a.lane, "on": a.cmd == "pause"})
    elif a.cmd == "break":
        _call(a, "/break", {"lane": a.lane, "seconds": a.minutes * 60})


if __name__ == "__main__":
    main()
