"""Learns how wrong MBTA predictions are *at your stops*.

Every poll we log (when, which stop, which trip, predicted time). When a trip's prediction
disappears, its last prediction made <=90s before the event is taken as the actual time.
Error = actual - predicted, bucketed by how far ahead the prediction was.
This is exactly the "it said 4 min but left in 2" problem on the way home.
"""
import random
import sqlite3
import time
from collections import defaultdict

BUCKETS = [(0, 3), (3, 6), (6, 10), (10, 15), (15, 30)]

# Used until we have ~12 real observations in a bucket. (mean, sd) in minutes as a function of lead.
DEFAULTS = {
    "bus_in_fst":   lambda lead: (0.0, 0.6 + 0.15 * lead),
    "bus_out_bway": lambda lead: (0.2, 0.6 + 0.15 * lead),
    "bus_sched":    lambda lead: (1.5, 3.0),          # no prediction yet, only the schedule
    "red":          lambda lead: (0.2, 0.3 + 0.08 * lead),
}


def _bucket(lead):
    for i, (a, b) in enumerate(BUCKETS):
        if a <= lead < b:
            return i
    return len(BUCKETS) - 1


class Recorder:
    def __init__(self, path="commute.sqlite"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("""create table if not exists obs(
            ts real, key text, trip text, pred real, primary key(ts, key, trip))""")
        self.db.commit()

    def log(self, key, preds, t=None):
        """preds: list of dicts with 'trip' and an aware datetime under 'dep' or 'arr'."""
        t = t or time.time()
        rows = []
        for p in preds:
            when = p.get("dep") or p.get("arr")
            if when and p.get("rel") not in ("CANCELLED", "SKIPPED"):
                rows.append((t, key, p["trip"], when.timestamp()))
        self.db.executemany("insert or ignore into obs values(?,?,?,?)", rows)
        self.db.commit()

    def errors(self, max_age_days=60):
        """{key: {bucket: [errors in minutes]}}"""
        since = time.time() - max_age_days * 86400
        cur = self.db.execute("select key, trip, ts, pred from obs where ts > ? order by key, trip, ts", (since,))
        by_trip = defaultdict(list)
        for key, trip, ts, pred in cur:
            by_trip[(key, trip, int(pred // 86400))].append((ts, pred))
        out = defaultdict(lambda: defaultdict(list))
        for (key, _, _), seq in by_trip.items():
            last_ts, last_pred = seq[-1]
            if last_pred - last_ts > 90:      # never saw it arrive; can't score it
                continue
            actual = last_pred
            for ts, pred in seq[:-1]:
                lead = (pred - ts) / 60
                if lead >= 0:
                    out[key][_bucket(lead)].append((actual - pred) / 60)
        return out


class ErrorModel:
    def __init__(self, recorder=None, rng=None):
        self.rng = rng or random.Random()
        self.data = recorder.errors() if recorder else {}

    def sample(self, key, lead):
        family = key if key in DEFAULTS else ("red" if key.startswith("red") else "bus_out_bway")
        xs = self.data.get(key, {}).get(_bucket(max(lead, 0)), [])
        if len(xs) >= 12:
            return self.rng.choice(xs)
        mu, sd = DEFAULTS[family](max(lead, 0))
        return self.rng.gauss(mu, sd)

    def summary(self):
        lines = []
        for key, b in self.data.items():
            for i, xs in sorted(b.items()):
                if len(xs) >= 5:
                    xs = sorted(xs)
                    lo, hi = BUCKETS[i]
                    lines.append(f"{key} {lo}-{hi} min out: n={len(xs)} "
                                 f"median {xs[len(xs)//2]:+.1f}, 10–90% {xs[len(xs)//10]:+.1f}..{xs[9*len(xs)//10]:+.1f}")
        return "\n".join(lines) or "No prediction-accuracy data yet (it builds up as the watcher runs)."
