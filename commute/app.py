"""Wires it all together.

`tick` is the one entry point GitHub Actions calls every ~15 min in your commute windows:
  read your phone's commands → night-before plan if due → watch the morning (every minute, from an
  hour before you'd leave) or the way home (from an hour before you leave work) → publish status.
"""
import datetime as dt
import hashlib
import json
import pathlib
import re
import time
import tomllib

from . import model
from .feedback import Feedback
from .history import History
from .mbta import MBTA
from .notify import Notifier, Ntfy
from .recorder import ErrorModel, Recorder
from .util import TZ, hm, log, mins, now, parse_hm, pct

DEAD = ("CANCELLED", "SKIPPED")
IMPORTANT = {"SHUTTLE", "SUSPENSION", "DETOUR", "STOP_CLOSURE", "STATION_CLOSURE", "DELAY",
             "SERVICE_CHANGE", "STOP_MOVE", "STOP_MOVED", "CANCELLATION", "SNOW_ROUTE"}
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def parse_clock(tok, evening=False):
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", tok.strip())
    if not m:
        return None
    h, mi, ap = int(m[1]), int(m[2] or 0), m[3]
    if ap == "pm" and h < 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    elif not ap and evening and h < 12:
        h += 12
    return h * 60 + mi


class App:
    def __init__(self, config_path="config.toml", dry_run=False, readonly=False):
        self.cfg = cfg = tomllib.loads(pathlib.Path(config_path).read_text())
        self.readonly = readonly
        self.route = cfg["home"]["bus_route"]
        self.stop_label = cfg["home"]["bus_stop_match"]
        self.places = cfg.get("places", {})
        self.mbta = MBTA(cfg["mbta"]["api_key"])
        self.hist = History(cfg)
        self.rec = Recorder()
        n = cfg["ntfy"]
        self.ntfy = Ntfy(n["server"], n["topic"], n["command_topic"], n.get("status_topic", ""),
                         n.get("token", ""), dry_run)
        self.state_path = pathlib.Path("state.json")
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.notify = Notifier(self.ntfy, cfg.get("webpush", {}), self.state, dry_run)
        ids = pathlib.Path("resolved_stops.json")
        self.ids = json.loads(ids.read_text()) if ids.exists() else None
        p, h, w, t = cfg["prefs"], cfg["home"], cfg["work"], cfg["transfer"]
        # Starting guesses; every one except buffer gets replaced by what you log (feedback.py).
        self.base_c = {
            "walk_to_bus_stop": h["walk_to_bus_stop_min"], "walk_to_station": h["walk_to_station_min"],
            "walk_from_kendall": w["walk_kendall_to_desk_min"],
            "desk_to_kendall": w.get("walk_desk_to_kendall_min", w["walk_kendall_to_desk_min"]),
            "bus_to_platform": t["bus_to_platform_min"], "platform_to_bus": t["platform_to_bus_min"],
            "stop_to_home": h.get("walk_stop_to_home_min", h["walk_to_bus_stop_min"]),
            "station_to_home": h.get("walk_station_to_home_min", h["walk_to_station_min"]),
            "buffer": p["buffer_min"], "bus_ride_default": h["bus_ride_default_min"],
        }
        fb = cfg.get("feedback", {})
        self.fb = Feedback(self.rec.db, prior_weight=fb.get("prior_weight", 2.0))
        self.ride_weight = fb.get("ride_weight", 5)
        self._cache_day, self._samples = None, {}
        self.st = {"morning": None, "home": None}     # latest results, for the web app
        self.watching = None
        self._last_status = (None, 0)

    @property
    def c(self):
        return self.fb.params(self.base_c)

    def save_state(self):
        if self.readonly:
            return
        cutoff = str(now().date() - dt.timedelta(days=7))
        for k in ("overrides", "plans"):
            self.state[k] = {d: v for d, v in self.state.get(k, {}).items() if d >= cutoff}
        self.state_path.write_text(json.dumps(self.state, indent=1))

    # ================================================================ your schedule
    def day(self, d: dt.date):
        p = self.cfg["prefs"]
        o = self.state.get("overrides", {}).get(str(d), {})
        return {"off": o.get("off", d.weekday() not in p.get("workdays", [0, 1, 2, 3, 4])),
                "arrive": o.get("arrive", parse_hm(p["arrive_by"])),
                "leave_work": o.get("leave_work", parse_hm(p["leave_work_at"])),
                "changed": bool(o)}

    def expected_leave(self, d):
        plan = self.state.get("plans", {}).get(str(d))
        return plan["leave"] if plan else self.day(d)["arrive"] - 35

    def schedule_cmd(self, t):
        """'tomorrow arrive 10 leave 6:15', 'today leave 4:45', 'friday off', 'tomorrow default'"""
        m = re.match(r"(today|tomorrow|tmrw|tmr|" + "|".join(DAYS) + r")\b(.*)", t)
        if not m:
            return None
        word, rest = m.group(1), m.group(2)
        today = now().date()
        if word == "today":
            d = today
        elif word.startswith("t"):
            d = today + dt.timedelta(days=1)
        else:
            d = today + dt.timedelta(days=(DAYS.index(word) - today.weekday() - 1) % 7 + 1)
        if not re.search(r"\b(default|reset|normal|usual|off|wfh|no commute|home all day|day off|holiday|"
                         r"not going|on|going in|commuting|in office)\b|arriv|leav", rest):
            return None                         # e.g. a note that happens to start with "today"
        ov = self.state.setdefault("overrides", {})
        o = dict(ov.get(str(d), {}))
        if re.search(r"\b(default|reset|normal|usual)\b", rest):
            o = {}
        if re.search(r"\b(off|wfh|no commute|home all day|day off|holiday|not going)\b", rest):
            o["off"] = True
        elif re.search(r"\b(on|going in|commuting|in office)\b", rest):
            o["off"] = False
        a = re.search(r"arriv\w*\s+(?:by\s+|at\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)", rest)
        lv = re.search(r"leav\w*\s+(?:work\s+)?(?:by\s+|at\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)", rest)
        if a and parse_clock(a.group(1)) is not None:
            o["arrive"], o["off"] = parse_clock(a.group(1)), False
        if lv and parse_clock(lv.group(1), evening=True) is not None:
            o["leave_work"], o["off"] = parse_clock(lv.group(1), evening=True), False
        ov[str(d)] = o
        if not o:
            ov.pop(str(d), None)
        self.save_state()
        s = self.day(d)
        label = "Today" if d == today else ("Tomorrow" if d == today + dt.timedelta(days=1) else d.strftime("%A"))
        msg = "No commute." if s["off"] else f"In by {hm(s['arrive'])}, leave work {hm(s['leave_work'])}."
        self.notify.push(f"{label}: updated", msg, priority=2, tag="schedule")
        if d == today + dt.timedelta(days=1) and not s["off"] and self.state.get("night_sent") == str(today):
            self.night(d)            # already sent tonight's plan; redo it for the new times
        return True

    def info(self, d):
        s = self.day(d)
        out = {"date": str(d), "off": s["off"], "arrive": hm(s["arrive"]), "leave_work": hm(s["leave_work"]),
               "changed": s["changed"]}
        plan = self.state.get("plans", {}).get(str(d))
        if plan:
            out.update(ready=hm(plan["ready"], "floor"), plan=plan["text"])
        return out

    # ================================================================ setup + history samples
    def setup(self):
        match = self.cfg["home"]["bus_stop_match"].lower()
        inb, outb = self.mbta.route_stops(self.route, 1), self.mbta.route_stops(self.route, 0)
        fst = [s for s in inb if match in s["name"].lower()]
        # Bus stops have no parent_station in the live API; match by name ("Broadway Station - Red Line",
        # "W Broadway @ Broadway Station") as well as by parent, to stay robust if MBTA adds one.
        is_bw = lambda s: s["parent"] == "place-brdwy" or "broadway station" in s["name"].lower()  # noqa: E731
        b_in = [s for s in inb if is_bw(s)]
        b_out = [s for s in outb if is_bw(s)]
        if not fst:
            raise SystemExit(f"No inbound {self.route} stop matching '{match}'. Inbound stops:\n" +
                             "\n".join(f"  {s['id']}: {s['name']}" for s in inb))
        self.ids = {"fst_in": fst[0]["id"], "bway_in": [s["id"] for s in b_in], "bway_out": [s["id"] for s in b_out]}
        pathlib.Path("resolved_stops.json").write_text(json.dumps(self.ids, indent=2))
        print(f"Home stop (inbound): {fst[0]['id']} {fst[0]['name']}")
        for s in fst[1:]:
            print(f"   other match: {s['id']} {s['name']}  (edit resolved_stops.json if this is yours)")
        print(f"Broadway, 9 arriving: {[(s['id'], s['name']) for s in b_in]}")
        print(f"Broadway, 9 leaving: {[(s['id'], s['name']) for s in b_out]}")

    def _need_ids(self):
        if not self.ids:
            self.setup()
        return self.ids

    def samples(self):
        today = now().date()
        if self._cache_day != today:
            h = self.cfg["history"]
            off_in = self.cfg["home"]["fst_offset_from_perkins_in_min"]
            off_out = self.cfg["home"]["fst_offset_from_perkins_out_min"]
            hs = self.hist
            s = {
                "red_nb": hs.ride_samples(today, [h["red_nb_broadway"]], [h["red_nb_kendall"]], 480, 630),
                "red_nb_headway": hs.headway_median(today, [h["red_nb_broadway"]], [h["red_nb_kendall"]], 480, 630),
                "bus_in": [r - off_in for r in hs.ride_samples(today, h["bus_in_perkins"], h["bus_in_broadway"], 480, 630)],
                "red_sb": hs.ride_samples(today, [h["red_sb_kendall"]], [h["red_sb_broadway"]], 930, 1200),
                "red_sb_headway": hs.headway_median(today, [h["red_sb_kendall"]], [h["red_sb_broadway"]], 930, 1200),
                "bus_out": [r + off_out for r in hs.ride_samples(today, h["bus_out_broadway"], h["bus_out_perkins"], 930, 1200)],
            }
            self._samples, self._cache_day = s, today
        merged = dict(self._samples)
        for key, xs in self.fb.ride_samples().items():
            merged[key] = list(merged.get(key, [])) + list(xs) * self.ride_weight
        return merged

    # ================================================================ 1) night before
    def night(self, target: dt.date = None):
        ids, c, p = self._need_ids(), self.c, self.cfg["prefs"]
        target = target or (now().date() + dt.timedelta(days=1))
        s = self.day(target)
        if s["off"]:
            log(f"{target}: no commute")
            return
        deadline = s["arrive"]
        days = self.hist.morning_days(target)
        if not days:
            self.notify.push("Commute plan unavailable", "Couldn't load Red Line history from TransitMatters.")
            return
        note, sched = "", []
        try:
            sched = [mins(x["time"]) for x in self.mbta.schedules(
                ids["fst_in"], self.route, 1, target.isoformat(), "07:00", "11:00")]
        except Exception as e:
            log(f"[night] schedule lookup failed: {e}")
        bus_days = [d for d in days if len(d.buses) >= 5]
        if len(bus_days) >= 5:
            days = bus_days
        elif sched:
            days = model.synthetic_days(sched, days, c)
            note = "\n(9 bus history not published yet, so this uses tomorrow's 9 schedule plus typical lateness.)"
        best, cands = model.plan_night(days, c, deadline, deadline - 80, deadline - 12, p["on_time_confidence"])
        if not best:
            self.notify.push("Commute plan unavailable", "Not enough history to plan tomorrow.")
            return
        ready = best.leave - c["buffer"]
        at_stop = best.leave + c["walk_to_bus_stop"]
        lines = [f"Leave {hm(best.leave, 'floor')}, at the {self.stop_label} stop by {hm(at_stop, 'floor')}."]
        nxt = [t for t in sched if t >= at_stop - 1]
        if nxt:
            line = f"Aim for the {hm(nxt[0])} 9 bus (scheduled)"
            if len(best.bus_times) >= 5:
                line += (f"; the bus you'd catch has come between {hm(pct(best.bus_times, .1))} and "
                         f"{hm(pct(best.bus_times, .9))}")
            lines.append(line + ".")
        lines.append(f"Desk ~{hm(best.arr_med, 'ceil')} typically, {hm(best.arr_p90, 'ceil')} on a bad day "
                     f"({best.p_on_time:.0%} of days by {hm(deadline)}). Door to door ~{best.dur_med:.0f} min.")
        if best.p_walk >= 0.25:
            lines.append(f"Walking straight to Broadway beat the bus on {best.p_walk:.0%} of days at this time.")
        late = [x for x in cands if x.p_on_time >= 0.75]
        if late:
            last = max(late, key=lambda x: x.leave)
            if last.leave - best.leave >= 4:
                lines.append(f"Running late? Latest sensible leave: {hm(last.leave, 'floor')} ({last.p_on_time:.0%} on time).")
        try:
            at = dt.datetime.combine(target, dt.time(9, 0), TZ).isoformat()
            alerts = [a for a in self.mbta.alerts([self.route, "Red"], at=at)
                      if a["effect"] in IMPORTANT or a["severity"] >= 5]
            lines += [f"⚠ {a['header']}" for a in alerts[:3]]
            if alerts:
                lines.append("History may not reflect these alerts, so give yourself extra time.")
        except Exception as e:
            log(f"[night] alerts failed: {e}")
        lines.append(f"Based on {best.n} recent weekdays.{note}")
        text = "\n".join(lines)
        self.state.setdefault("plans", {})[str(target)] = {"leave": best.leave, "ready": ready, "text": text}
        self.save_state()
        label = "Tomorrow" if target == now().date() + dt.timedelta(days=1) else target.strftime("%A")
        self.notify.push(f"{label}: be ready by {hm(ready, 'floor')}", text, tags=["crescent_moon"], tag="night",
                         buttons=[self.ntfy.button("Find my optimal path", "plan_morning")])
        return best

    # ================================================================ 2) morning, live
    def morning_plan(self, earliest=None):
        ids, c, now_m = self._need_ids(), self.c, mins(now())
        s = self.samples()
        deadline = self.day(now().date())["arrive"]
        bus_f = self.mbta.predictions(ids["fst_in"], self.route, 1)
        bus_b = [x for st in ids["bway_in"] for x in self.mbta.predictions(st, self.route, 1)]
        red = self.mbta.predictions("place-brdwy", "Red", 1)
        try:
            alerts = [a for a in self.mbta.alerts([self.route, "Red"]) if a["effect"] in IMPORTANT]
        except Exception:
            alerts = []
        self.rec.log("bus_in_fst", bus_f)
        self.rec.log("red_nb_bway", red)
        buses = [{"t": mins(x["dep"] or x["arr"]), "trip": x["trip"]}
                 for x in bus_f if x["rel"] not in DEAD and (x["dep"] or x["arr"])]
        cancelled = [x for x in bus_f if x["rel"] in DEAD]
        bway_arr = {x["trip"]: mins(x["arr"] or x["dep"]) for x in bus_b
                    if (x["arr"] or x["dep"]) and x["rel"] not in DEAD}
        red_deps = sorted(mins(x["dep"]) for x in red
                          if x["dep"] and x["rel"] not in DEAD and mins(x["dep"]) >= now_m - 1)
        trains = model.extend_trains(red_deps, s["red_nb_headway"], now_m + 90)
        opts = model.morning_options(now_m, buses, bway_arr, trains, s["red_nb"], s["bus_in"], c, deadline, earliest)
        best, earliest = model.pick_morning(opts)
        gap = self.cfg["prefs"]["red_gap_alert_min"]
        gaps = [(a, b) for a, b in zip(red_deps, red_deps[1:]) if b - a >= gap and a <= now_m + 35]
        notes = [(f"c:{x['trip']}", f"❌ {'The ' + hm(mins(x['sched'])) if x['sched'] else 'A'} 9 bus is cancelled.")
                 for x in cancelled if not x["sched"] or mins(x["sched"]) >= now_m - 2]
        notes += [(f"g:{a:.1f}", f"🚇 Red Line gap at Broadway: {hm(a)} → {hm(b)}.") for a, b in gaps[:1]]
        notes += [(f"a:{a['id']}", f"⚠ {a['header']}") for a in alerts[:2]]
        self.st["morning"] = {
            "t": int(time.time()), "best": self.describe(best), "notes": [n for _, n in notes],
            "alt": self.describe(earliest) if earliest and best and earliest.arrive < best.arrive - 1 else None,
            "legs": [[k, round(a, 1), round(b, 1)] for k, a, b in best.legs(c)] if best else [],
            "leave": hm(best.leave, "floor") if best else None, "arrive": hm(best.arrive, "ceil") if best else None,
        }
        return {"best": best, "earliest": earliest, "cancelled": cancelled, "gaps": gaps, "alerts": alerts,
                "now": now_m, "deadline": deadline, "notes": notes}

    def describe(self, o):
        if o is None:
            return "No good option in the next half hour. Check the T app."
        if o.mode == "bus":
            s = (f"Leave {hm(o.leave, 'floor')} → 9 at {self.stop_label} {hm(o.bus_at_stop)} → Red {hm(o.train_dep)} "
                 f"→ desk ~{hm(o.arrive, 'ceil')}")
        else:
            s = f"Walk to Broadway: leave {hm(o.leave, 'floor')} → Red {hm(o.train_dep)} → desk ~{hm(o.arrive, 'ceil')}"
        if o.tight:
            s += " (go right now, no buffer)"
        elif o.slack < 1:
            s += " (tight transfer)"
        return s

    def push_morning_plan(self):
        plan = self.morning_plan()
        msg = "\n".join([self.describe(plan["best"])] +
                        ([f"Earliest arrival instead: {self.st['morning']['alt']}"] if self.st["morning"]["alt"] else []) +
                        [n for _, n in plan["notes"][:3]])
        self.notify.push("Your best way in right now", msg, priority=4, tags=["bus"], tag="morning",
                         buttons=self.morning_buttons())
        self.publish_status(force=True)

    def morning_buttons(self):
        return [self.ntfy.button("Find my optimal path", "plan_morning"),
                self.ntfy.button("🚪 Leaving now (log trip)", "log:left_home")]

    def home_buttons(self):
        return [self.ntfy.button("Refresh way home", "plan_home"),
                self.ntfy.button("🏢 Leaving desk (log trip)", "log:left_desk")]

    def _watch(self, mode, step, end_m):
        """Poll every minute until `step` says we're done or end_m passes."""
        poll = self.cfg["prefs"]["poll_seconds"]
        self.watching = mode
        ctx = {"first": True}
        try:
            while mins(now()) < end_m:
                self.consume()
                try:
                    if step(ctx):
                        break
                except Exception as e:
                    log(f"[{mode}] {e}")
                ctx["first"] = False
                self.publish_status()
                time.sleep(poll)
        finally:
            self.watching = None
            self.publish_status(force=True)

    @staticmethod
    def fresh_notes(notes, seen):
        """Only problems we haven't told you about. A Red Line gap that drifts a few minutes is the same gap."""
        out = []
        for key, text in notes:
            kind, val = key.split(":", 1)
            if kind == "g":
                v = float(val)
                if any(k == "g" and abs(x - v) < 6 for k, x in seen):
                    continue
                seen.add(("g", v))
            elif (kind, val) in seen:
                continue
            else:
                seen.add((kind, val))
            out.append(text)
        return out

    def watch_morning(self):
        d = self.day(now().date())
        planned = self.expected_leave(now().date())
        seen, reminded = set(), set()
        state = {"prev": None, "left_train": None}

        def step(ctx):
            if self.fb.logged_today("morning", "left_home") and not state["left_train"]:
                return True                       # you told us you've left
            plan = self.morning_plan(earliest=planned - 10)    # stay near your planned leave time
            now_m, best = plan["now"], plan["best"]
            if state["left_train"] and now_m > state["left_train"] + 2:
                return True
            msgs = self.fresh_notes(plan["notes"], seen)
            prio = 4 if any(m.startswith(("❌", "⚠")) for m in msgs) else 3
            if not state["left_train"] and best:
                key = (best.mode, best.trip, round(best.train_dep))
                told = state["prev"]
                to_go = best.leave - now_m
                if ctx["first"] or told is None:
                    msgs.insert(0, "Plan: " + self.describe(best))
                    state["prev"] = best
                else:
                    shift = best.leave - told.leave
                    changed = (best.mode != told.mode or abs(shift) >= 2 or abs(best.arrive - told.arrive) >= 2)
                    # Leaving EARLIER is urgent: always tell you. LATER can wait: only within 20 min of leaving,
                    # and at most every 5 min (a bus that keeps slipping shouldn't buzz you every minute).
                    urgent = shift <= -2 or best.mode != told.mode
                    later_ok = to_go <= 20 and now_m - state.get("told_at", -99) >= 5
                    if changed and (urgent or later_ok):
                        state["told_at"] = now_m
                        msgs.insert(0, f"🔄 Leave {'earlier' if shift < 0 else 'later'}: " + self.describe(best))
                        prio = max(prio, 4)
                        state["prev"] = best
                if 0.5 < to_go <= 3.5 and (key, 3) not in reminded:
                    reminded.add((key, 3))
                    msgs.insert(0, f"⏰ Leave in {max(1, round(to_go))} min.")
                    prio = max(prio, 4)
                if to_go <= 0.5 and (key, 0) not in reminded:
                    reminded.add((key, 0))
                    msgs = ["🚶 Leave now: " + self.describe(best)] + [m for m in msgs if not m.startswith(("🔄", "Plan"))]
                    prio, state["left_train"] = 5, best.train_dep
            if msgs:
                self.notify.push("Commute", "\n".join(msgs), priority=prio, tags=["bus"], tag="morning",
                                 buttons=self.morning_buttons())
            return False

        self._watch("morning", step, d["arrive"] + 10)

    # ================================================================ 3) way home
    def compute_home(self, prefer=None):
        ids, c = self._need_ids(), self.c
        t_now = now()
        now_m = mins(t_now)
        s = self.samples()
        red_k = self.mbta.predictions("place-knncl", "Red", 0)
        red_b = self.mbta.predictions("place-brdwy", "Red", 0)
        self.rec.log("red_sb_knd", red_k)
        self.rec.log("red_sb_bway", red_b)
        arr_b = {x["trip"]: mins(x["arr"]) for x in red_b if x["arr"]}
        preds = []
        for stop in ids["bway_out"]:
            ps = self.mbta.predictions(stop, self.route, 0)
            self.rec.log("bus_out_bway", ps)
            preds += ps
        buses, dead, dead_times = [], set(), []
        for x in preds:
            if x["rel"] in DEAD:
                dead.add(x["trip"])
                if x["sched"] and now_m - 2 <= mins(x["sched"]) <= now_m + 75:
                    dead_times.append(mins(x["sched"]))
            elif x["dep"] or x["arr"]:
                tt = mins(x["dep"] or x["arr"])
                buses.append({"t": tt, "kind": "pred", "label": hm(tt), "trip": x["trip"]})
        have = {b["trip"] for b in buses}
        try:
            end = t_now + dt.timedelta(minutes=90)
            for sc in self.mbta.schedules(",".join(ids["bway_out"]), self.route, 0, t_now.date().isoformat(),
                                          t_now.strftime("%H:%M"), end.strftime("%H:%M")):
                if sc["trip"] not in have and sc["trip"] not in dead:
                    tt = mins(sc["time"])
                    buses.append({"t": tt, "kind": "sched", "label": hm(tt) + "*", "trip": sc["trip"]})
        except Exception as e:
            log(f"[home] schedules failed: {e}")
        trains = sorted(({"dep": mins(x["dep"]), "trip": x["trip"], "arr_bway": arr_b.get(x["trip"])}
                         for x in red_k if x["dep"] and x["rel"] not in DEAD), key=lambda x: x["dep"])
        pred_deps = [t["dep"] for t in trains]
        for d in model.extend_trains(pred_deps, s["red_sb_headway"], now_m + 75)[len(trains):]:
            trains.append({"dep": d, "trip": None, "arr_bway": None})
        trains = [t for t in trains if t["dep"] - c["desk_to_kendall"] - c["buffer"] >= now_m - 0.2][:8]
        sims = model.simulate_home(now_m, trains, sorted(buses, key=lambda b: b["t"]), s["red_sb"], s["bus_out"],
                                   ErrorModel(self.rec), c)
        gap = self.cfg["prefs"]["red_gap_alert_min"]
        gaps = [(a, b) for a, b in zip(pred_deps, pred_deps[1:]) if b - a >= gap and a <= now_m + 40]
        try:
            alerts = [a for a in self.mbta.alerts([self.route, "Red"]) if a["effect"] in IMPORTANT]
        except Exception:
            alerts = []
        notes = [(f"c:{t:.0f}", f"❌ The {hm(t)} outbound 9 is cancelled.") for t in sorted(dead_times)]
        notes += [(f"g:{a:.1f}", f"🚇 Red Line gap at Kendall: {hm(a)} → {hm(b)}.") for a, b in gaps[:1]]
        notes += [(f"a:{a['id']}", f"⚠ {a['header']}") for a in alerts[:2]]
        best = self.pick_home(sims, now_m, prefer)
        self.st["home"] = {
            "t": int(time.time()), "best": self.describe_home(best), "notes": [n for _, n in notes],
            "rows": [[hm(r["train"]), hm(r["leave_office"], "floor"),
                      ", ".join(f"{k} {v:.0%}" for k, v in list(r["outcomes"].items())[:3]),
                      hm(r["home_med"], "ceil")] for r in sims[:6]],
        }
        return {"sims": sims, "best": best, "notes": notes, "now": now_m}

    def pick_home(self, sims, now_m, prefer=None):
        """The train nearest your usual leave-work time that wastes the least time standing around.
        `prefer`: the train we already told you about; keep it unless another is >1.5 min better."""
        if not sims:
            return None
        lw = self.day(now().date())["leave_work"]
        window = [r for r in sims if lw - 10 <= r["leave_office"] <= lw + 20] or sims[:2]
        best = min(window, key=lambda r: (round(r["door_to_door"]), r["leave_office"]))
        if prefer is not None:
            same = [r for r in window if abs(r["train"] - prefer) < 1]
            if same and same[0]["door_to_door"] - best["door_to_door"] < 1.5:
                return same[0]
        return best

    def describe_home(self, r):
        if not r:
            return "No Red Line predictions out of Kendall right now."
        top = next(iter(r["outcomes"].items()), ("walk", 1))
        via = "walk home from Broadway" if top[0] == "walk" else f"9 at {top[0]} ({top[1]:.0%})"
        return (f"Leave desk {hm(r['leave_office'], 'floor')} → {hm(r['train'])} train → {via} "
                f"→ home ~{hm(r['home_med'], 'ceil')}")

    def home_message(self, h):
        sims = h["sims"]
        if not sims:
            return "No Red Line predictions out of Kendall right now. Check the T app."
        first = sims[0]
        lines = [f"Leaving now: the {hm(first['train'])} train, home ~{hm(first['home_med'], 'ceil')} "
                 f"(80%: {hm(first['home_p80'], 'ceil')})."]
        eff = min(sims[:5], key=lambda r: (round(r["door_to_door"]), r["leave_office"]))
        if eff is not first and eff["home_med"] - first["home_med"] <= 6:
            lines.append(f"Least time in transit: leave your desk {hm(eff['leave_office'], 'floor')} for the "
                         f"{hm(eff['train'])} train, home ~{hm(eff['home_med'], 'ceil')} "
                         f"(~{first['door_to_door'] - eff['door_to_door']:.0f} min less standing around).")
        lines.append("")
        for a, b in zip(sims[:5], sims[1:6] + [None]):
            odds = ", ".join(f"{k} {v:.0%}" for k, v in list(a["outcomes"].items())[:3])
            same = b and abs(b["home_med"] - a["home_med"]) < 1
            lines.append(f"{hm(a['train'])} train (leave desk {hm(a['leave_office'], 'floor')}): {odds} → "
                         f"home ~{hm(a['home_med'], 'ceil')}{'  ← same as next' if same else ''}")
        lines += [n for _, n in h["notes"][:3]]
        lines.append("Times = 9 leaving Broadway; * = schedule only, no live prediction yet.")
        return "\n".join(lines)

    def home_plan(self, push=True):
        h = self.compute_home()
        msg = self.home_message(h)
        if push:
            self.notify.push("Way home", msg, tags=["house"], tag="home", buttons=self.home_buttons())
            self.publish_status(force=True)
        return msg

    def watch_home(self):
        lw = self.day(now().date())["leave_work"]
        seen, reminded = set(), set()
        state = {"prev": None, "announced": False, "left": None}

        def step(ctx):
            if self.fb.logged_today("home", "left_desk"):
                return True
            h = self.compute_home(prefer=state["prev"]["train"] if state["prev"] else None)
            now_m, best = h["now"], h["best"]
            if state["left"] and now_m > state["left"] + 3:
                return True
            msgs = self.fresh_notes(h["notes"], seen)
            prio = 4 if any(m.startswith(("❌", "⚠")) for m in msgs) else 3
            if best and not state["left"] and now_m >= lw - 25:
                told = state["prev"]
                if told is None:
                    msgs.insert(0, "Way home: " + self.describe_home(best))
                    state["prev"] = best
                elif (abs(best["leave_office"] - told["leave_office"]) >= 2
                      or abs(best["home_med"] - told["home_med"]) >= 3):
                    msgs.insert(0, "🔄 Way home changed: " + self.describe_home(best))
                    prio = max(prio, 4)
                    state["prev"] = best
                to_go = best["leave_office"] - now_m
                k = round(best["train"])
                if 0.5 < to_go <= 3.5 and (k, 3) not in reminded:
                    reminded.add((k, 3))
                    msgs.insert(0, f"⏰ Leave your desk in {max(1, round(to_go))} min.")
                    prio = max(prio, 4)
                if to_go <= 0.5 and (k, 0) not in reminded:
                    reminded.add((k, 0))
                    msgs = ["🚶 Leave your desk now: " + self.describe_home(best)] + \
                        [m for m in msgs if not m.startswith(("🔄", "Way home"))]
                    prio, state["left"] = 5, best["leave_office"]
            if msgs:
                self.notify.push("Way home", "\n".join(msgs), priority=prio, tags=["house"], tag="home",
                                 buttons=self.home_buttons())
            return False

        self._watch("home", step, lw + 45)

    # ================================================================ your phone ↔ the planner
    def plan_hook(self, direction):
        try:
            if direction == "morning":
                p = self.morning_plan()
                e = p["earliest"]
                return (e.arrive, self.describe(e)) if e else (None, None)
            h = self.compute_home()
            f = h["sims"][0] if h["sims"] else None
            return (f["home_med"], f"{hm(f['train'])} train") if f else (None, None)
        except Exception as e:
            log(f"[plan_hook] {e}")
            return None, None

    def log_trip(self, text, ts=None):
        r = self.fb.handle(text, ts or time.time(), self.plan_hook)
        if r:
            btns = [self.ntfy.button(lbl, body, clear=True) for lbl, body in r.get("buttons", [])]
            # step-by-step confirmations only go to the ntfy app; the web app shows them itself
            if not r.get("quiet") or not self.state.get("webpush_subs"):
                self.notify.push(r["title"], r["message"], priority=2 if r.get("quiet") else 3,
                                 tags=["memo"], buttons=btns, tag="trip")
        return r

    def handle(self, raw, ts=None):
        ts = ts or time.time()
        cmd = raw.strip().lower()
        try:
            if cmd.startswith("subscribe "):
                sub = json.loads(raw.strip()[10:])
                subs = [x for x in self.state.get("webpush_subs", []) if x.get("endpoint") != sub.get("endpoint")]
                self.state["webpush_subs"] = subs + [sub]
                self.save_state()
                self.notify.push("Notifications are on", "You'll get commute alerts here.", priority=2)
                return
            if cmd in ("plan_morning", "plan", "plan_home", "night", "accuracy", "ping", "status"):
                if time.time() - ts > 150:      # an old "plan now" request is useless now
                    return
                if cmd in ("plan_morning", "plan"):
                    self.push_morning_plan()
                elif cmd == "plan_home":
                    self.home_plan()
                elif cmd == "night":
                    self.night()
                elif cmd == "accuracy":
                    self.notify.push("Prediction accuracy at your stops", ErrorModel(self.rec).summary(), priority=2)
                else:
                    self.publish_status(force=True)
                return
            if self.schedule_cmd(re.sub(r"\s*@ts=\S+", "", cmd)):
                return
            if self.log_trip(raw, ts):
                return
            if time.time() - ts < 150:
                self.notify.push("Didn't catch that", "Try “tomorrow arrive 10 leave 6:15”, “on platform”, "
                                 "“note …”, “report”, “plan”, “plan_home”.", priority=2)
        except Exception as e:
            log(f"[cmd] {e}")

    def consume(self):
        """Process everything your phone has sent since last time (taps keep their original times)."""
        if self.readonly:
            return
        since = self.state.get("ntfy_since", "12h")
        for mid, ts, text in self.ntfy.poll(self.ntfy.cmd, since):
            self.state["ntfy_since"] = mid
            self.handle(text, ts)
        self.save_state()

    def publish_status(self, force=False):
        """The web app's data: one compact JSON message on the status topic. Sent when something
        changed, plus a heartbeat every 10 min so the app knows the watcher is alive."""
        if not self.ntfy.status_topic:
            return
        today = now().date()
        self.c  # noqa: B018  (loads learned priors)
        st = {"v": 1, "t": int(time.time()), "watch": self.watching,
              "vapid": self.cfg.get("webpush", {}).get("public_key"),
              "today": self.info(today), "tomorrow": self.info(today + dt.timedelta(days=1)),
              "morning": self.st["morning"], "home": self.st["home"],
              "learned": self.fb.report().splitlines()[:10], "trip": self.fb.trip_status()}
        body = {k: v for k, v in st.items() if k != "t"}
        digest = hashlib.sha1(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
        last_digest, last_t = self._last_status
        if not force and digest == last_digest and time.time() - last_t < 600:
            return
        text = json.dumps(st, separators=(",", ":"), ensure_ascii=False)
        if len(text.encode()) > 4000:           # ntfy turns >4 KB into an attachment; trim
            st["learned"] = st["learned"][:4]
            if st["home"] and st["home"].get("rows"):
                st["home"] = dict(st["home"], rows=st["home"]["rows"][:4])
            text = json.dumps(st, separators=(",", ":"), ensure_ascii=False)
        self.ntfy.publish_raw(self.ntfy.status_topic, text)
        self._last_status = (digest, time.time())

    # ================================================================ the scheduler
    def tick(self):
        """One scheduled run. Cheap no-op unless something is due."""
        self.consume()
        p = self.cfg["prefs"]
        t = now()
        today, now_m = t.date(), mins(t)
        st, d = self.state, self.day(today)
        lead = p["watch_lead_min"]
        if now_m >= parse_hm(p["night_notify_at"]) and st.get("night_sent") != str(today):
            st["night_sent"] = str(today)
            self.save_state()
            self.night(today + dt.timedelta(days=1))
        if not d["off"] and st.get("morning_done") != str(today):
            start = self.expected_leave(today) - lead
            if start - 15 <= now_m < d["arrive"] + 5:      # cron can run a bit early or late
                self.watch_morning()
                st["morning_done"] = str(today)
                self.save_state()
        if not d["off"] and st.get("home_done") != str(today):
            if d["leave_work"] - lead - 15 <= now_m < d["leave_work"] + 40:
                self.watch_home()
                st["home_done"] = str(today)
                self.save_state()
        self.publish_status()

    def serve(self):
        """Laptop / always-on mode: the same as Actions' tick, every minute."""
        log("Running. Ctrl-C to stop.")
        while True:
            try:
                self.tick()
            except Exception as e:
                log(f"[serve] {e}")
            time.sleep(60)
