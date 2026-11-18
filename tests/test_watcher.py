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


def run(seed):
    tmp = tempfile.mkdtemp()
    cfg = open(os.path.join(ROOT, "config.example.toml")).read().replace('status_topic = "FROM-KEYGEN"', 'status_topic = "x"')
    open(f"{tmp}/config.toml", "w").write(cfg)
    cwd = os.getcwd()
    os.chdir(tmp)
    try:
        CLOCK["t"] = real_now().replace(hour=7, minute=50, second=0, microsecond=0)
        a = appmod.App("config.toml", dry_run=True)
        a.mbta, a.hist = JitterMBTA(seed), FakeHist()
        a.setup()
        sent = []
        a.notify.push = lambda title, msg, **k: sent.append((util.mins(CLOCK["t"]), msg))
        a.watch_morning()
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
    print("worst non-gap message count:", worst)
    assert worst <= 5, f"too chatty ({worst})"
    print("WATCHER OK")
