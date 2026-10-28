"""
Test harness: loads the pyscript app outside Home Assistant.

pyscript injects globals (pyscript, hass, state, task, service, time_trigger,
state_trigger, event_trigger) into each script, so the app can't be imported
normally. The harness compiles the file into a fresh namespace with fakes for
all of those, plus a fake asyncio (instant sleeps), a fake clock, fake HA
services and a fake SolisCloud.
"""
import asyncio
import copy
import json
import logging
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

APP_PATH = Path(__file__).resolve().parents[1] / "pyscript" / "apps" / "solis_octopus.py"
TZ = ZoneInfo("Europe/London")

EVENTS = "event.octopus_energy_a_test_octoplus_power_down_events"
HH_EVENTS = "event.octopus_energy_a_test_octoplus_power_up_events"
HH_TOKENS = "sensor.octopus_energy_a_test_octoplus_weekend_happy_hours"
SOC = "sensor.solis_battery_soc"
AXLE_SENSORS = ["sensor.axle_energy_event_type", "sensor.axle_energy_event_start",
                "sensor.axle_energy_event_end"]
LOAD = "sensor.solis_house_load"
CHARGER_SWITCH = "switch.hypervolt_charging"
CHARGER_CURRENT = "sensor.hypervolt_charger_current"
DISPATCH = "binary_sensor.octopus_energy_test_intelligent_dispatching"
WATER_HEATER = "water_heater.octopus_energy_heat_pump_abc"

BASE_CONFIG = {
    "key_id": "test-key",
    "secret": "test-secret",
    "username": "user@example.com",
    "password": "hunter2",
    "plantId": "1234",
    "inverter_timezone": "Europe/London",
    "inter_write_delay": 0,
    "battery": {
        "soc_sensor": SOC,
        "capacity_kwh": 10,
        "floor_soc": 10,
        "reserve_margin": 1.0,
        "default_load_kw": 0.5,
        "voltage": 50,
        "max_discharge_kw": 5,
        "max_discharge_amps": 100,
        "export_limit_kw": 5,
        "discharge_efficiency": 1.0,
    },
    "power_down": {"power_down_events_entity": EVENTS},
}

EV_CONFIG = {"ev_follow": {"dispatch_sensor": DISPATCH}}
HH_CONFIG = {"happy_hour": {"power_up_events_entity": HH_EVENTS, "heat_pump_id": "abc"}}

# The Power Down app's flat config keys, so its tests keep working unchanged.
LEGACY_KEYS = {
    "power_down_events_entity": ("power_down", "power_down_events_entity"),
    "auto_join": ("power_down", "auto_join"),
    "soc_sensor": ("battery", "soc_sensor"),
    "battery_capacity_kwh": ("battery", "capacity_kwh"),
    "floor_soc": ("battery", "floor_soc"),
    "reserve_margin": ("battery", "reserve_margin"),
    "core_charge_soc": ("battery", "core_charge_soc"),
    "load_power_sensor": ("battery", "load_power_sensor"),
    "history_days": ("battery", "history_days"),
    "default_load_kw": ("battery", "default_load_kw"),
    "solar_forecast_sensors": ("battery", "solar_forecast_sensors"),
    "battery_voltage_sensor": ("battery", "voltage_sensor"),
    "battery_voltage": ("battery", "voltage"),
    "max_discharge_kw": ("battery", "max_discharge_kw"),
    "max_discharge_amps": ("battery", "max_discharge_amps"),
    "export_limit_kw": ("battery", "export_limit_kw"),
    "discharge_efficiency": ("battery", "discharge_efficiency"),
    "min_export_kw": ("battery", "min_export_kw"),
    "current_step_amps": ("battery", "current_step_amps"),
    "prepare_minutes": ("battery", "prepare_minutes"),
    "discharge_slot": ("slots", "export"),
    "peak_export": ("peak_export", "enabled"),
    "peak_window": ("peak_export", "window"),
    "peak_export_rate": ("peak_export", "rate"),
    "peak_export_switch": ("peak_export", "switch"),
    "notify_peak": ("peak_export", "notify"),
    "axle_events": ("axle", "enabled"),
    "axle_grace_minutes": ("axle", "grace_minutes"),
    "axle_event_type_sensor": ("axle", "event_type_sensor"),
    "axle_event_start_sensor": ("axle", "event_start_sensor"),
    "axle_event_end_sensor": ("axle", "event_end_sensor"),
}
SECTIONS = ("battery", "ev_follow", "happy_hour", "power_down", "peak_export", "axle", "slots")

# Duplicated from the app on purpose, so a wrong CID in the app fails the tests.
TIME_CID, CURRENT_CID, SOC_CID, SWITCH_CID = "5964", "5967", "5965", "5922"
CHARGE_TIME_CIDS = ["5946", "5949", "5952", "5955", "5958", "5961"]
CHARGE_CURRENT_CIDS = ["5948", "5951", "5954", "5957", "5960", "5963"]
CHARGE_SOC_CIDS = ["5928", "5929", "5930", "5931", "5932", "5933"]
CHARGE_SWITCH_CIDS = ["5916", "5917", "5918", "5919", "5920", "5921"]
DISCHARGE_SWITCH_CIDS = ["5922", "5923", "5924", "5925", "5926", "5927"]
SLOT_SWITCH_CIDS = CHARGE_SWITCH_CIDS + DISCHARGE_SWITCH_CIDS


# Keep the app's log output out of the test run (assertLogs still works).
_app_log = logging.getLogger("pyscript.solis_octopus")
_app_log.addHandler(logging.NullHandler())
_app_log.propagate = False


def _install_homeassistant_stub():
    if "homeassistant.helpers.aiohttp_client" in sys.modules:
        return
    ha = types.ModuleType("homeassistant")
    helpers = types.ModuleType("homeassistant.helpers")
    client = types.ModuleType("homeassistant.helpers.aiohttp_client")
    client.async_get_clientsession = lambda hass: None
    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.aiohttp_client"] = client


def build_config(*overrides):
    """BASE_CONFIG with overrides merged in. Flat Power Down keys go to their section; None deletes."""
    cfg = copy.deepcopy(BASE_CONFIG)
    for override in overrides:
        for key, value in (override or {}).items():
            if key in SECTIONS and isinstance(value, dict):
                section = cfg.setdefault(key, {})
                for k, v in value.items():
                    if v is None:
                        section.pop(k, None)
                    else:
                        section[k] = v
                continue
            if key in SECTIONS and value is None:
                cfg.pop(key, None)
                continue
            if key in LEGACY_KEYS:
                section, name = LEGACY_KEYS[key]
                target = cfg.setdefault(section, {})
            else:
                target, name = cfg, key
            if value is None:
                target.pop(name, None)
            else:
                target[name] = value
    return cfg


class FakeState:
    def __init__(self, value, attributes=None):
        self.state = value
        self.attributes = attributes or {}


class FakeAsyncio:
    """Stands in for the asyncio module inside the app: sleeps are instant and recorded."""

    def __init__(self):
        self.sleeps = []
        self.on_sleep = None

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        if self.on_sleep:
            self.on_sleep(seconds)


class FakeServices:
    def __init__(self):
        self.calls = []
        self.fail = set()

    async def async_call(self, domain, service, data=None, blocking=False, target=None):
        self.calls.append({"domain": domain, "service": service, "data": data or {}, "target": target})
        if (domain, service) in self.fail:
            raise RuntimeError("service failed")

    def joins(self):
        return [c["data"]["event_code"] for c in self.calls
                if c["service"] == "join_octoplus_power_down_session_event"]

    def hh_joins(self):
        return [c["data"]["event_code"] for c in self.calls
                if c["service"] == "join_octoplus_weekend_happy_hour_event"]

    def notifications(self):
        return [c for c in self.calls if c["domain"] == "notify"]

    def boosts(self):
        return [c for c in self.calls if c["service"] == "boost_water_heater"]

    def titles(self):
        return [c["data"]["title"] for c in self.notifications()]

    def others(self):
        return [(c["domain"], c["service"]) for c in self.calls
                if c["domain"] not in ("notify", "octopus_energy")]


# -----------------------------
# Fake SolisCloud
# -----------------------------
OK_CONTROL = {"code": "0", "data": [{"code": "0"}]}


def storage_inverter(inv_id="111", sn="SN111", name="Solis"):
    return {"id": inv_id, "sn": sn, "name": name, "productModel": "2"}


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status = status
        self._text = body if isinstance(body, str) else json.dumps(body if body is not None else {})

    async def text(self):
        return self._text


class FakeSolisCloud:
    """Records every POST and answers like SolisCloud."""

    def __init__(self):
        self.calls = []
        self.records = [storage_inverter()]
        self.fail_cids = set()
        self.queued = {}
        self.values = {cid: "0" for cid in SLOT_SWITCH_CIDS}
        self.values["636"] = "35"

    async def post(self, url, data=None, headers=None):
        path = url.split(":13333", 1)[1]
        body = json.loads(data)
        self.calls.append({"path": path, "body": body, "headers": headers or {}})
        if self.queued.get(path):
            return self.queued[path].pop(0)
        if path == "/v2/api/login":
            return FakeResponse(200, {"csrfToken": "tok-123"})
        if path == "/v1/api/inverterList":
            return FakeResponse(200, {"data": {"page": {"records": self.records}}})
        if path == "/v2/api/control":
            if str(body["cid"]) in self.fail_cids:
                return FakeResponse(500, {})
            self.values[str(body["cid"])] = str(body["value"])
            return FakeResponse(200, OK_CONTROL)
        if path == "/v2/api/atRead":
            return FakeResponse(200, {"code": "0", "data": {"msg": self.values.get(str(body["cid"]), "")}})
        if path == "/v2/api/atReadBatch":
            items = [{"cid": int(cid), "msg": self.values.get(cid, "")} for cid in body["cids"].split(",")]
            return FakeResponse(200, {"code": "0", "data": [items]})
        return FakeResponse(404, {})

    def paths(self):
        return [c["path"] for c in self.calls]

    def control_writes(self):
        """(cid, value, yuanzhi) for every control write in order, excluding the time sync."""
        out = []
        for c in self.calls:
            if c["path"] == "/v2/api/control" and c["body"].get("cid") != "56":
                out.append((c["body"]["cid"], c["body"]["value"], c["body"].get("yuanzhi")))
        return out

    def logins(self):
        return [c for c in self.calls if c["path"] == "/v2/api/login"]

    def time_syncs(self):
        return [c for c in self.calls if c["path"] == "/v2/api/control" and c["body"].get("cid") == "56"]


# -----------------------------
# The loaded app
# -----------------------------
class App:
    def __init__(self, config=None, *more, started=True, raw=None):
        _install_homeassistant_stub()
        cfg = raw if raw is not None else build_config(config, *more)

        self.states = {}
        self.state_sets = []
        self.triggers = []
        self.service_funcs = []
        self.unique_calls = []
        self.asyncio = FakeAsyncio()
        self.cloud = FakeSolisCloud()
        self.services = FakeServices()
        self.ops = None  # set by capture_ops()

        hass = types.SimpleNamespace(
            config=types.SimpleNamespace(time_zone="Europe/London"),
            states=types.SimpleNamespace(get=self._get_state),
            services=self.services,
        )
        g = {
            "__name__": "solis_octopus",
            "pyscript": types.SimpleNamespace(app_config=cfg, config={}),
            "hass": hass,
            "state": types.SimpleNamespace(set=self._set_state),
            "task": types.SimpleNamespace(unique=self._unique),
            "service": self._service,
            "time_trigger": self._trigger("time_trigger"),
            "state_trigger": self._trigger("state_trigger"),
            "event_trigger": self._trigger("event_trigger"),
        }
        source = APP_PATH.read_text()
        exec(compile(source, str(APP_PATH), "exec"), g)

        g["asyncio"] = self.asyncio
        g["async_get_clientsession"] = lambda _hass: self.cloud
        self.load_profile = {}

        async def fake_fetch():
            return dict(self.load_profile)

        g["fetch_load_profile"] = fake_fetch
        g["_flags"]["started"] = started
        self.g = g
        self.set_soc(80)
        self.at("12:00")

    # --- pyscript / HA fakes ---
    def _get_state(self, entity_id):
        if entity_id in self.states:
            value, attrs = self.states[entity_id]
            return FakeState(value, attrs)
        return None

    def _set_state(self, entity_id, value, new_attributes=None):
        self.state_sets.append((entity_id, value, new_attributes or {}))

    def _unique(self, name, kill_me=False):
        self.unique_calls.append((name, kill_me))

    def _service(self, func):
        self.service_funcs.append(func.__name__)
        return func

    def _trigger(self, kind):
        def decorator_factory(*args, **kwargs):
            def decorator(func):
                self.triggers.append((kind, args, func.__name__))
                return func
            return decorator
        return decorator_factory

    # --- helpers for tests ---
    def __getattr__(self, name):
        g = self.__dict__.get("g")
        if g is not None and name in g:
            return g[name]
        raise AttributeError(name)

    def set_soc(self, soc):
        self.states[SOC] = (str(soc), {})

    def set_events(self, joined=(), available=()):
        self.states[EVENTS] = ("2026-10-06T10:00:00+00:00",
                               {"joined_events": list(joined), "available_events": list(available)})

    def set_happy_hours(self, booked=(), available=(), tokens=None):
        self.states[HH_EVENTS] = ("2026-10-06T10:00:00+00:00",
                                  {"events": list(booked), "available_events": list(available)})
        if tokens is not None:
            self.states[HH_TOKENS] = (str(tokens), {})

    def set_charger(self, switch="on", amps="16", dispatch="on"):
        for entity, value in ((CHARGER_SWITCH, switch), (CHARGER_CURRENT, amps), (DISPATCH, dispatch)):
            if value is None:
                self.states.pop(entity, None)
            else:
                self.states[entity] = (value, {})

    def set_axle(self, kind=None, start=None, end=None, day=6):
        """Axle Energy sensors; kind None means no event (all 'unknown')."""
        if kind is None:
            values = ("unknown", "unknown", "unknown")
        else:
            values = (kind, local(start, day).astimezone(timezone.utc).isoformat(),
                      local(end, day).astimezone(timezone.utc).isoformat())
        for entity, value in zip(AXLE_SENSORS, values):
            self.states[entity] = (value, {})

    def at(self, hhmm, day=6, second=0):
        """Freeze the app's clock at 2026-10-<day> HH:MM (Europe/London)."""
        now = local(hhmm, day).replace(second=second)
        self.g["now_local"] = lambda: now
        return now

    def known(self, *keys):
        """Pretend these slots (default: all) are known to be empty on the inverter."""
        for key in keys or self.g["_slots"].keys():
            self.g["_slots"][key]["unknown"] = False
        return self

    def capture_ops(self, ok=True):
        """Replace the SolisCloud layer with a recorder of the batches the app applies."""
        self.ops = []
        results = list(ok) if isinstance(ok, (list, tuple)) else None

        async def fake_apply(ops):
            if ops:
                self.ops.append(list(ops))
            if results is not None:
                return results.pop(0) if results else True
            return ok

        self.g["solis_apply"] = fake_apply
        return self.ops

    def run(self, coro):
        return asyncio.run(coro)

    def reconcile(self, reason="test"):
        return self.run(self.g["reconcile"](reason))

    def _last(self, entity):
        sets = [s for s in self.state_sets if s[0] == entity]
        return sets[-1] if sets else None

    def status(self):
        return self._last("sensor.solis_power_down")

    def active(self):
        s = self._last("binary_sensor.solis_power_down_active")
        return s[1] if s else None

    def ev_status(self):
        return self._last("sensor.solis_ev_charge_slot")

    def hh_status(self):
        return self._last("sensor.octopus_happy_hour")

    def hh_active(self):
        s = self._last("binary_sensor.octopus_happy_hour_active")
        return s[1] if s else None

    def overview(self):
        return self._last("sensor.solis_octopus")

    def slot(self, key):
        return self.g["_slots"][key]["window"]


def local(hhmm, day=6):
    hour, minute = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


def event(start, end, eid="101", code="PD101", ppk=800, day=6):
    """A power down event dict as the Octopus integration exposes it (UTC ISO strings)."""
    return {
        "id": eid,
        "code": code,
        "start": local(start, day).astimezone(timezone.utc).isoformat(),
        "end": local(end, day).astimezone(timezone.utc).isoformat(),
        "duration_in_minutes": 60,
        "octopoints_per_kwh": ppk,
    }


def hh_event(start, end, eid="201", code="HH201", day=10, availability=""):
    """A power up / happy hour event dict (UTC ISO strings). Day 10 Oct 2026 is a Saturday."""
    return {
        "id": eid,
        "code": code,
        "start": local(start, day).astimezone(timezone.utc).isoformat(),
        "end": local(end, day).astimezone(timezone.utc).isoformat(),
        "availability": availability,
    }


def session(start, end, ppk=800, day=6):
    return {"id": "1", "code": "PD1", "start": local(start, day), "end": local(end, day),
            "octopoints_per_kwh": ppk, "joined": True, "kind": "power_down"}
