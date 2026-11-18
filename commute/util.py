"""Small time helpers. Everything internal is 'minutes since local midnight' (floats)."""
import datetime as dt
import math
import random
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")


def now() -> dt.datetime:
    return dt.datetime.now(TZ)


def mins(d: dt.datetime) -> float:
    d = d.astimezone(TZ)
    return d.hour * 60 + d.minute + d.second / 60


def parse_hm(s: str) -> float:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def hm(m: float, how: str = "round") -> str:
    """Format minutes-of-day as 9:04. how='floor' for leave times, 'ceil' for arrivals."""
    f = {"floor": math.floor, "ceil": math.ceil}.get(how, round)
    m = int(f(m))
    h, mm = divmod(m, 60)
    return f"{(h - 1) % 12 + 1}:{mm:02d}"


def pct(xs, p):
    if not xs:
        return None
    s = sorted(xs)
    k = (len(s) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def median(xs):
    return pct(xs, 0.5)


def sample(xs, default, rng=random):
    return rng.choice(xs) if xs else default


def parse_iso(s):
    return dt.datetime.fromisoformat(s) if s else None


def log(*a):
    """Quiet in GitHub Actions (public repo logs are public): set COMMUTE_QUIET=1."""
    import os
    if not os.environ.get("COMMUTE_QUIET"):
        print(*a, flush=True)
