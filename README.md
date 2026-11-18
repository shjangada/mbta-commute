# mbta-commute

A personal commute planner (vibe coded!) (9 bus + Red Line, Boston ⇄ Cambridge). Set your start and end addresses in the
`[places]` section of `config.toml` (gitignored; copy `config.example.toml`).
- **Night before:** "be ready by…".
- **Mornings and evenings:** live alerts starting an hour before you leave.
  - To work: the best bus/train combination.
  - Way home: the Red Line train that puts a 9 bus at the stop within ~2 min of you arriving (least waiting).
    It also tells you if walking is faster when you leave right now.
- **Trip log:** learns your real door, stair and walking times, leg by leg.
- **"I want to leave ASAP":** one tap and it plans for leaving now, not at your usual time.

It runs free on GitHub Actions, and your phone uses a home-screen web app hosted on GitHub Pages.
Project context and design decisions: [CLAUDE.md](CLAUDE.md).

## Setup (about 20 min)
1. **Get an MBTA API key** (free) at https://api-v3.mbta.com/register.
2. **Make your config.** Locally (macOS blocks `pip install` on system Python, so use a venv):
   `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`, then
   `cp config.example.toml config.toml` and run `.venv/bin/python -m commute keygen`. Paste its `[ntfy]`, `[webpush]`
   and `[actions]` sections into `config.toml`. Add your MBTA key, your addresses under `[places]`, and a real email in
   `[webpush] subject` (Apple's push service sees it). Save the **setup code** it prints for step 5.
   `config.toml` and your setup code are gitignored. Never commit them (the repo is public).
3. **Test it locally** (needs internet):
   - `.venv/bin/python -m commute setup` checks it found your home bus stop (set `bus_stop_match` to a word in its name,
     e.g. "F St") and the Broadway stops.
   - `.venv/bin/python -m commute night --dry-run` and `.venv/bin/python -m commute plan morning` show sample output.
4. **Set up GitHub.**
   - Settings → Secrets and variables → Actions → new secret `CONFIG_TOML`, containing your whole config.toml
     (`gh secret set CONFIG_TOML < config.toml`). Update it whenever config.toml changes.
   - **In the repo's** Settings → Pages (not your account settings; no custom domain needed): deploy from branch
     `main`, folder `/docs`.
   - The Actions tab: enable workflows, then run **tick** once by hand.
5. **On your iPhone:**
   - Open `https://<you>.github.io/mbta-commute/` in Safari, then Share → Add to Home Screen.
   - Open it **from the Home Screen**, then tap the **gear icon** (top right) for Settings.
   - Paste the setup code. For instant results, add `you/mbta-commute` and a fine-grained token (this repo only,
     Actions: read and write). The token powers "Find my optimal path" and "I want to leave ASAP".
   - Tap **Turn on notifications**.

## Using it
- **Now tab:** the live plan. Switch **To work** / **Way home** (it slides).
  - **Find my optimal path** / **Find my best way home:** a fresh answer right now. The way-home view normally shows
    the plan for your leave-work time (5:30); after you press the button it shows the leave-now plan for an hour, then
    goes back.
  - **⚡ I want to leave ASAP:** plans for leaving within a few minutes and starts watching right away.
    "Back to default" on today's card in Days cancels it.
- **Trip tab:** tap **Leaving home** or **Leaving my desk**, then tap the start and end of each leg as it happens
  (e.g. off the train at Broadway → at the bus stop). Only legs with both taps count. Taps made underground send later
  with their real times.
- **Days tab:** tap a time to change in-by or leave-work for today or tomorrow. It saves by itself. "↺ Default" resets.
- **Learned tab:** every leg's time (icons, trips logged, how far from your starting guess). Values update as you log trips.
- **Settings (gear):** setup code, GitHub repo and token, notifications. Kept out of the way on purpose.

## Updating
- Push to `main`. The Python planner updates on the next **tick** run. The web app updates when GitHub Pages rebuilds
  (about a minute). Then fully close the app and reopen it (twice if needed).
- If you change `config.toml`, update the `CONFIG_TOML` secret too.

## Tests
No network needed. Use the venv Python:
- `python3 tests/test_offline.py`: end to end with fake MBTA/TransitMatters data and a fake clock.
- `python3 tests/test_watcher.py`: morning watcher stability with jittery predictions, plus the ASAP button.
- `python3 tests/test_triplog.py`: trip log leg learning.
- `python3 tests/test_home.py`: way-home choice (a bus within 2 min beats a faster walk).
- `node tests/test_ui.js <dir containing jsdom>`: the web app in a headless DOM (`npm i jsdom` in any folder).
