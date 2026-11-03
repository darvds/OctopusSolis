"""
Test harness: loads the pyscript app outside Home Assistant.

pyscript injects globals (pyscript, hass, state, task, service, time_trigger,
state_trigger, event_trigger) into each script, so the app can't be imported
normally. The harness compiles the file into a fresh namespace with fakes for
all of those, plus a fake asyncio (instant sleeps), a fake clock, fake HA
services and a fake Solis Modbus integration (its time, number and switch
entities).
"""
import asyncio
import copy
import logging
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

# Duplicated from the app on purpose, so a wrong entity ID in the app fails the tests.
PREFIX = "solis"


def charge_slot(n, prefix=PREFIX):
    """((start, end), current, soc, switch) Solis Modbus entities for charge slot n."""
    return ((f"time.{prefix}_grid_time_of_use_charge_start_slot_{n}",
             f"time.{prefix}_grid_time_of_use_charge_end_slot_{n}"),
            f"number.{prefix}_grid_time_of_use_charge_battery_current_slot_{n}",
            f"number.{prefix}_grid_time_of_use_charge_cut_off_soc_slot_{n}",
            f"switch.{prefix}_grid_time_of_use_charging_period_{n}")


def discharge_slot(n, prefix=PREFIX):
    return ((f"time.{prefix}_grid_time_of_use_discharge_start_slot_{n}",
             f"time.{prefix}_grid_time_of_use_discharge_end_slot_{n}"),
            f"number.{prefix}_grid_time_of_use_discharge_battery_current_slot_{n}",
            f"number.{prefix}_grid_time_of_use_discharge_cut_off_soc_slot_{n}",
            f"switch.{prefix}_grid_time_of_use_discharge_period_{n}")


# Discharge slot 1 (the export slot).
EXPORT_TIME, EXPORT_CURRENT, EXPORT_SOC, EXPORT_SWITCH = discharge_slot(1)
CHARGE_TIMES = [charge_slot(n)[0] for n in range(1, 7)]
CHARGE_CURRENTS = [charge_slot(n)[1] for n in range(1, 7)]
CHARGE_SOCS = [charge_slot(n)[2] for n in range(1, 7)]
CHARGE_SWITCHES = [charge_slot(n)[3] for n in range(1, 7)]
DISCHARGE_SWITCHES = [discharge_slot(n)[3] for n in range(1, 7)]


# Keep the app's log output out of the test run (assertLogs still works).
_app_log = logging.getLogger("pyscript.solis_octopus")
_app_log.addHandler(logging.NullHandler())
_app_log.propagate = False


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
        self.modbus = None

    async def async_call(self, domain, service, data=None, blocking=False, target=None):
        if self.modbus is not None and self.modbus.handles(domain, data):
            return self.modbus.call(domain, service, data)
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
# Fake Solis Modbus
# -----------------------------
class FakeModbus:
    """
    The Solis Modbus integration's slot entities, kept in the app's HA states,
    and the time.set_value / number.set_value / switch.turn_on|off calls on them.
    """

    def __init__(self, states, prefix=PREFIX):
        self.states = states
        self.calls = []
        self.fail = set()
        self.entities = set()
        for n in range(1, 7):
            for times, current, soc, switch in (charge_slot(n, prefix), discharge_slot(n, prefix)):
                for t in times:
                    states[t] = ("00:00:00", {})
                states[current] = ("0.0", {})
                states[soc] = ("100.0", {})
                states[switch] = ("off", {})
                self.entities.update(list(times) + [current, soc, switch])

    def handles(self, domain, data):
        return domain in ("time", "number", "switch") and (data or {}).get("entity_id") in self.entities

    def call(self, domain, service, data):
        entity_id = data["entity_id"]
        if entity_id in self.fail:
            raise RuntimeError(f"modbus write to {entity_id} failed")
        if domain == "time":
            value = data["time"]
        elif domain == "number":
            value = str(float(data["value"]))
        else:
            value = "on" if service == "turn_on" else "off"
        self.calls.append((domain, service, entity_id, value))
        self.states[entity_id] = (value, {})

    def set(self, entity_id, value):
        self.states[entity_id] = (value, {})

    def writes(self):
        """(entity_id, value) for every write, in order: times as HH:MM, numbers as ints, switches as on/off."""
        out = []
        for domain, _, entity_id, value in self.calls:
            if domain == "time":
                value = value[:5]
            elif domain == "number":
                value = str(int(float(value))) if float(value).is_integer() else value
            out.append((entity_id, value))
        return out

    def written(self):
        return [w[0] for w in self.writes()]


# -----------------------------
# The loaded app
# -----------------------------
class App:
    def __init__(self, config=None, *more, started=True, raw=None):
        cfg = raw if raw is not None else build_config(config, *more)

        self.states = {}
        self.state_sets = []
        self.triggers = []
        self.service_funcs = []
        self.unique_calls = []
        self.asyncio = FakeAsyncio()
        self.services = FakeServices()
        self.modbus = FakeModbus(self.states)
        self.services.modbus = self.modbus
        self.ops = None  # set by capture_ops()

        hass = types.SimpleNamespace(
            config=types.SimpleNamespace(time_zone="Europe/London"),
            states=types.SimpleNamespace(get=self._get_state, async_entity_ids=self._entity_ids),
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

    def _entity_ids(self, domain=None):
        return [e for e in self.states if domain is None or e.startswith(domain + ".")]

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
        """Replace the Solis Modbus layer with a recorder of the batches the app applies."""
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
