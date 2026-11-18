"""Way-home choice: the train that puts a bus within 2 min of reaching the stop wins, even when walking is faster.
Run: python3 tests/test_home.py"""
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from commute import model  # noqa: E402
from commute.app import App  # noqa: E402


class ExactErr:      # no prediction error: isolates the decision logic
    def sample(self, key, lead):
        return 0.0


c = {"desk_to_kendall": 4, "buffer": 2, "platform_to_bus": 2, "station_to_home": 13, "stop_to_home": 2, "bus_ride_default": 6}
T = 17 * 60 + 40                      # 5:40pm
trains = [{"dep": T + k, "trip": f"t{k}", "arr_bway": None} for k in (0, 4, 8, 12)]
buses = [{"t": T + 12 + 3, "kind": "pred", "label": "a"}, {"t": T + 12 + 20, "kind": "pred", "label": "b"}]
sims = model.simulate_home(T - 10, trains, buses, [11.0], [6.0], ExactErr(), c, n=40, rng=random.Random(1))
by = {int(r["train"] - T): r for r in sims}
# train +0 arrives Broadway T+11, at the stop T+13, bus at T+15: wait 2 -> ready
assert by[0]["p_ready"] == 1 and abs(by[0]["wait_med"] - 2) < 1e-6, by[0]
# train +4: at the stop T+17, next bus T+32: a 15 min wait; walking (home ~T+28) beats it by far -> walk
assert by[4]["outcomes"].get("walk", 0) == 1 and by[4]["p_ready"] == 0, by[4]
best = min(sims, key=App._bus_key)
assert int(best["train"] - T) == 0, best
assert App.bus_cell(by[4]) == "walk home" and App.bus_cell(by[0]).startswith("9 a")
assert sims[0]["bus_med"] is not None and sims[0]["walk_med"] is not None   # both reported for "leave now"
print("HOME OK")
