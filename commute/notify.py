"""How messages reach you, and how your phone talks back.

ntfy (https://ntfy.sh) is a free, open-source "post a message to a URL, it shows up on your phone"
service. Here it does two jobs:
  * a mailbox: your phone (the web app, or ntfy buttons) posts commands/taps to the *command topic*;
    the planner reads them. The planner posts its current plan as JSON to the *status topic*;
    the web app reads that.
  * optionally, notifications via the ntfy app (if you don't use the web app's own notifications).
Web push = notifications to the home-screen web app itself (iOS 16.4+), sent with your own keys.
"""
import json
import time
import urllib.request

from .util import log


class Ntfy:
    def __init__(self, server, topic, command_topic, status_topic="", token="", dry_run=False):
        self.server = server.rstrip("/")
        self.topic, self.cmd, self.status_topic = topic, command_topic, status_topic
        self.token, self.dry = token, dry_run

    def _headers(self, ctype="application/json"):
        h = {"Content-Type": ctype}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def button(self, label, command, clear=False):
        return {"action": "http", "label": label, "url": f"{self.server}/{self.cmd}",
                "method": "POST", "body": command, "clear": clear}

    def push(self, title, message, priority=3, tags=None, buttons=()):
        body = {"topic": self.topic, "title": title, "message": message,
                "priority": priority, "tags": tags or [], "actions": list(buttons)[:3]}
        self._post(self.server, json.dumps(body).encode(), self._headers())

    def publish_raw(self, topic, text):
        if self.dry:
            self.last_status = text
            return
        self._post(f"{self.server}/{topic}", text.encode(), self._headers("text/plain"))

    def _post(self, url, data, headers):
        if self.dry:
            return
        try:
            urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method="POST"),
                                   timeout=15).read()
        except Exception as e:
            log(f"[ntfy] publish failed: {e}")

    def poll(self, topic, since):
        """All cached messages after `since` (a message id, or e.g. '12h'). Returns [(id, time, text)]."""
        if self.dry:
            return []
        url = f"{self.server}/{topic}/json?poll=1&since={since}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=self._headers()), timeout=20) as r:
                out = []
                for line in r:
                    ev = json.loads(line)
                    if ev.get("event") == "message":
                        out.append((ev["id"], ev.get("time") or time.time(), ev.get("message", "")))
                return out
        except Exception as e:
            log(f"[ntfy] poll failed: {e}")
            return []


class Notifier:
    """Sends each alert to the web app (web push) and/or the ntfy app, per config `[webpush] via`:
    'auto' = web push once the web app has subscribed, ntfy until then; 'ntfy'; 'webpush'; 'both'."""

    def __init__(self, ntfy: Ntfy, wp_cfg: dict, state: dict, dry_run=False):
        self.ntfy, self.wp, self.state, self.dry = ntfy, wp_cfg or {}, state, dry_run

    def push(self, title, message, priority=3, tags=None, buttons=(), tag=None):
        if self.dry:
            print(f"\n=== [{priority}] {title}\n{message}\n   buttons: {[b['label'] for b in buttons]}")
            return
        subs = self.state.get("webpush_subs", [])
        via = self.wp.get("via", "auto")
        if subs and via in ("auto", "webpush", "both"):
            self._webpush(subs, {"title": title, "body": message, "tag": tag or "commute",
                                 "urgent": priority >= 5, "t": int(time.time())})
        if via in ("ntfy", "both") or (via == "auto" and not subs):
            self.ntfy.push(title, message, priority, tags, buttons)

    def _webpush(self, subs, payload):
        try:
            from pywebpush import WebPushException, webpush
        except ImportError:
            log("[webpush] pywebpush not installed; `pip install pywebpush`")
            return
        keep = []
        for sub in subs:
            try:
                webpush(subscription_info=sub, data=json.dumps(payload), ttl=900,
                        vapid_private_key=self.wp["private_key"],
                        vapid_claims={"sub": self.wp.get("subject", "mailto:commute@example.com")},
                        headers={"Urgency": "high" if payload["urgent"] else "normal"})
                keep.append(sub)
            except WebPushException as e:
                code = getattr(e.response, "status_code", None)
                if code not in (404, 410):      # 404/410 = phone unsubscribed; drop it
                    keep.append(sub)
                log(f"[webpush] failed ({code})")
            except Exception as e:
                keep.append(sub)
                log(f"[webpush] error: {e}")
        self.state["webpush_subs"] = keep
