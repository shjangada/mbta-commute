# CLAUDE.md: context for this project (read first)

Personal commute planner for one person, built in a long design conversation. This file holds every
decision so far. Keep it updated when decisions change.

## The commute
(Exact addresses are personal: see the gitignored `CLAUDE.local.md` and `[places]` in `config.toml`.)
- **Morning:** walk ~2 min to the inbound **9 bus** stop, ride to **Broadway** station, take the **Red Line
  northbound** to **Kendall/MIT**, walk to the office.
  Fallback: walk ~13 min straight to Broadway.
- **Evening:** reverse. The hard part is choosing which Red Line train out of Kendall to take so a
  9 bus is leaving Broadway soon after. Buses and trains don't leave exactly when predicted.
- **Ideal timing:** bus ~1 min away on reaching the stop, then the train ~1 min away on reaching the platform.

## The user's preferences (decided)
- Cares more about **short transit time** than strict punctuality; arriving earlier is fine.
- **Defaults: in by 9:30am, leave work 5:30pm.** Changeable for any day from the phone
  ("tomorrow arrive 10 leave 6:15", "friday off", "tomorrow default").
- **Always a 2-minute buffer** wherever they must *be* somewhere (stop, platform).
- **Polling every minute** is enough (delays show up minutes ahead). Watching starts **1 hour before**
  the expected leave time, morning and evening.
- **No always-on machine.** Runs on **GitHub Actions** (public repo, so free minutes). The 60-day
  inactivity pause doesn't matter: they're only here ~30 more days. ntfy's 12h message cache is fine.
- **iPhone on iOS 18, won't update.** Notifications go to a **home-screen web app** (PWA, web push,
  works on iOS 16.4+) hosted on **GitHub Pages**. No Apple developer account. The ntfy app is the fallback.
- Office exit (several doors) and Broadway's two sets of stairs take real time. These are **learned from
  the user's own trip logs**, not guessed (see Feedback).

## Architecture
```
GitHub Actions (cron every 15 min in commute windows, UTC; code converts to Boston time)
  tick.yml → python -m commute tick
     1. consume(): read phone commands from the ntfy *command topic* (taps keep original timestamps)
     2. night-before plan if due (21:30)
     3. morning watcher if now ≥ planned leave − 60 min: poll MBTA every 60 s until you leave
     4. way-home watcher if now ≥ leave_work − 60 min
     5. publish status JSON to the ntfy *status topic*; push alerts via web push (or ntfy app)
     state (sqlite + json) is encrypted (Fernet) and committed to state/state.enc
  plan.yml → python -m commute plan morning|home   (read-only; started by the web app's button
     when no watcher is running; needs a GitHub fine-grained token stored on the phone)

Phone web app (docs/, GitHub Pages, "Add to Home Screen")
  reads the status topic, posts commands/taps to the command topic, receives web push
  tabs: Now (plan + trip bar + "Find my optimal path") · Trip (tap-as-you-go log) · Days (per-day
  times) · Learned · Settings (setup code from keygen, GitHub repo + token, enable notifications)
```
ntfy (ntfy.sh) here is just a free message relay ("POST to a URL, read it back"). Topic names act as
passwords, so they're random (from `keygen`). The free tier allows ~250 messages/day per IP, so status is
published only on change plus a 10-min heartbeat.

## Data sources
- **MBTA V3 API** (key in config): `/predictions` (with `include=schedule` so cancelled trips show their
  time), `/schedules`, `/alerts`, `/stops`. Red direction: 0 = south, 1 = north. The 9: 1 = inbound.
- **TransitMatters Data Dashboard API** (`https://dashboard-api.labs.transitmatters.org`, backend of
  github.com/transitmatters/t-performance-dash): `/api/traveltimes/{date}?from_stop=&to_stop=` gives
  per-trip actual times.
  - Red platforms: Broadway NB `70082` → Kendall NB `70072`; Kendall SB `70071` → Broadway SB `70081`.
  - 9 timepoints: **Perkins Sq** (Dorchester St, one block from F St) inbound `9-1-45`, outbound
    `9-0-21`/`9-0-157`; Broadway Station inbound `9-1-150`, outbound `9-0-150`/`9-0-151`.
  - F St = Perkins +1 min inbound, −1 min outbound (config).
  - TM's **bus data can lag weeks**. The night plan then falls back to tomorrow's 9 schedule plus
    synthetic lateness, and says so.
- **Own logs:** every prediction seen is recorded and later scored (`recorder.py`), giving prediction
  error at *your* stops by lead time. Used in the way-home simulation.

## Planning logic (commute/model.py)
- **Night plan:** replay each candidate leave time against the last 30 weekdays of real bus and train
  times (cancellations and gaps are in the data). Pick the fastest typical door-to-door (median + 0.35×p90)
  that arrives by `arrive_by` on ≥ `on_time_confidence` (0.8, since the goal is "around 9:30") of days.
  Tie goes to the later leave. "Be ready by" = leave − buffer.
- **Live morning:** options = each predicted bus at F St (leave = bus − walk − buffer) and walking for
  each train. Pick the shortest duration; a <1 min transfer is penalized. While watching, options must
  start ≥ planned leave − 10 (`earliest`) so an early watch doesn't say "leave now" an hour early.
  "Find my optimal path" uses earliest = now (the user is ready).
- **Way home:** Monte Carlo (1,500 runs) per upcoming Kendall train. Inputs: Kendall→Broadway ride
  samples, learned prediction error for the 9, schedule-only buses (marked *), cancellations.
  Outputs: odds of catching each bus vs walking, and median/p80 home time.
  `pick_home` = least door-to-door near `leave_work`, sticking with the train already announced
  unless another is >1.5 min better.
- **Notifications:** problems are deduped by identity (trip id, alert id; a Red gap drifting <6 min is
  the same gap). Plan changes compare against what was last *told*. Earlier = always tell. Later = only
  within 20 min of leaving, at most every 5 min. Then "leave in 3 min" and "leave now"
  (watcher stops after you leave or log "left home").

## Feedback / learning (commute/feedback.py)
- **Trip tab = leg cards** (decided 2026-11-18). Tapping "Leaving home" / "Leaving my desk" opens that direction's legs,
  each a start tap and an end tap with the duration shown once both exist:
  - To work: home → F St stop · Broadway bus stop → Broadway platform · Kendall/MIT platform → desk.
  - Home: desk → Kendall platform · Broadway platform → bus stop · F St stop → home.
  - Optional taps (on bus, on train) feed ride times; "Walking instead" swaps in the walking legs.
  - **A leg missing either tap is scrapped** (feedback.py only learns segments with both ends); the rest of the trip still counts.
  - Learned value is still prior blended toward the user's 75th percentile (not a plain average, so buffers cover
    slower days). User said "average it out"; revisit if they want the mean.
- Trip log steps:
  - Morning: left_home, at_stop, on_bus, off_bus, on_platform, on_train, off_train, at_dest.
  - Evening: left_desk, … , at_dest.
  - Also `walking`, `undo`, notes, and back-dated times. The web app sends `log:<step> @ts=<epoch>`,
    queued offline so underground taps keep their time.
- Learned segments:
  - Morning: door→stop, door→Broadway walking, off bus→NB platform (stairs), Kendall train→desk.
  - Evening: desk→Kendall platform (doors), off train→bus stop, stop→door, Broadway→door walking.
- Estimate = config prior blended toward the user's **75th percentile**, weight n/(n+2). Outliers dropped.
  `set <param> <min>` pins a value.
- Logged ride times are added to the history samples ×5. A trip summary follows each completed trip.

## Files
- `commute/`:
  - `app.py`: orchestration, schedule overrides, watchers, commands, status, `tick`
  - `model.py`: pure planning math
  - `history.py`: TransitMatters fetch + cache
  - `mbta.py`: V3 client
  - `recorder.py`: prediction-error learning
  - `feedback.py`: trip log + learned times
  - `notify.py`: ntfy + web push
  - `statefile.py`: encrypted state
  - `__main__.py`: CLI (`keygen setup night morning home watch watch-home tick plan serve log report
    accuracy state-load state-save`)
- `docs/`: the web app (index.html, sw.js, manifest, icons)
- `.github/workflows/`: `tick.yml` (scheduler, writes state) and `plan.yml` (on-demand, read-only)
- `tests/test_offline.py`: fake MBTA/TM + fake clock, full end-to-end. **Run after every change:**
  `python3 tests/test_offline.py` and `python3 tests/test_watcher.py` (needs `pip install cryptography pywebpush` locally)
- `config.example.toml`: all settings. The real config lives only in the `CONFIG_TOML` Actions secret.

## Status: live 2026-11-18. APIs, GitHub Pages deploy and iPhone web push all verified
Verified live: web push to the iPhone (iOS 18, home-screen app; test push from a laptop arrived), Pages deploy,
plan.yml dispatch from the app's button, MBTA stop lookup, TransitMatters, predictions (incl. CANCELLED/ADDED), ntfy publish/poll.
- **Fixed:** Broadway bus stops have no `parent_station` in the live API (`setup` found none). Now matched by
  name "Broadway Station". Bus stop ids: inbound F St `159`, Broadway in `150`, out `150`+`151`.
- **TM bus history is current** (9 data through the same day, ~100 trips/day, 29 weekdays cached). The synthetic
  fallback is only a safety net. Outbound `9-0-150→9-0-21` is empty; `9-0-151→9-0-157` works (config lists both).
- **Workflows were missing** (CLAUDE.md claimed they existed). Written: `.github/workflows/tick.yml`, `plan.yml`.
- **Privacy decision:** repo is public, so addresses live in gitignored `config.toml` `[places]` and
  `CLAUDE.local.md`. `.gitignore` also covers config, state, sqlite, cache, setup-code.txt, .venv.
- Local deps live in `.venv` (system Python is PEP 668 locked): `.venv/bin/python tests/test_offline.py`.
- Still hardcoded "F St"/"Broadway" labels in feedback.py and docs/index.html (the stop label in app.py
  messages now comes from `bus_stop_match`).

### Next tasks, in order
1. ~~**Go live, verify each external call**~~ **DONE 2026-11-18** (remaining: first `tick` run must consume the
   phone's `subscribe` message so the planner stores it in state/state.enc):
   - `python -m commute setup` resolves the F St stop id.
   - TM `/api/traveltimes` returns data for `70082→70072` and `9-1-45→9-1-150` (check bus-data lag).
   - Predictions and `include=schedule` parse correctly.
   - ntfy poll/publish work.
   - Web push reaches the iPhone (VAPID `subject` must be a real `mailto:`). **Needs deploy first**: push repo,
     add `CONFIG_TOML` secret, enable Pages, install the PWA on the phone, tap Turn on notifications.
2. ~~Fix the morning watcher's flip-flopping~~ **DONE** (`tests/test_watcher.py`, jittery fake predictions, 8 seeds:
   worst case went from 22 messages to 5, 0 plan changes, exactly one "leave in" and one "leave now").
   `watch_morning` now tracks the announced option (`same_option`: same bus trip, or walk for the train within 3 min)
   and keeps it unless it is gone (cancelled/vanished) or another option arrives ≥3 min earlier (≥5 when more than
   20 min from leaving, since beyond ~30 min trains are padded with the typical headway and wobble). Reminders
   ("leave in", "leave now") only refer to the announced option. An option that slips a hair into the past within
   1 min of leaving becomes "leave now" instead of being replaced. Matching ignores the `earliest` cut-off.
   Not covered by a test: a cancelled announced bus mid-watch (code path exists: `cur is None` → "Change of plan").
3. **First week:** watch the Actions logs (quiet mode: no personal data in public logs) and the cron
   start delays. Check the ntfy daily message count stays well under 250.
4. **Iterate on the web app** with the user on their phone (iOS 18 Safari, standalone mode). The user
   is open to more personalization.

### Visual design
Tried a transit-themed restyle (navy brand strip, departure-board "Leave" time, red buttons, rail-style trip legs) on
2026-11-18; **the user found it uglier and had it reverted** (commit f7a1ba4, reverted). The original look stays. The user
likes the layout/flow and loves the app icon. Don't restyle again without asking what specifically to change, and show a
screenshot first (headless Chrome in a 390px iframe works).

### Gotchas
- **Cron** is UTC with up to ~15 min start delay; `tick` tolerates starting 15 min early or late.
- **Concurrency:** only `tick.yml` writes state, so `plan.yml` must stay read-only.
- **iOS web push:** only works when opened from the Home Screen; each push must show a notification
  (sw.js does); no action buttons, so tapping opens the app.
- **ntfy messages** >4 KB become attachments. Status is trimmed, and the app also handles attachments.
- **Public repo:** never commit `config.toml`, `state.json`, `commute.sqlite` (gitignored); state is
  only committed encrypted.
