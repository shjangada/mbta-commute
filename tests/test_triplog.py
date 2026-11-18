"""Trip log: only legs with both taps are learned; a missing tap scraps just that leg.
Run: python3 tests/test_triplog.py"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from commute.feedback import Feedback  # noqa: E402

db = sqlite3.connect(":memory:")
fb = Feedback(db)
fb._priors = {"desk_to_kendall": 4, "platform_to_bus": 2, "stop_to_home": 2, "bus_to_platform": 2,
              "walk_to_bus_stop": 2, "walk_from_kendall": 4}
T0 = 1_800_000_000


def tap(step, minutes, t0):
    fb.handle(f"log:{step} @ts={t0 + minutes * 60}", t0 + 7200)


# evening trip 1: forgot "off the train at Broadway" -> platform_to_bus scrapped, other two legs kept
tap("left_desk", 0, T0); tap("on_platform", 5, T0); tap("at_stop", 20, T0); tap("off_bus", 26, T0); tap("at_dest", 29, T0)
seg, _ = fb.observations()
assert abs(seg["desk_to_kendall"][0] - 5) < 1e-6 and abs(seg["stop_to_home"][0] - 3) < 1e-6, seg
assert "platform_to_bus" not in seg, "a leg missing its start must not count"
# evening trip 2: left desk but never reached the platform tap -> that leg is dropped
T1 = T0 + 86400
tap("left_desk", 0, T1); tap("off_train", 15, T1); tap("at_stop", 18, T1)
seg, _ = fb.observations()
assert len(seg["desk_to_kendall"]) == 1 and abs(seg["platform_to_bus"][0] - 3) < 1e-6, seg
# morning leg 2 and 3 can be logged on their own
T2 = T0 + 2 * 86400
tap("off_bus", 0, T2); tap("on_platform", 2.5, T2); tap("off_train", 12, T2); tap("at_dest", 17, T2)
seg, _ = fb.observations()
assert abs(seg["bus_to_platform"][0] - 2.5) < 1e-6 and abs(seg["walk_from_kendall"][0] - 5) < 1e-6, seg
# learned values start at the config default and move toward your logs as n grows
v = fb.learned()
assert v["bus_to_platform"][1] == 1 and 2 < v["bus_to_platform"][0] < 2.5, v["bus_to_platform"]
print("TRIPLOG OK")
