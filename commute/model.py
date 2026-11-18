"""The planning math. Pure functions over minutes-of-day so they're easy to test."""
import bisect
import random
from dataclasses import dataclass, field

from .history import Day
from .util import median, pct


def _first_ge(pairs, t):
    i = bisect.bisect_left(pairs, (t, -1e9))
    return pairs[i] if i < len(pairs) else None


# =====================================================================================
# 1) NIGHT BEFORE: replay "if I had left at L on each of the last N weekdays, when would I
#    have reached my desk?" using the real bus and train times from those days. Cancelled
#    buses, bunching, and Red Line gaps are all in the data, so no distribution guessing.
# =====================================================================================
def replay_once(day: Day, leave, c):
    at_stop = leave + c["walk_to_bus_stop"]
    bus = _first_ge(day.buses, at_stop)
    bus_ready = bus[1] + c["bus_to_platform"] if bus else 1e9
    walk_ready = leave + c["walk_to_station"]
    # At the stop you can see live predictions, so assume you pick whichever reaches the platform first.
    mode, ready = ("bus", bus_ready) if bus_ready <= walk_ready else ("walk", walk_ready)
    train = _first_ge(day.trains, ready)
    if not train:
        return None
    return {"arrive": train[1] + c["walk_from_kendall"], "mode": mode,
            "bus_at_stop": bus[0] if mode == "bus" else None, "train": train[0]}


@dataclass
class Candidate:
    leave: float
    n: int
    p_on_time: float
    dur_med: float
    dur_p90: float
    arr_med: float
    arr_p90: float
    p_walk: float
    bus_times: list = field(default_factory=list)


def plan_night(days, c, deadline, earliest_leave, latest_leave, confidence):
    cands = []
    L = earliest_leave
    while L <= latest_leave:
        res = [r for r in (replay_once(d, L, c) for d in days) if r]
        if len(res) >= 3:
            arr = [r["arrive"] for r in res]
            dur = [a - L for a in arr]
            cands.append(Candidate(
                leave=L, n=len(res), p_on_time=sum(a <= deadline for a in arr) / len(arr),
                dur_med=median(dur), dur_p90=pct(dur, 0.9), arr_med=median(arr), arr_p90=pct(arr, 0.9),
                p_walk=sum(r["mode"] == "walk" for r in res) / len(res),
                bus_times=[r["bus_at_stop"] for r in res if r["bus_at_stop"]]))
        L += 1
    if not cands:
        return None, []
    ok = [x for x in cands if x.p_on_time >= confidence]
    pool = ok or sorted(cands, key=lambda x: -x.p_on_time)[:1]
    # Fastest typical trip; a bad-day (p90) term so we don't pick a time that's fast only when lucky;
    # ties go to the later leave time (more sleep).
    best = min(pool, key=lambda x: (round(x.dur_med + 0.35 * x.dur_p90, 1), -x.leave))
    return best, cands


def synthetic_days(sched_bus_fst, train_days, c, n=40, rng=None):
    """Fallback when TransitMatters' bus data is lagging: tomorrow's 9 schedule at F St with
    realistic noise (lateness, ~5% dropped trips) + the real Red Line days."""
    rng = rng or random.Random(7)
    out = []
    for i in range(n):
        base = train_days[i % len(train_days)]
        buses = []
        for t in sched_bus_fst:
            if rng.random() < c.get("bus_cancel_rate", 0.05):
                continue
            at = t + max(-1.5, rng.gauss(1.0, 2.5))
            buses.append((at, at + max(4, rng.gauss(c["bus_ride_default"], 1.5))))
        out.append(Day(base.date, sorted(buses), base.trains))
    return out


# =====================================================================================
# 2) LIVE MORNING: from real-time predictions, what are the next good ways to go?
# =====================================================================================
@dataclass
class Option:
    mode: str              # "bus" or "walk"
    leave: float
    arrive: float
    train_dep: float
    bus_at_stop: float = None
    trip: str = None
    slack: float = 0.0     # minutes between reaching the platform and the train leaving
    tight: bool = False
    bus_arr: float = None  # bus reaches Broadway
    train_arr: float = None

    def legs(self, c):
        """[(kind, start, end)] for the timeline bar in the web app."""
        if self.mode == "bus":
            at_stop = self.leave + c["walk_to_bus_stop"]
            return [("walk", self.leave, at_stop), ("wait", at_stop, self.bus_at_stop),
                    ("bus", self.bus_at_stop, self.bus_arr), ("walk", self.bus_arr, self.bus_arr + c["bus_to_platform"]),
                    ("wait", self.bus_arr + c["bus_to_platform"], self.train_dep), ("train", self.train_dep, self.train_arr),
                    ("walk", self.train_arr, self.arrive)]
        at_plat = self.leave + c["walk_to_station"]
        return [("walk", self.leave, at_plat), ("wait", at_plat, self.train_dep),
                ("train", self.train_dep, self.train_arr), ("walk", self.train_arr, self.arrive)]

    @property
    def duration(self):
        return self.arrive - self.leave


def extend_trains(pred_deps, headway, until):
    """Predictions only reach ~20-30 min out; pad with the typical headway after that."""
    deps = sorted(pred_deps)
    t = deps[-1] if deps else None
    while t is not None and t < until:
        t += headway
        deps.append(t)
    return deps


def morning_options(now_m, buses, bway_arr, train_deps, red_ride, bus_ride, c, deadline, earliest=None):
    """buses: [{'t': min at F St, 'trip':...}], bway_arr: {trip: min at Broadway},
    train_deps: [min] NB departures from Broadway."""
    opts = []
    red = median(red_ride) or 11.0
    for b in buses:
        leave = b["t"] - c["walk_to_bus_stop"] - c["buffer"]
        tight = False
        if leave < now_m - 0.2:
            if b["t"] - c["walk_to_bus_stop"] < now_m:
                continue          # already gone for you
            tight, leave = True, now_m
        arr_b = bway_arr.get(b["trip"]) or b["t"] + (median(bus_ride) or c["bus_ride_default"])
        ready = arr_b + c["bus_to_platform"]
        i = bisect.bisect_left(train_deps, ready)
        if i == len(train_deps):
            continue
        dep = train_deps[i]
        opts.append(Option("bus", leave, dep + red + c["walk_from_kendall"], dep, b["t"], b["trip"],
                           slack=dep - ready, tight=tight, bus_arr=arr_b, train_arr=dep + red))
    for dep in train_deps:
        leave = dep - c["walk_to_station"] - c["buffer"]
        if leave >= now_m - 0.2:
            opts.append(Option("walk", leave, dep + red + c["walk_from_kendall"], dep, slack=c["buffer"],
                               train_arr=dep + red))
    # `earliest`: don't suggest leaving before your planned time just because the watch started early
    start = max(now_m, earliest if earliest is not None else now_m)
    if earliest is not None:   # a "run for it now" option only counts once your planned time has come
        opts = [o for o in opts if o.leave >= start - 0.2 and (not o.tight or now_m >= earliest - 0.2)]
    opts = [o for o in opts if o.leave <= start + 35 and o.arrive <= deadline + 0.01] or opts
    return opts


def pick_morning(opts):
    if not opts:
        return None, None
    # quickest door-to-door; a connection under 1 min counts as 2 min worse; prefer earlier on ties
    best = min(opts, key=lambda o: (o.duration + (2 if o.slack < 1 else 0) + (3 if o.tight else 0), o.leave))
    earliest = min(opts, key=lambda o: (o.arrive, o.leave))
    return best, earliest


# =====================================================================================
# 3) WAY HOME: for each Red Line train out of Kendall, simulate many times what happens at
#    Broadway given (a) how long Kendall->Broadway really takes, (b) how wrong the 9's
#    predictions/schedule usually are. Out of that: chance of making each bus, and when home.
# =====================================================================================
def simulate_home(now_m, trains, buses, red_ride, bus_ride, err, c, n=1500, rng=None):
    """trains: [{'dep': min at Kendall SB, 'trip', 'arr_bway': min or None}]
    buses:  [{'t': min at Broadway outbound, 'kind': 'pred'|'sched', 'label'}]"""
    rng = rng or random.Random()
    results = []
    for tr in trains:
        leave_office = tr["dep"] - c["desk_to_kendall"] - c["buffer"]
        if leave_office < now_m - 0.2:
            continue
        lead_tr = tr["dep"] - now_m
        homes, waits, outcome = [], [], {}
        for _ in range(n):
            if tr.get("arr_bway"):
                arr = tr["arr_bway"] + err.sample("red_sb_bway", lead_tr + 10)
            else:
                arr = tr["dep"] + err.sample("red_sb_knd", lead_tr) + (rng.choice(red_ride) if red_ride else 10.5)
            at_stop = arr + c["platform_to_bus"]
            walk_home = arr + c["station_to_home"]
            best_bus, best_lbl = None, None
            for b in buses:
                lead = b["t"] - now_m
                d = b["t"] + err.sample("bus_out_bway" if b["kind"] == "pred" else "bus_sched", lead)
                if d >= at_stop and (best_bus is None or d < best_bus):
                    best_bus, best_lbl = d, b["label"]
            bus_home = (best_bus + (rng.choice(bus_ride) if bus_ride else c["bus_ride_default"])
                        + c["stop_to_home"]) if best_bus is not None else 1e9
            if bus_home <= walk_home:
                homes.append(bus_home)
                waits.append(best_bus - at_stop)
                outcome[best_lbl] = outcome.get(best_lbl, 0) + 1
            else:
                homes.append(walk_home)
                outcome["walk"] = outcome.get("walk", 0) + 1
        results.append({
            "train": tr["dep"], "leave_office": leave_office,
            "home_med": median(homes), "home_p80": pct(homes, 0.8),
            "door_to_door": median(homes) - leave_office,
            "wait_med": median(waits) if waits else None,
            "outcomes": {k: v / n for k, v in sorted(outcome.items(), key=lambda kv: -kv[1])},
        })
    return results
