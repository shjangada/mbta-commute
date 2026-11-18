"""Thin client for the MBTA V3 API (https://api-v3.mbta.com). Free key: https://api-v3.mbta.com/register"""
import json
import urllib.parse
import urllib.request

from .util import parse_iso


class MBTA:
    BASE = "https://api-v3.mbta.com"

    def __init__(self, api_key: str = "", timeout: int = 15):
        self.key = api_key
        self.timeout = timeout

    def _get(self, path, params):
        q = urllib.parse.urlencode(params, safe="[],:")
        headers = {"accept": "application/vnd.api+json"}
        if self.key:
            headers["x-api-key"] = self.key
        req = urllib.request.Request(f"{self.BASE}/{path}?{q}", headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.load(r)

    def predictions(self, stop, route, direction):
        """Upcoming predictions at a stop. Cancelled trips come back with rel='CANCELLED' and
        their scheduled time (from the included schedule) so we can say *which* bus died."""
        d = self._get("predictions", {
            "filter[stop]": stop, "filter[route]": route,
            "filter[direction_id]": direction, "include": "schedule",
        })
        sched = {}
        for inc in d.get("included", []):
            if inc["type"] == "schedule":
                a = inc["attributes"]
                sched[inc["id"]] = parse_iso(a.get("departure_time") or a.get("arrival_time"))
        out = []
        for it in d.get("data", []):
            a, rel = it["attributes"], it["relationships"]
            s_ref = (rel.get("schedule") or {}).get("data")
            veh = (rel.get("vehicle") or {}).get("data")
            out.append({
                "trip": rel["trip"]["data"]["id"],
                "dep": parse_iso(a.get("departure_time")),
                "arr": parse_iso(a.get("arrival_time")),
                "sched": sched.get(s_ref["id"]) if s_ref else None,
                "rel": a.get("schedule_relationship"),
                "status": a.get("status"),
                "vehicle": veh["id"] if veh else None,
            })
        return out

    def schedules(self, stop, route, direction, date, min_time, max_time):
        d = self._get("schedules", {
            "filter[stop]": stop, "filter[route]": route, "filter[direction_id]": direction,
            "filter[date]": date, "filter[min_time]": min_time, "filter[max_time]": max_time,
        })
        out = []
        for it in d.get("data", []):
            a = it["attributes"]
            t = parse_iso(a.get("departure_time") or a.get("arrival_time"))
            if t:
                out.append({"trip": it["relationships"]["trip"]["data"]["id"], "time": t})
        return sorted(out, key=lambda x: x["time"])

    def alerts(self, routes, at=None):
        params = {"filter[route]": ",".join(routes)}
        if at:
            params["filter[datetime]"] = at
        d = self._get("alerts", params)
        out = []
        for it in d.get("data", []):
            a = it["attributes"]
            out.append({
                "id": it["id"], "header": a.get("short_header") or a.get("header"),
                "effect": a.get("effect"), "severity": a.get("severity") or 0,
                "lifecycle": a.get("lifecycle"),
                "routes": sorted({e.get("route") for e in a.get("informed_entity", []) if e.get("route")}),
                "stops": sorted({e.get("stop") for e in a.get("informed_entity", []) if e.get("stop")}),
            })
        return out

    def route_stops(self, route, direction):
        d = self._get("stops", {"filter[route]": route, "filter[direction_id]": direction})
        out = []
        for it in d.get("data", []):
            parent = ((it.get("relationships") or {}).get("parent_station") or {}).get("data")
            out.append({"id": it["id"], "name": it["attributes"]["name"],
                        "parent": parent["id"] if parent else None})
        return out
