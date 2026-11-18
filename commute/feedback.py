"""Your own trip log: tap (or type) where you are as you go, and the planner learns your real
door-to-platform times, stairs and all, plus your actual ride times.

Ways in (all end up in handle()):
  - buttons on notifications:  body "log:<step>", "log:walking", "undo"
  - typed text to the command topic (ntfy web app, iOS Shortcut, curl):
        "at stop", "on platform @9:14", "off train 2 min ago", "note the escalator was out",
        "set desk_to_kendall 6", "unset desk_to_kendall", "trip home", "report", "end"
  - CLI:  python3 -m commute log "at desk @9:31"
"""
import datetime as dt
import re
import time

from .util import TZ, hm, median, mins, pct

STEPS = {
    "morning": [
        ("left_home", "🚪 Left home", ["left home", "leaving home", "leaving", "left", "leave", "out the door"]),
        ("at_stop", "📍 At the F St stop", ["at the stop", "at stop", "at bus stop", "stop"]),
        ("on_bus", "🚌 On the bus", ["on the bus", "on bus", "boarded", "bus"]),
        ("off_bus", "Off the bus at Broadway", ["off the bus", "off bus", "at broadway", "broadway"]),
        ("on_platform", "🚇 On the platform", ["on the platform", "on platform", "at platform", "platform"]),
        ("on_train", "On the train", ["on the train", "on train", "train"]),
        ("off_train", "Off at Kendall", ["off the train", "off train", "at kendall", "kendall"]),
        ("at_dest", "🏢 At my desk", ["at my desk", "at desk", "desk", "at work", "arrived", "here"]),
    ],
    "home": [
        ("left_desk", "🏢 Left my desk", ["left my desk", "left desk", "left work", "leaving", "left", "leave"]),
        ("on_platform", "🚇 On the Kendall platform", ["on the platform", "on platform", "platform", "at kendall", "kendall"]),
        ("on_train", "On the train", ["on the train", "on train", "train"]),
        ("off_train", "Off at Broadway", ["off the train", "off train", "at broadway", "broadway"]),
        ("at_stop", "📍 At the Broadway bus stop", ["at the stop", "at stop", "at bus stop", "stop"]),
        ("on_bus", "🚌 On the bus", ["on the bus", "on bus", "boarded", "bus"]),
        ("off_bus", "Off at F St", ["off the bus", "off bus", "at f st", "f st"]),
        ("at_dest", "🏠 Home", ["at home", "home", "arrived", "here"]),
    ],
}
WALK_ALIASES = ["walking instead", "walking home", "walking", "walk"]

# (from_step, to_step, what it measures, only-if-walking?)  Totals include doors, stairs, crossings.
SEGMENTS = {
    "morning": [
        ("left_home", "at_stop", "walk_to_bus_stop", False),
        ("left_home", "on_platform", "walk_to_station", True),
        ("off_bus", "on_platform", "bus_to_platform", False),
        ("off_train", "at_dest", "walk_from_kendall", False),
    ],
    "home": [
        ("left_desk", "on_platform", "desk_to_kendall", False),
        ("off_train", "at_stop", "platform_to_bus", False),
        ("off_bus", "at_dest", "stop_to_home", False),
        ("off_train", "at_dest", "station_to_home", True),
    ],
}
RIDES = {"morning": {("on_bus", "off_bus"): "bus_in", ("on_train", "off_train"): "red_nb"},
         "home": {("on_bus", "off_bus"): "bus_out", ("on_train", "off_train"): "red_sb"}}
WAITS = {("at_stop", "on_bus"): "waiting for the bus", ("on_platform", "on_train"): "waiting for the train"}
LABELS = {
    "walk_to_bus_stop": "door → F St stop", "walk_to_station": "door → Broadway platform (walking)",
    "bus_to_platform": "off bus → NB platform (Broadway stairs)", "walk_from_kendall": "Kendall train → desk",
    "desk_to_kendall": "desk → Kendall platform (office doors)", "platform_to_bus": "off train → Broadway bus stop",
    "stop_to_home": "F St stop → door", "station_to_home": "Broadway → door (walking)", "buffer": "buffer",
}
TIME_RE = re.compile(r"@?\s*(\d{1,2}):(\d{2})\s*(am|pm)?|(\d+)\s*(?:min|m)\s*ago|^-(\d+)$|\s-(\d+)$", re.I)


class Feedback:
    def __init__(self, db, prior_weight=2.0):
        self.db, self.k = db, prior_weight
        db.executescript("""
          create table if not exists trips(id integer primary key, direction text, date text, started real,
                                           plan_arrive real, plan_text text, closed int default 0);
          create table if not exists events(trip_id int, step text, ts real, raw text);
          create table if not exists notes(trip_id int, ts real, text text);
          create table if not exists overrides(param text primary key, value real);
        """)
        db.commit()

    # ---------------------------------------------------------------- storage
    def active_trip(self, ts=None):
        r = self.db.execute("select id, direction, started from trips where closed=0 order by id desc limit 1").fetchone()
        if r and abs((ts or time.time()) - r[2]) > 3 * 3600:   # forgotten trip: close it
            self.db.execute("update trips set closed=1 where id=?", (r[0],))
            self.db.commit()
            return None
        return r

    def start_trip(self, direction, ts, plan_arrive=None, plan_text=None):
        self.db.execute("update trips set closed=1 where closed=0")
        d = dt.datetime.fromtimestamp(ts, TZ).date().isoformat()
        cur = self.db.execute("insert into trips(direction, date, started, plan_arrive, plan_text) values(?,?,?,?,?)",
                              (direction, d, ts, plan_arrive, plan_text))
        self.db.commit()
        return cur.lastrowid

    def logged_today(self, direction, step):
        today = dt.datetime.now(TZ).date().isoformat()
        return self.db.execute("select 1 from events e join trips t on t.id=e.trip_id where t.direction=? "
                               "and t.date=? and e.step=? limit 1", (direction, today, step)).fetchone() is not None

    def trip_status(self):
        a = self.active_trip()
        if not a:
            return None
        return {"dir": a[1], "steps": [[s, int(ts)] for s, ts in self.events(a[0])]}

    def events(self, trip_id):
        return self.db.execute("select step, ts from events where trip_id=? order by ts", (trip_id,)).fetchall()

    def next_steps(self, direction, trip_id):
        seq = [s for s, _, _ in STEPS[direction]]
        done = {s for s, _ in self.events(trip_id)}
        walking = "walking" in done
        last = max((seq.index(s) for s in done if s in seq), default=-1)
        rest = seq[last + 1:]
        if walking:   # skip the bus steps
            rest = [s for s in rest if s in (("on_platform", "on_train", "off_train", "at_dest") if direction == "morning"
                                               else ("at_dest",))]
        return rest, walking

    # ---------------------------------------------------------------- parsing
    @staticmethod
    def _parse_time(text, ts):
        m = TIME_RE.search(text)
        if not m:
            return text, ts
        base = dt.datetime.fromtimestamp(ts, TZ)
        if m.group(1):
            h, mi = int(m.group(1)), int(m.group(2))
            ap = (m.group(3) or "").lower()
            if ap == "pm" and h < 12:
                h += 12
            elif not ap and h < 7:          # "5:42" typed in the evening means pm
                h += 12
            t = base.replace(hour=h, minute=mi, second=0)
        else:
            t = base - dt.timedelta(minutes=int(m.group(4) or m.group(5) or m.group(6)))
        return (text[:m.start()] + text[m.end():]).strip(), t.timestamp()

    @staticmethod
    def _match_step(direction, text):
        best = None
        for step, _, aliases in STEPS[direction]:
            for a in aliases:
                if text == a or text.startswith(a + " ") or text.endswith(" " + a):
                    if not best or len(a) > best[1]:
                        best = (step, len(a))
        return best[0] if best else None

    # ---------------------------------------------------------------- main entry
    def handle(self, text, ts, plan_hook=None):
        """Returns None if `text` isn't a feedback command, else dict(title, message, buttons=[(label, body)])."""
        raw = text.strip()
        m = re.search(r"\s*@ts=(\d+(?:\.\d+)?)", raw)
        if m:
            ts = float(m.group(1))
            raw = (raw[:m.start()] + raw[m.end():]).strip()
        t = raw.lower().strip()
        if t.startswith("log:"):
            t = t[4:].replace("_", " ")
            t = {"left home": "left home", "at dest": "here"}.get(t, t)
        if t in ("report", "learned", "what did you learn"):
            return {"title": "What I've learned from your trips", "message": self.report()}
        if t.startswith("set "):
            parts = t.split()
            if len(parts) == 3 and parts[1] in LABELS:
                self.db.execute("insert or replace into overrides values(?,?)", (parts[1], float(parts[2])))
                self.db.commit()
                return {"title": "Got it", "message": f"{LABELS[parts[1]]} fixed at {float(parts[2]):g} min "
                                                      f"(`unset {parts[1]}` to go back to learning)."}
            return {"title": "Unknown setting", "message": "Settable: " + ", ".join(LABELS)}
        if t.startswith("unset "):
            self.db.execute("delete from overrides where param=?", (t.split()[1],))
            self.db.commit()
            return {"title": "Back to learning", "message": f"{t.split()[1]} will be learned from your trips again."}
        if t in ("end", "end trip", "cancel", "cancel trip"):
            self.db.execute("update trips set closed=1 where closed=0")
            self.db.commit()
            return {"title": "Trip log closed", "message": "Saved what you logged."}

        active = self.active_trip(ts)
        m = re.match(r"(note|n)[:\s]+(.*)", raw, re.I | re.S)
        if m:
            self.db.execute("insert into notes values(?,?,?)", (active[0] if active else None, ts, m.group(2)))
            self.db.commit()
            return {"title": "Note saved", "message": m.group(2)}
        m = re.match(r"trip\s+(morning|home|in|out)(?:\s+(\d{4}-\d{2}-\d{2}))?", t)
        if m:
            direction = "morning" if m.group(1) in ("morning", "in") else "home"
            when = ts
            if m.group(2):
                when = dt.datetime.combine(dt.date.fromisoformat(m.group(2)),
                                           dt.time(8 if direction == "morning" else 17), TZ).timestamp()
            tid = self.start_trip(direction, when)
            return self._step_reply(direction, tid, f"Started a {direction} trip log.")
        if t == "undo":
            if not active:
                return {"title": "Nothing to undo", "message": "No trip in progress."}
            r = self.db.execute("select rowid, step from events where trip_id=? order by ts desc limit 1",
                                (active[0],)).fetchone()
            if r:
                self.db.execute("delete from events where rowid=?", (r[0],))
                self.db.commit()
            return self._step_reply(active[1], active[0], f"Removed “{r[1] if r else 'nothing'}”.")

        body, when = self._parse_time(t, ts)
        body = body.strip(" .!,")
        if any(body == a for a in WALK_ALIASES):
            if not active:
                return None
            self.db.execute("insert into events values(?,?,?,?)", (active[0], "walking", when, raw))
            self.db.commit()
            return self._step_reply(active[1], active[0], "Walking. Noted.")
        if active:
            direction = active[1]
        elif any(w in body for w in ("desk", "work", "office")) and self._match_step("home", body) == "left_desk":
            direction = "home"
        elif "home" in body and self._match_step("morning", body) == "left_home":
            direction = "morning"
        else:
            direction = "morning" if dt.datetime.fromtimestamp(when, TZ).hour < 13 else "home"
        step = self._match_step(direction, body)
        if not step:
            if active:   # free text during a trip is kept as a note rather than dropped
                self.db.execute("insert into notes values(?,?,?)", (active[0], ts, raw))
                self.db.commit()
                return {"title": "Saved as a note", "message": raw}
            return None
        if not active:
            live = plan_hook and abs(when - time.time()) < 300   # only score live trips against a live plan
            plan_arrive, plan_text = plan_hook(direction) if live else (None, None)
            tid = self.start_trip(direction, when, plan_arrive, plan_text)
            active = (tid, direction, when)
        self.db.execute("insert into events values(?,?,?,?)", (active[0], step, when, raw))
        self.db.commit()
        label = dict((s, l) for s, l, _ in STEPS[direction])[step]
        if step == "at_dest":
            return self._finish(active[0], direction)
        return self._step_reply(direction, active[0],
                                f"{label} at {hm(mins(dt.datetime.fromtimestamp(when, TZ)))}.")

    def _step_reply(self, direction, tid, head):
        rest, walking = self.next_steps(direction, tid)
        labels = dict((s, l) for s, l, _ in STEPS[direction])
        buttons = []
        if rest:
            buttons.append((labels[rest[0]], f"log:{rest[0]}"))
            if not walking and rest[0] in (("at_stop", "on_bus") if direction == "morning" else ("at_stop", "on_bus")):
                buttons.append(("🚶 Walking instead", "log:walking"))
            elif len(rest) > 1:
                buttons.append((f"Skip → {labels[rest[1]]}", f"log:{rest[1]}"))
        buttons.append(("↩ Undo", "undo"))
        msg = f"Next: {labels[rest[0]]}." if rest else "Tap when you arrive."
        if len(self.events(tid)) <= 1:
            msg += ("\nTap as you go. Missed one? Type it with a time in the command topic, "
                    "e.g. “on platform @9:14”. Anything else you type is saved as a note.")
        return {"title": head, "message": msg, "buttons": buttons, "quiet": True}

    def _finish(self, tid, direction):
        before = self.learned()
        self.db.execute("update trips set closed=1 where id=?", (tid,))
        self.db.commit()
        after = self.learned()
        ev = self.events(tid)
        trip = self.db.execute("select plan_arrive, plan_text from trips where id=?", (tid,)).fetchone()
        first, last = ev[0][1], ev[-1][1]
        tmin = lambda x: mins(dt.datetime.fromtimestamp(x, TZ))  # noqa: E731
        lines = [f"Door to door: {(last - first) / 60:.0f} min ({hm(tmin(first))} → {hm(tmin(last))})."]
        if trip and trip[0]:
            diff = tmin(last) - trip[0]
            lines.append(f"Plan said ~{hm(trip[0], 'ceil')}; you were {abs(diff):.0f} min "
                         f"{'later' if diff > 0 else 'earlier'}." if abs(diff) >= 1 else "Right on plan.")
        times = dict(ev)
        for a, b, param, needs_walk in SEGMENTS[direction]:
            if a in times and b in times and (not needs_walk or "walking" in times):
                lines.append(f"{LABELS[param]}: {(times[b] - times[a]) / 60:.1f} min")
        for (a, b), what in WAITS.items():
            if a in times and b in times:
                lines.append(f"{what}: {(times[b] - times[a]) / 60:.1f} min")
        changed = [f"{LABELS[k]} {before[k][0]:.1f} → {after[k][0]:.1f} min"
                   for k in after if k in before and abs(after[k][0] - before[k][0]) >= 0.2]
        if changed:
            lines.append("Updated: " + "; ".join(changed))
        return {"title": "Trip logged ✔", "message": "\n".join(lines), "quiet": True}

    # ---------------------------------------------------------------- learning
    def observations(self):
        """{param: [minutes]}, {ride_key: [minutes]} from all completed segments."""
        seg, rides = {}, {}
        for tid, direction in self.db.execute("select id, direction from trips"):
            times = {}
            for step, ts in self.events(tid):
                times.setdefault(step, ts)
            for a, b, param, needs_walk in SEGMENTS[direction]:
                if a in times and b in times and (not needs_walk or "walking" in times):
                    seg.setdefault(param, []).append((times[b] - times[a]) / 60)
            for (a, b), key in RIDES[direction].items():
                if a in times and b in times:
                    rides.setdefault(key, []).append((times[b] - times[a]) / 60)
        return seg, rides

    def learned(self, priors=None):
        """{param: (value, n, source)}. Blends your config guess with what you've logged:
        weight n/(n+2), aiming at your 75th percentile so plans aren't built on your best day."""
        priors = priors or getattr(self, "_priors", {})
        seg, _ = self.observations()
        overrides = dict(self.db.execute("select param, value from overrides"))
        out = {}
        for p, prior in priors.items():
            if p in overrides:
                out[p] = (overrides[p], 0, "set by you")
                continue
            xs = [x for x in seg.get(p, []) if 0 < x < max(3 * prior, prior + 15)]   # drop forgot-to-tap outliers
            if not xs:
                out[p] = (prior, 0, "config")
                continue
            n = len(xs)
            target = pct(xs, 0.75) if n >= 3 else median(xs)
            w = n / (n + self.k)
            out[p] = (w * target + (1 - w) * prior, n, f"{n} trip{'s' if n > 1 else ''}")
        return out

    def params(self, base):
        self._priors = {k: v for k, v in base.items() if k in LABELS and k != "buffer"}
        c = dict(base)
        for k, (v, _, _) in self.learned(self._priors).items():
            c[k] = round(v, 1)
        return c

    def ride_samples(self):
        return self.observations()[1]

    def report(self):
        lines = []
        for k, (v, n, src) in self.learned().items():
            prior = self._priors.get(k)
            lines.append(f"{LABELS[k]}: {v:.1f} min ({src}{'' if not n else f', you guessed {prior:g}'})")
        _, rides = self.observations()
        for k, xs in rides.items():
            lines.append(f"your {k} rides: median {median(xs):.1f} min over {len(xs)}")
        rows = self.db.execute("select plan_arrive, (select max(ts) from events e where e.trip_id=t.id and step='at_dest') "
                               "from trips t where plan_arrive is not null").fetchall()
        diffs = [mins(dt.datetime.fromtimestamp(b, TZ)) - a for a, b in rows if b]
        if diffs:
            lines.append(f"Plan vs. reality: median {median(diffs):+.1f} min over {len(diffs)} trips")
        notes = self.db.execute("select ts, text from notes order by ts desc limit 5").fetchall()
        if notes:
            lines.append("Recent notes: " + " | ".join(n for _, n in notes))
        return "\n".join(lines) or "No trips logged yet."
