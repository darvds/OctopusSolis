"""
Compatibility layer so the Happy Hour app's tests run against the combined
app with as few edits as possible: its flat config keys go to the
`happy_hour:` section, and its helpers / names map onto the combined ones.
"""
from datetime import datetime, timezone

from tests import harness
from tests.harness import CHARGE_CURRENT_CIDS, CHARGE_SOC_CIDS, CHARGE_SWITCH_CIDS, CHARGE_TIME_CIDS, TZ

EVENTS = harness.HH_EVENTS
TOKENS = harness.HH_TOKENS
WATER_HEATER = "water_heater.octopus_energy_heat_pump_hp1"

# Charge slot 3 (the default).
TIME_CID, CURRENT_CID, SOC_CID, SWITCH_CID = (CHARGE_TIME_CIDS[2], CHARGE_CURRENT_CIDS[2],
                                              CHARGE_SOC_CIDS[2], CHARGE_SWITCH_CIDS[2])

HH_KEYS = {
    "power_up_events_entity", "weekend_happy_hours_entity", "auto_join", "join_selection",
    "battery_charge", "charge_current", "charge_soc", "prepare_minutes", "heat_pump_id",
    "water_heater_entity", "hot_water_boost", "hot_water_temperature", "start_actions",
    "end_actions", "notify_scheduled", "notify_end",
}

BASE = {
    "power_down": None,
    "notify_service": "notify.mobile_app_phone",
    "happy_hour": {"power_up_events_entity": EVENTS, "heat_pump_id": "hp1"},
}


def _translate(config):
    out = {"happy_hour": {}}
    for key, value in (config or {}).items():
        if key in HH_KEYS:
            out["happy_hour"][key] = value
        elif key == "charge_slot":
            out["slots"] = {"happy_hour": value}
        else:
            out[key] = value
    return out


class App(harness.App):
    def __init__(self, config=None, started=True):
        super().__init__(BASE, _translate(config), started=started)
        self.states[WATER_HEATER] = ("heat_pump", {"temperature": 45})
        self.at("12:00")
        # Old names, so the ported tests read the same.
        g = self.g
        g.setdefault("sessions", g["hh_sessions"])
        g.setdefault("target_window", g["hh_target_window"])
        g.setdefault("seed", g["hh_seed"])
        g.setdefault("auto_join", g["hh_auto_join"])
        g.setdefault("BATTERY_CHARGE", g["HH_BATTERY_CHARGE"])
        g.setdefault("TOKENS_ENTITY", g["HH_TOKENS_ENTITY"])

    def set_events(self, booked=(), available=()):
        self.states[EVENTS] = ("2026-10-31T10:00:00+00:00",
                               {"events": list(booked), "available_events": list(available)})

    def set_tokens(self, tokens):
        self.states[TOKENS] = (str(tokens), {})

    def at(self, hhmm, day=31, second=0):
        return super().at(hhmm, day=day, second=second)

    def status(self):
        return self.hh_status()

    def active(self):
        return self.hh_active()


def local(hhmm, day=31):
    hour, minute = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


def booked(start, end, eid="201", day=31, as_datetime=True):
    """A booked power up event as the integration exposes it (local datetimes, no code)."""
    s, e = local(start, day), local(end, day)
    if not as_datetime:
        s, e = s.astimezone(timezone.utc).isoformat(), e.astimezone(timezone.utc).isoformat()
    return {"id": eid, "code": None, "start": s, "end": e,
            "duration_in_minutes": int((local(end, day) - local(start, day)).total_seconds() // 60)}


def available(start, end, eid="301", code="HH301", availability="AVAILABLE", day=31):
    ev = booked(start, end, eid=eid, day=day)
    ev["code"] = code
    ev["availability"] = availability
    return ev
