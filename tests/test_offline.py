"""Offline end-to-end test: fake MBTA + TransitMatters data and a fake clock, real planning code.
Run: python3 tests/test_offline.py"""
import datetime as dt
import json
import os
import random
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from commute import app as appmod, model, util  # noqa: E402
from commute.history import Day  # noqa: E402

# ---------------- fake clock: today at a chosen time, advanced by sleep()
CLOCK = {"t": None}
real_now = util.now


def fake_now():
    return CLOCK["t"]


def set_clock(h, m):
    CLOCK["t"] = real_now().replace(hour=h, minute=m, second=0, microsecond=0)


def fake_sleep(sec):
    CLOCK["t"] += dt.timedelta(seconds=max(sec, 60))


util.now = appmod.now = fake_now
appmod.time.sleep = fake_sleep
rng = random.Random(1)


def fake_day(seed):
    r = random.Random(seed)
    buses, t = [], 8 * 60
    while t < 10.5 * 60:
        t += r.choice([5, 6, 7, 8, 12])
        buses.append((t, t + r.gauss(6.5, 1.2)))
    trains, t = [], 8 * 60
    while t < 10.5 * 60:
        t += r.choice([3, 4, 4, 5, 6, 10])
        trains.append((t, t + r.gauss(11, 0.8)))
    return Day(f"d{seed}", buses, trains)


DAYS = [fake_day(i) for i in range(30)]
c = {"walk_to_bus_stop": 2, "walk_to_station": 13, "walk_from_kendall": 4, "bus_to_platform": 2,
     "platform_to_bus": 2, "buffer": 2, "bus_ride_default": 6}
best, _ = model.plan_night(DAYS, c, 570, 490, 558, 0.8)
print(f"night replay: leave {util.hm(best.leave)}, {best.p_on_time:.0%} by 9:30, median {best.dur_med:.1f} min")
assert best.p_on_time >= 0.8


class FakeMBTA:
    n = 0

    def _t(self, m):
        return CLOCK["t"].replace(hour=0, minute=0, second=0) + dt.timedelta(minutes=m)

    def route_stops(self, route, d):
        return [{"id": "1234" if d else "x", "name": "W Broadway @ F St", "parent": None},
                {"id": "150" if d else "151", "name": "Broadway Station - Red Line", "parent": None}]

    def predictions(self, stop, route, direction):
        """A fixed, realistic timetable: 9 bus every ~7 min with per-trip lateness (one trip cancelled),
        Red every 4 min with one 11-min gap. Predictions only reach ~30 min ahead, like the real thing."""
        self.n += 1
        now_m = util.mins(CLOCK["t"])
        r = random.Random(7)
        out = []
        if route != "Red":
            off = 6.5 if stop in ("150", "151") else 0
            for k in range(200):
                m = 6 * 60 + 7 * k + r.choice([0, 0, 1, 2, 3])
                if now_m - 1 <= m <= now_m + 30:
                    trip = f"b{direction}-{k}"
                    if k % 23 == 5:
                        out.append({"trip": trip, "dep": None, "arr": None, "sched": self._t(m), "rel": "CANCELLED"})
                    else:
                        out.append({"trip": trip, "dep": self._t(m + off), "arr": self._t(m + off), "sched": None, "rel": None})
            return out
        t = 6 * 60
        while t < 23 * 60:
            t += 11 if int(t) % 97 < 4 else 4
            if now_m - 1 <= t <= now_m + 30:
                out.append({"trip": f"r{direction}-{t:.0f}", "dep": self._t(t),
                            "arr": self._t(t + (10 if stop == "place-brdwy" else 0)), "sched": None, "rel": None})
        return out

    def schedules(self, stop, route, direction, date, lo, hi):
        h, m = map(int, lo.split(":"))
        return [{"trip": f"s{i}", "time": self._t(h * 60 + m + 7 * i + 3)} for i in range(10)]

    def alerts(self, routes, at=None):
        return [{"id": "a1", "header": "Red Line: delays of about 10 minutes", "effect": "DELAY", "severity": 5,
                 "lifecycle": "NEW", "routes": ["Red"], "stops": []}]


class FakeHist:
    def morning_days(self, target):
        return DAYS

    def ride_samples(self, target, frm, to, *a, **k):
        mu = 5.5 if frm[0].startswith("9-") else 10.5
        return [rng.gauss(mu, 1) for _ in range(200)]

    def headway_median(self, *a, **k):
        return 4.5


tmp = tempfile.mkdtemp()
cfgtxt = open(os.path.join(ROOT, "config.example.toml")).read().replace(
    'status_topic = "FROM-KEYGEN"', 'status_topic = "status-test"')
open(f"{tmp}/config.toml", "w").write(cfgtxt)
os.chdir(tmp)
set_clock(21, 40)
a = appmod.App("config.toml", dry_run=True)
a.mbta, a.hist = FakeMBTA(), FakeHist()
a.setup()

print("\n----- schedule changes")
a.handle("tomorrow arrive 10 leave 6:15")
a.handle("friday off")
a.handle("today the bus driver was great")        # not a schedule command → must not change anything
tomorrow = CLOCK["t"].date() + dt.timedelta(days=1)
d = a.day(tomorrow)
assert d["arrive"] == 600 and d["leave_work"] == 18 * 60 + 15 and not d["off"], d
assert a.day(CLOCK["t"].date())["changed"] is False
a.handle("tomorrow default")
assert a.day(tomorrow)["arrive"] == 570

print("\n----- tick at 9:40pm → night plan")
a.tick()
assert a.state["plans"][str(tomorrow)]

print("\n----- tick next morning → watches every minute from an hour before leaving")
leave = a.state["plans"][str(tomorrow)]["leave"]
CLOCK["t"] = CLOCK["t"].replace(hour=0, minute=0) + dt.timedelta(days=1, minutes=leave - 60)
calls_before = a.mbta.n
a.tick()
polls = (a.mbta.n - calls_before) // 3
print(f"morning watch polled ~{polls} times (once a minute)")
assert a.state["morning_done"] == str(tomorrow) and polls >= 20

print("\n----- tick at 4:35pm → way-home watch")
set_clock(16, 35)
a.tick()
assert a.state["home_done"] == str(CLOCK["t"].date())
st = json.loads(a.ntfy.last_status)
print("status JSON bytes:", len(a.ntfy.last_status.encode()), "keys:", sorted(st))
assert len(a.ntfy.last_status.encode()) < 4096 and st["home"]["rows"]

print("\n----- trip log via the web app (with @ts) + learning")
c0 = a.c
base = CLOCK["t"].timestamp() - 2 * 86400
for day in range(3):
    t0 = base + day * 86400
    for step, m in [("left_home", 0), ("at_stop", 2.5), ("on_bus", 5), ("off_bus", 11), ("on_platform", 14.5),
                    ("on_train", 16), ("off_train", 27), ("at_dest", 33)]:
        a.handle(f"log:{step} @ts={t0 + m * 60:.0f}", t0 + 3600)   # sent late (no signal) but keeps tap time
c1 = a.c
for k in ("walk_to_bus_stop", "bus_to_platform", "walk_from_kendall"):
    print(f"  {k}: {c0[k]} → {c1[k]}")
assert c1["bus_to_platform"] > c0["bus_to_platform"]

print("\n----- web push subscription")
a.handle('subscribe {"endpoint":"https://web.push.apple.com/abc","keys":{"p256dh":"x","auth":"y"}}')
assert len(a.state["webpush_subs"]) == 1
print("\nALL OK")
shutil.rmtree(tmp, ignore_errors=True)
