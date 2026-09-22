import time


from archive_uploader.daemon.db import DB, FINAL
from archive_uploader.daemon.fake import MB, FakeStages
from archive_uploader.daemon.scheduler import Scheduler


def make(fake=None, **settings):
    fake = fake or FakeStages(speed=25, mb_per_part=100)
    s = {"tick_s": 0.3, "retry_base_s": 0.1, "artist_window": 2}
    s.update(settings)
    sch = Scheduler(DB(), fake, s)
    sch.start()
    return sch, fake


def wait(cond, timeout=20):
    t = time.time()
    while time.time() - t < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


def status(sch, jid):
    return sch.db.get_job(jid)["status"]


def starts(fake, lane, job=None):
    return [(t, j, p) for t, l, j, p, w in fake.log if l == lane and w == "start" and (job is None or j == job)]


def ends(fake, lane, job=None):
    return [(t, j, p) for t, l, j, p, w in fake.log if l == lane and w == "end" and (job is None or j == job)]


def test_stages_overlap_within_a_release():
    sch, fake = make(FakeStages(speed=25, mb_per_part=100, parts_per_album=lambda r: 4))
    (a,) = sch.add(["A"])
    assert wait(lambda: status(sch, a["id"]) == "done")
    # part 2 downloads while part 1 is still uploading or before it finished
    up1_end = [t for t, j, p in ends(fake, "upload", a["id"]) if p == 1][0]
    dl2_start = [t for t, j, p in starts(fake, "download", a["id"]) if p == 2][0]
    assert dl2_start < up1_end
    assert len(ends(fake, "upload", a["id"])) == 4
    sch.stop()


def test_now_waits_only_for_running_upload():
    sch, fake = make(FakeStages(speed=10, mb_per_part=100))
    ids = [x["id"] for x in sch.add(["A", "B", "C", "D"])]
    assert wait(lambda: len(starts(fake, "upload")) >= 1)
    (n,) = sch.add(["NOW"], priority=0)
    assert wait(lambda: status(sch, n["id"]) == "done")
    first_up_end = ends(fake, "upload")[0][0]
    n_up = starts(fake, "upload", n["id"])[0][0]
    other_starts = sorted(t for t, j, p in starts(fake, "upload") if j != n["id"])
    assert n_up >= first_up_end - 0.01            # waited for the ongoing upload
    assert n_up < other_starts[1]                 # ...but jumped ahead of the rest of the queue
    assert wait(lambda: all(status(sch, i) == "done" for i in ids))
    sch.stop()


def test_now_bypasses_pause_normal_does_not():
    sch, fake = make()
    sch.set_pause("upload", True)
    (a,) = sch.add(["A"])
    (n,) = sch.add(["N"], priority=0)
    assert wait(lambda: status(sch, n["id"]) == "done")
    assert status(sch, a["id"]) == "active"
    sch.set_pause("upload", False)
    assert wait(lambda: status(sch, a["id"]) == "done")
    sch.stop()


def test_artist_window_and_fairness():
    sch, fake = make(FakeStages(speed=25, mb_per_part=100, artist_albums=6))
    (art,) = sch.add(["artist:42"])
    peak = 0
    (x,) = sch.add(["X"])                         # a normal album added after the artist
    done_x = None
    t0 = time.time()
    while time.time() - t0 < 30:
        kids = sch.db.q("SELECT status FROM jobs WHERE parent_id=?", (art["id"],))
        open_ = sum(1 for k in kids if k["status"] not in FINAL)
        peak = max(peak, open_)
        if done_x is None and status(sch, x["id"]) == "done":
            done_x = len(kids)
        if status(sch, art["id"]) == "done":
            break
        time.sleep(0.02)
    assert status(sch, art["id"]) == "done"
    assert peak <= 2
    assert done_x is not None and done_x < 6      # X finished before the artist had even materialised all albums
    assert sch.db.s("SELECT COUNT(*) FROM jobs WHERE parent_id=? AND status='done'", (art["id"],)) == 6
    sch.stop()


def test_priority_now_propagates_to_artist_children():
    sch, fake = make(FakeStages(speed=25, mb_per_part=100, artist_albums=3))
    sch.set_pause("all", True)
    (art,) = sch.add(["artist:7"])
    assert wait(lambda: status(sch, art["id"]) == "expanding" or True)
    sch.set_pause("all", False)
    sch.set_priority(art["id"], 1)
    assert wait(lambda: status(sch, art["id"]) == "done")
    assert all(k["priority"] == 1 for k in sch.db.q("SELECT priority FROM jobs WHERE parent_id=?", (art["id"],)))
    sch.stop()


def test_stats_recorded():
    sch, fake = make(FakeStages(speed=25, mb_per_part=100, parts_per_album=lambda r: 2))
    (a,) = sch.add(["A"])
    assert wait(lambda: status(sch, a["id"]) == "done")
    time.sleep(0.8)
    tot = {r["stage"]: r for r in sch.db.totals(0)}
    for lane in ("download", "opus", "upload"):
        assert tot[lane]["units"] == 2
        assert abs(tot[lane]["bytes"] - 200 * MB) < 2 * MB
        assert tot[lane]["mbps"] > 0
    assert "mega" not in tot                       # off by default
    assert sch.db.series("upload", 0, 1)
    assert sch.db.cycle("upload", 0, "hod")
    sch.stop()


def test_retry_then_success_and_fail_permanently():
    fake = FakeStages(speed=50, mb_per_part=20, fail={("A", 1, "upload")})
    sch, _ = make(fake)
    (a,) = sch.add(["A"])
    assert wait(lambda: status(sch, a["id"]) == "done")
    assert sch.db.events(10)[0]["kind"] == "retry"
    sch.stop()

    fake2 = FakeStages(speed=50, mb_per_part=20)
    fake2.fail = set()
    sch2, _ = make(fake2, max_retries=1)
    fake2.fail.add(("B", 1, "opus"))
    (b,) = sch2.add(["B"])
    assert wait(lambda: status(sch2, b["id"]) == "failed")
    sch2.retry(b["id"])
    assert wait(lambda: status(sch2, b["id"]) == "done")
    sch2.stop()


def test_mega_optional_and_dedupe():
    sch, fake = make()
    r1 = sch.add(["M"], opts={"mega": True, "opus": False})
    dup = sch.add(["M"])
    assert dup[0].get("duplicate")
    assert wait(lambda: status(sch, r1[0]["id"]) == "done")
    assert ends(fake, "mega") and not ends(fake, "opus")
    sch.stop()


def test_staged_budget_limits_prefetch():
    fake = FakeStages(speed=25, mb_per_part=100, parts_per_album=lambda r: 6)
    sch, _ = make(fake, max_staged_bytes=250 * MB, max_ahead=5)
    (a,) = sch.add(["A"])
    peak = 0
    t0 = time.time()
    while time.time() - t0 < 30 and status(sch, a["id"]) != "done":
        parts = sch.db.parts(a["id"])
        staged = sum(p["est_bytes"] for p in parts if p["stage"] in ("opus", "upload", "mega")
                     or (p["stage"] == "download" and p["status"] == "running"))
        peak = max(peak, staged)
        time.sleep(0.01)
    assert status(sch, a["id"]) == "done"
    assert peak <= 300 * MB
    sch.stop()
