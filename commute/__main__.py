import argparse
import base64
import datetime as dt
import json
import os
import secrets
import sys
import time


def keygen():
    """Everything secret you need, generated locally."""
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    k = ec.generate_private_key(ec.SECP256R1())
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
    priv = b64(k.private_numbers().private_value.to_bytes(32, "big"))
    pub = b64(k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    base = "commute-" + secrets.token_hex(8)
    topics = {"topic": base, "command_topic": base + "-cmd", "status_topic": base + "-status"}
    print("# ---- paste into config.toml ----")
    print("[ntfy]\nserver = \"https://ntfy.sh\"")
    for kk, v in topics.items():
        print(f'{kk} = "{v}"')
    print(f'\n[webpush]\nvia = "auto"\nprivate_key = "{priv}"\npublic_key = "{pub}"\nsubject = "mailto:you@example.com"')
    print(f'\n[actions]\nstate_key = "{Fernet.generate_key().decode()}"')
    code = b64(json.dumps({"server": "https://ntfy.sh", "cmd": topics["command_topic"],
                           "status": topics["status_topic"]}).encode())
    print(f"\n# ---- web app setup code (paste into the app's Settings) ----\n{code}")


def main():
    ap = argparse.ArgumentParser(prog="python -m commute")
    ap.add_argument("cmd", choices=["setup", "night", "morning", "home", "watch", "watch-home", "tick", "plan",
                                    "serve", "log", "report", "accuracy", "keygen", "state-load", "state-save"])
    ap.add_argument("arg", nargs="?", help="log: text like \"on platform @9:14\"; plan: morning|home; night: YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true", help="print notifications instead of sending them")
    ap.add_argument("--config", default="config.toml")
    a = ap.parse_args()
    if a.cmd == "keygen":
        return keygen()
    if a.cmd in ("state-load", "state-save"):
        import tomllib
        from . import statefile
        key = os.environ.get("STATE_KEY") or tomllib.load(open(a.config, "rb"))["actions"]["state_key"]
        if a.cmd == "state-save":
            statefile.save(key)
        else:
            statefile.load(key)
        return

    from .app import App
    app = App(a.config, dry_run=a.dry_run, readonly=(a.cmd == "plan"))
    if a.cmd == "setup":
        app.setup()
    elif a.cmd == "night":
        app.night(dt.date.fromisoformat(a.arg) if a.arg else None)
    elif a.cmd == "morning" or (a.cmd == "plan" and a.arg != "home"):
        app.push_morning_plan()
    elif a.cmd == "home" or a.cmd == "plan":
        app.home_plan()
    elif a.cmd == "watch":
        app.watch_morning()
    elif a.cmd == "watch-home":
        app.watch_home()
    elif a.cmd == "tick":
        app.tick()
    elif a.cmd == "log":
        r = app.fb.handle(a.arg or "", time.time(), app.plan_hook) or (app.schedule_cmd(a.arg.lower()) and {
            "title": "Schedule updated", "message": ""})
        print(r["title"] + "\n" + r["message"] if r else "Not understood.")
        app.save_state()
    elif a.cmd == "report":
        app.c  # noqa: B018
        print(app.fb.report())
    elif a.cmd == "accuracy":
        from .recorder import ErrorModel
        print(ErrorModel(app.rec).summary())
    else:
        app.serve()


sys.exit(main())
