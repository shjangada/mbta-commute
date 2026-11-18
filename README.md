# mbta-commute

A personal commute planner (vibe coded!) (9 bus + Red Line, Boston ⇄ Cambridge). Set your start and end addresses in the
`[places]` section of `config.toml` (gitignored; copy `config.example.toml`).
- **Night before:** "be ready by…".
- **Mornings and evenings:** live alerts starting an hour before you leave, with the best
  bus/train combination.
- **Trip log:** learns your real door, stair and walking times.

It runs free on GitHub Actions, and your phone uses a home-screen web app hosted on GitHub Pages.
Project context and design decisions: [CLAUDE.md](CLAUDE.md).

## Setup (about 20 min)
1. **Get an MBTA API key** (free) at https://api-v3.mbta.com/register.
2. **Make your config.** Locally: `pip install cryptography pywebpush`, then
   `cp config.example.toml config.toml` and run `python3 -m commute keygen`. Paste its `[ntfy]`, `[webpush]`
   and `[actions]` sections into `config.toml`. Add your MBTA key and a real email in `[webpush] subject`.
   Save the **setup code** it prints for step 5.
3. **Test it locally** (needs internet):
   - `python3 -m commute setup` checks it found your F St stop.
   - `python3 -m commute night --dry-run` and `python3 -m commute plan morning` show sample output.
4. **Set up GitHub.**
   - Settings → Secrets and variables → Actions → new secret `CONFIG_TOML`, containing your whole config.toml.
   - Settings → Pages → deploy from branch `main`, folder `/docs`.
   - The Actions tab: enable workflows, then run **tick** once by hand.
5. **On your iPhone:**
   - Open `https://<you>.github.io/mbta-commute/` in Safari, then Share → Add to Home Screen.
   - Open it **from the Home Screen** and go to Settings.
   - Paste the setup code. Optionally add `you/mbta-commute` and a fine-grained token (this repo only,
     Actions: read and write) for instant "Find my optimal path".
   - Tap **Turn on notifications**.

Run all offline tests with `python3 tests/test_offline.py`.

## Using it
- **Days tab:** change in-by and leave-work times for today or tomorrow, or mark no commute.
- **Trip tab:** tap as each thing happens for the first few trips. Taps made underground send later with
  their real times.
- **Now tab:** the live plan, and **Find my optimal path** whenever you want a fresh answer.
