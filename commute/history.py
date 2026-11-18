"""Historical trip data from the TransitMatters Data Dashboard API (same backend as
github.com/transitmatters/t-performance-dash). Every response is per-trip:
dep_dt at the origin stop, arr_dt at the destination stop, local wall-clock time.

Stop IDs (from the dashboard's own constants):
  Red Line platforms: Broadway NB 70082, Kendall NB 70072, Kendall SB 70071, Broadway SB 70081
  Route 9 timepoints: Perkins Sq (Dorchester St, 1 block from F St) inbound 9-1-45,
                      outbound 9-0-21/9-0-157; Broadway Station inbound 9-1-150, outbound 9-0-150/9-0-151
"""
import datetime as dt
import json
import pathlib
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .util import median


def _local_min(s):
    d = dt.datetime.fromisoformat(s)
    return d.hour * 60 + d.minute + d.second / 60


@dataclass
class Day:
    date: str
    buses: list = field(default_factory=list)   # morning: (time at F St, arrival at Broadway)
    trains: list = field(default_factory=list)  # morning: (dep Broadway NB, arr Kendall)


class History:
    def __init__(self, cfg, cache_dir="cache"):
        self.cfg = cfg
        self.api = cfg["history"]["tm_api"].rstrip("/")
        self.cache = pathlib.Path(cache_dir)
        self.cache.mkdir(exist_ok=True)

    # ---------- raw fetch with on-disk cache ----------
    def travel_times(self, date: dt.date, frm: list, to: list):
        key = f"tt_{date}_{'+'.join(frm)}_{'+'.join(to)}.json"
        path = self.cache / key
        if path.exists():
            return json.loads(path.read_text())
        q = urllib.parse.urlencode([("from_stop", s) for s in frm] + [("to_stop", s) for s in to])
        url = f"{self.api}/api/traveltimes/{date.isoformat()}?{q}"
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                data = json.load(r)
        except Exception as e:  # network hiccup or no data for that day
            print(f"[history] {url} failed: {e}")
            return []
        rows = sorted((_local_min(e["dep_dt"]), _local_min(e["arr_dt"])) for e in data)
        # TM's bus data can lag by weeks; don't cache an empty recent day forever
        if rows or (dt.date.today() - date).days > 45:
            path.write_text(json.dumps(rows))
        return rows

    def weekdays_before(self, end: dt.date, n: int):
        days, d = [], end - dt.timedelta(days=1)
        workdays = set(self.cfg["prefs"].get("workdays", [0, 1, 2, 3, 4]))
        while len(days) < n:
            if d.weekday() in workdays:
                days.append(d)
            d -= dt.timedelta(days=1)
        return days

    # ---------- morning ----------
    def morning_days(self, target: dt.date):
        h = self.cfg["history"]
        off = self.cfg["home"]["fst_offset_from_perkins_in_min"]
        lo, hi = 7 * 60 + 30, 11 * 60
        out = []
        for d in self.weekdays_before(target, h["lookback_weekdays"]):
            bus = self.travel_times(d, h["bus_in_perkins"], h["bus_in_broadway"])
            red = self.travel_times(d, [h["red_nb_broadway"]], [h["red_nb_kendall"]])
            day = Day(str(d),
                      buses=[(dep + off, arr) for dep, arr in bus if lo <= dep <= hi],
                      trains=[(dep, arr) for dep, arr in red if lo <= dep <= hi])
            if len(day.trains) > 10:   # skip days with no rail data at all
                out.append(day)
        return out

    # ---------- evening / generic samples ----------
    def ride_samples(self, target: dt.date, frm, to, start_min, end_min, n_days=None):
        """Ride durations (minutes) for trips departing in a time window, over recent weekdays."""
        n = n_days or self.cfg["history"]["lookback_weekdays"]
        rides = []
        for d in self.weekdays_before(target, n):
            for dep, arr in self.travel_times(d, frm, to):
                if start_min <= dep <= end_min and 0 < arr - dep < 60:
                    rides.append(arr - dep)
        return rides

    def headway_median(self, target, frm, to, start_min, end_min):
        gaps = []
        for d in self.weekdays_before(target, 10):
            deps = [dep for dep, _ in self.travel_times(d, frm, to) if start_min <= dep <= end_min]
            gaps += [b - a for a, b in zip(deps, deps[1:]) if b - a > 0.5]
        return median(gaps) or 5.0
