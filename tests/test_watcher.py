"""Morning watcher stability: predictions jitter every poll; the watcher must not flip-flop.
Run: python3 tests/test_watcher.py"""
import datetime as dt
import os
import random
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from commute import app as appmod, util  # noqa: E402

CLOCK = {"t": None}
real_now = util.now
util.now = appmod.now = lambda: CLOCK["t"]
appmod.time.sleep = lambda s: CLOCK.__setitem__("t", CLOCK["t"] + dt.timedelta(seconds=max(s, 60)))


class JitterMBTA:
    """Buses ~every 7 min, Red every ~4.5 min. Each poll re-draws every prediction with noise (sd `sd` min)."""

    def __init__(self, seed, sd=1.0):
        self.r, self.sd, self.n = random.Random(seed), sd, 0
        self.drift = {}

    def _t(self, m):
        return CLOCK["t"].replace(hour=0, minute=0, second=0) + dt.timedelta(minutes=m)

    def route_stops(self, route, d):
        return [{"id": "1234" if d else "x", "name": "W Broadway @ F St", "parent": None},
                {"id": "150" if d else "151", "name": "Broadway Station - Red Line", "parent": None}]

    def predictions(self, stop, route, direction):
        self.n += 1
        now_m = util.mins(CLOCK["t"])
        out = []
        if route != "Red":
            for k in range(120):
                m = 6 * 60 + 7 * k
                if now_m - 1 <= m + 8 and m <= now_m + 30:
                    d = self.drift.setdefault(k, self.r.gauss(1, 2))
                    self.drift[k] = d + self.r.gauss(0, 0.6)
                    off = 6.5 if stop in ("150", "151") else 0
                    t = m + off + self.drift[k]
                    if t >= now_m - 1:
                        out.append({"trip": f"b{k}", "dep": self._t(t), "arr": self._t(t), "sched": None, "rel": None})
            return out
        t = 6 * 60
        while t < 23 * 60:
            t += 4.5
            if now_m - 1 <= t <= now_m + 30:
                j = t + self.r.gauss(0, self.sd)
                out.append({"trip": f"r{t:.0f}", "dep": self._t(j), "arr": self._t(j + 1), "sched": None, "rel": None})
        return out

    def schedules(self, *a):
        return []

    def alerts(self, routes, at=None):
        return []


class FakeHist:
    def morning_days(self, target):
        return []

    def ride_samples(self, target, frm, to, *a, **k):
        return [(5.5 if frm[0].startswith("9-") else 10.5) + random.Random(2).gauss(0, 1) for _ in range(50)]

    def headway_median(self, *a, **k):
        return 4.5


def run(seed, asap=None, start=(7, 50)):
    tmp = tempfile.mkdtemp()
    cfg = open(os.path.join(ROOT, "config.example.toml")).read().replace('status_topic = "FROM-KEYGEN"', 'status_topic = "x"')
    open(f"{tmp}/config.toml", "w").write(cfg)
    cwd = os.getcwd()
    os.chdir(tmp)
    try:
        CLOCK["t"] = real_now().replace(hour=start[0], minute=start[1], second=0, microsecond=0)
        a = appmod.App("config.toml", dry_run=True)
        a.mbta, a.hist = JitterMBTA(seed), FakeHist()
        a.setup()
        sent = []
        a.notify.push = lambda title, msg, **k: sent.append((util.mins(CLOCK["t"]), msg))
        if asap:
            a.handle(f"asap {asap}")
        (a.watch_home if asap == "home" else a.watch_morning)()
        return sent
    finally:
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    worst = 0
    for seed in range(8):
        sent = run(seed)
        changes = [m for _, m in sent if m.startswith("🔄") or "\n🔄" in m]
        leave_in = [m for _, m in sent if "⏰" in m]
        leave_now = [m for _, m in sent if "Leave now" in m]
        print(f"seed {seed}: {len(sent)} messages, {len(changes)} changes, {len(leave_in)} 'leave in', {len(leave_now)} 'leave now'")
        if seed == 0:
            for t, m in sent:
                print(f"   {util.hm(t)}  {m.splitlines()[0][:110]}")
        plan_msgs = [m for _, m in sent if not m.startswith("🚇")]
        worst = max(worst, len(plan_msgs))
        assert len(changes) <= 1 and len(leave_in) <= 1, "flip-flopping or a stale reminder"
        assert len(leave_now) == 1, "must say 'leave now' exactly once"
    # "I want to leave ASAP": mid-morning press, well before the planned leave time
    for seed in range(4):
        sent = run(seed, asap="morning", start=(8, 5))
        first_t, first = sent[0]
        now_leave = [(t, m) for t, m in sent if "Leave now" in m]
        assert "⚡ Leaving ASAP" in first, first
        assert now_leave and now_leave[0][0] <= 8 * 60 + 5 + 12, f"ASAP should leave within ~10 min: {sent}"
        assert len(now_leave) == 1
    print("ASAP morning OK")
    for seed in range(4):
        sent = run(seed, asap="home", start=(14, 0))
        first_t, first = sent[0]
        assert "Way home:" in first, first
        now_leave = [(t, m) for t, m in sent if "Leave your desk now" in m]
        assert now_leave and now_leave[0][0] <= 14 * 60 + 12, f"ASAP home should leave within ~10 min: {sent}"
    print("ASAP home OK")
    print("worst non-gap message count:", worst)
    assert worst <= 5, f"too chatty ({worst})"
    print("WATCHER OK")
