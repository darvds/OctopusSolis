"""
Solis Octopus (pyscript app)

One app that owns the Solis battery for everything Octopus (and Axle) related,
so features never overwrite each other on the inverter:

- EV-Follow: charge the battery from the grid only while the car is really
  being smart-charged by Octopus Intelligent (charge slot 2), with the core
  overnight window kept in charge slot 1.
- Happy Hour: charge at full speed during Octoplus Happy Hours / Power Ups
  (charge slot 3), boost the Cosy hot water, run your start/end actions.
  Optionally move a Renault car's charge schedule into the happy hour and
  back to its normal window (e.g. 23:30-05:30) afterwards.
- Power Down: export during Octoplus Power Down sessions, sized so the battery
  still covers the house until the cheap overnight window (discharge slot 1).
- Peak export: the same, every day in a peak export window (e.g. 16:00-19:00).
- Axle Energy: Axle drives the battery itself during its events; the app holds
  battery back beforehand and keeps off the inverter while they run.

Priority, decided once per pass for all slots together:
    1. Axle event (and grace period): no inverter writes at all.
    2. EV-Follow charging: pauses any export it overlaps.
    3. Octopus Power Down export.
    4. Happy Hour / Power Up charging: cancels overlapping peak export.
    5. Daily peak export.
    6. Core overnight window: always kept.

All inverter writes go through one place, locally over Modbus via the Solis
Modbus integration's Home Assistant entities (time, number and switch), with
one lock around every batch so writes never interleave. Nothing goes through
SolisCloud.

Requires the Solis Modbus integration (github.com/Pho3niX90/solis_modbus) on
a hybrid inverter with "Grid Time of Use" (V2, 6-slot) charge/discharge slots,
time-of-use enabled, the Octopus Energy integration, and optionally the Axle
Energy integration.
"""
import re
import math
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger("pyscript.solis_octopus")
log.setLevel(logging.DEBUG)

VERSION = "2.0.1"

# -----------------------------
# Solis Modbus entities (Grid Time of Use, 6 slots)
# -----------------------------
# Entity IDs are "<domain>.<prefix>_<suffix>", where the prefix comes from the
# inverter's device name in Solis Modbus (e.g. "solis" or "solis_rhi_5k_48es_5g").
PREFIX_PROBE = "grid_time_of_use_charge_start_slot_1"
EMPTY_SLOT = "00:00-00:00"

OCTOPUS_DOMAIN = "octopus_energy"
PD_JOIN_SERVICE = "join_octoplus_power_down_session_event"
HH_JOIN_SERVICE = "join_octoplus_weekend_happy_hour_event"
BOOST_SERVICE = "boost_water_heater"

OVERVIEW_SENSOR = "sensor.solis_octopus"
EV_STATUS_SENSOR = "sensor.solis_ev_charge_slot"
HH_STATUS_SENSOR = "sensor.octopus_happy_hour"
HH_ACTIVE_SENSOR = "binary_sensor.octopus_happy_hour_active"
STATUS_SENSOR = "sensor.solis_power_down"
ACTIVE_SENSOR = "binary_sensor.solis_power_down_active"

# -----------------------------
# Configuration (from configuration.yaml -> pyscript: apps: solis_octopus:)
# -----------------------------
try:
    # Set when this file lives in <config>/pyscript/apps/
    CFG = pyscript.app_config or {}
except Exception:
    # Fallback if the file was placed directly in <config>/pyscript/
    CFG = (pyscript.config.get("apps") or {}).get("solis_octopus") or {}
    log.warning("Not loaded as a pyscript app; move this file to <config>/pyscript/apps/")


def _section(name):
    value = CFG.get(name)
    return dict(value) if isinstance(value, dict) else {}


BAT = _section("battery")
EVC = _section("ev_follow")
HHC = _section("happy_hour")
PDC = _section("power_down")
PKC = _section("peak_export")
AXC = _section("axle")
SLC = _section("slots")
RNC = _section("renault")


def _as_bool(value):
    return str(value).lower() in ("true", "1", "yes", "on")


def _cfg_bool(key, default, section=None):
    src = CFG if section is None else section
    return _as_bool(src.get(key, default))


def _as_list(value):
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _cfg_list(key, section=None):
    src = CFG if section is None else section
    return _as_list(src.get(key))


def _cfg_actions(key, section):
    """
    A list of HA actions, each {service|action: "domain.name", data: {...},
    target: {...}}. Invalid entries are logged and dropped.
    """
    out = []
    value = section.get(key) or []
    if isinstance(value, dict):
        value = [value]
    for item in value:
        if isinstance(item, str):
            item = {"service": item}
        name = str((item.get("service") or item.get("action") or "") if isinstance(item, dict) else "")
        domain, _, service_name = name.partition(".")
        if not domain or not service_name:
            log.error("Ignoring %s entry %s: needs service: <domain>.<name>", key, item)
            continue
        out.append({
            "domain": domain,
            "service": service_name,
            "data": dict(item.get("data") or {}),
            "target": dict(item.get("target")) if item.get("target") else None,
        })
    return out


# --- Shared ---
INVERTER_TIMEZONE = str(CFG.get("inverter_timezone", hass.config.time_zone or "UTC"))
# Solis Modbus entity ID prefix; found automatically when left out.
INVERTER_PREFIX = str(CFG.get("inverter_prefix", "") or "").strip().strip("_").lower()
CORE_WINDOW = str(CFG.get("core_window", "23:30-05:30"))
NOTIFY_SERVICES = _cfg_list("notify_service")
NOTIFY_DATA = dict(CFG.get("notify_data") or {})
STARTUP_DELAY_SECONDS = int(CFG.get("startup_delay_seconds", 60))
DIAGNOSTICS_ONLY = _cfg_bool("diagnostics_only", False)
CONTROL_RETRIES = int(CFG.get("control_retries", 3))
CONTROL_DELAY = float(CFG.get("control_delay", 0.5))
INTER_WRITE_DELAY = float(CFG.get("inter_write_delay", 0.25))
VERIFY_READBACK = _cfg_bool("verify_readback", False)

# --- Slots ---
CORE_SLOT = int(SLC.get("core", 1))
EV_SLOT = int(SLC.get("ev", 2))
HH_SLOT = int(SLC.get("happy_hour", 3))
DISCHARGE_SLOT = int(SLC.get("export", 1))
CLEAR_OTHER_SLOTS = _cfg_bool("clear_other_charge_slots", False, SLC)

# --- Battery / prediction ---
SOC_SENSOR = str(BAT.get("soc_sensor", ""))
BATTERY_CAPACITY_KWH = float(BAT.get("capacity_kwh", 10.0))
FLOOR_SOC = int(BAT.get("floor_soc", 15))
RESERVE_MARGIN = float(BAT.get("reserve_margin", 1.2))
CORE_CHARGE_SOC = int(BAT.get("core_charge_soc", 100))
LOAD_POWER_SENSOR = str(BAT.get("load_power_sensor", ""))
HISTORY_DAYS = int(BAT.get("history_days", 14))
DEFAULT_LOAD_KW = float(BAT.get("default_load_kw", 0.5))
SOLAR_FORECAST_SENSORS = _cfg_list("solar_forecast_sensors", BAT)
BATTERY_VOLTAGE_SENSOR = str(BAT.get("voltage_sensor", ""))
BATTERY_VOLTAGE = float(BAT.get("voltage", 51.2))
MAX_DISCHARGE_KW = float(BAT.get("max_discharge_kw", 5.0))
MAX_DISCHARGE_AMPS = int(BAT.get("max_discharge_amps", 100))
EXPORT_LIMIT_KW = float(BAT.get("export_limit_kw", 3.68))
DISCHARGE_EFFICIENCY = float(BAT.get("discharge_efficiency", 0.95))
MIN_EXPORT_KW = float(BAT.get("min_export_kw", 0.2))
CURRENT_STEP_AMPS = int(BAT.get("current_step_amps", 5))
EXPORT_PREPARE_MINUTES = int(BAT.get("prepare_minutes", 15))

# --- EV-Follow ---
EV_ENABLED = _cfg_bool("enabled", bool(EVC), EVC)
EV_DISPATCH_SENSOR = str(EVC.get("dispatch_sensor", ""))
EV_CHARGER_SWITCH = str(EVC.get("charger_switch", "switch.hypervolt_charging"))
EV_CHARGER_CURRENT = str(EVC.get("charger_current", "sensor.hypervolt_charger_current"))
EV_MIN_CURRENT_AMPS = float(EVC.get("min_current_amps", 3))
EV_DEBOUNCE_SECONDS = int(EVC.get("debounce_seconds", 60))
EV_EXTEND_LEAD_MINUTES = int(EVC.get("extend_lead_minutes", 3))
MANAGE_CORE_SLOT = _cfg_bool("manage_core_slot", True, EVC) and EV_ENABLED

# --- Happy Hour / Power Up ---
HH_ENABLED = _cfg_bool("enabled", bool(HHC), HHC)
HH_EVENTS_ENTITY = str(HHC.get("power_up_events_entity", "")) if HH_ENABLED else ""
# sensor.octopus_energy_<account>_octoplus_weekend_happy_hours, derived from the events entity.
_m = re.match(r"^event\.octopus_energy_(.+)_octoplus_power_up_events$", HH_EVENTS_ENTITY)
HH_TOKENS_ENTITY = str(HHC.get("weekend_happy_hours_entity",
                               f"sensor.octopus_energy_{_m.group(1)}_octoplus_weekend_happy_hours" if _m else ""))
HH_AUTO_JOIN = _cfg_bool("auto_join", False, HHC)
HH_JOIN_SELECTION = str(HHC.get("join_selection", "earliest")).lower()
HH_BATTERY_CHARGE = _cfg_bool("battery_charge", True, HHC) and HH_ENABLED
HH_CHARGE_CURRENT = int(HHC.get("charge_current", 100))
HH_CHARGE_SOC = int(HHC.get("charge_soc", 100))
HH_PREPARE_MINUTES = int(HHC.get("prepare_minutes", 5))
HEAT_PUMP_ID = str(HHC.get("heat_pump_id", ""))
WATER_HEATER = str(HHC.get("water_heater_entity",
                           f"water_heater.octopus_energy_heat_pump_{HEAT_PUMP_ID}" if HEAT_PUMP_ID else ""))
HOT_WATER_BOOST = _cfg_bool("hot_water_boost", bool(WATER_HEATER), HHC) and HH_ENABLED
HOT_WATER_TEMPERATURE = float(HHC.get("hot_water_temperature", 50))
START_ACTIONS = _cfg_actions("start_actions", HHC)
END_ACTIONS = _cfg_actions("end_actions", HHC)
NOTIFY_SCHEDULED = _cfg_bool("notify_scheduled", True, HHC)
NOTIFY_END = _cfg_bool("notify_end", True, HHC)

# --- Power Down ---
PD_ENABLED = _cfg_bool("enabled", bool(PDC), PDC)
PD_EVENTS_ENTITY = str(PDC.get("power_down_events_entity", "")) if PD_ENABLED else ""
PD_AUTO_JOIN = _cfg_bool("auto_join", True, PDC)

# --- Peak export ---
PEAK_EXPORT = _cfg_bool("enabled", bool(PKC), PKC)
PEAK_WINDOW = str(PKC.get("window", "16:00-19:00"))
PEAK_EXPORT_RATE = float(PKC.get("rate", 16))
PEAK_SWITCH = str(PKC.get("switch", ""))
NOTIFY_PEAK = _cfg_bool("notify", False, PKC)

# --- Axle ---
AXLE_EVENTS = _cfg_bool("enabled", bool(AXC), AXC)
AXLE_TYPE_SENSOR = str(AXC.get("event_type_sensor", "sensor.axle_energy_event_type"))
AXLE_START_SENSOR = str(AXC.get("event_start_sensor", "sensor.axle_energy_event_start"))
AXLE_END_SENSOR = str(AXC.get("event_end_sensor", "sensor.axle_energy_event_end"))
AXLE_SENSORS = [AXLE_TYPE_SENSOR, AXLE_START_SENSOR, AXLE_END_SENSOR] if AXLE_EVENTS else []
AXLE_GRACE_MINUTES = int(AXC.get("grace_minutes", 10))
# Self-Dispatch (Axle sends no inverter commands): the app exports during Axle
# export events. Otherwise (Events Only) Axle drives the battery and the app
# stands aside.
AXLE_CONTROL = _cfg_bool("control", False, AXC) and AXLE_EVENTS
AXLE_KEEP_RESERVE = _cfg_bool("keep_evening_reserve", False, AXC)

# --- Renault car charge schedule (moved into happy hours, back to normal after) ---
RENAULT_ENABLED = _cfg_bool("enabled", bool(RNC), RNC)
RENAULT_VEHICLE = str(RNC.get("vehicle", ""))
RENAULT_NAME = str(RNC.get("name", "Car"))
RENAULT_SCHEDULE_ID = int(RNC.get("schedule_id", 1))
RENAULT_NORMAL_WINDOW = str(RNC.get("normal_window", CORE_WINDOW))
RENAULT_PREPARE_MINUTES = int(RNC.get("prepare_minutes", 10))
RENAULT_RETRY_MINUTES = int(RNC.get("retry_minutes", 5))
RENAULT_NOTIFY = _cfg_bool("notify", True, RNC)
if RENAULT_ENABLED and not RENAULT_VEHICLE:
    log.error("renault.vehicle (the Renault device ID) isn't set; the car's charge schedule won't be changed")
    RENAULT_ENABLED = False
if RENAULT_ENABLED and not HH_ENABLED:
    log.warning("renault needs the happy_hour section; the car's charge schedule won't be changed")
    RENAULT_ENABLED = False

INVERTER_CONTROL = _cfg_bool("inverter_control", True)
if not INVERTER_CONTROL:
    log.warning("inverter_control is off; the inverter won't be controlled")
    HH_BATTERY_CHARGE = False
if HOT_WATER_BOOST and not WATER_HEATER:
    log.error("happy_hour.hot_water_boost needs heat_pump_id or water_heater_entity; hot water boost is off")
    HOT_WATER_BOOST = False


def _slot_problems():
    """Configuration clashes between features' slots."""
    problems = []
    charge = []
    if MANAGE_CORE_SLOT:
        charge.append(("core", CORE_SLOT))
    if EV_ENABLED:
        charge.append(("ev", EV_SLOT))
    if HH_BATTERY_CHARGE:
        charge.append(("happy_hour", HH_SLOT))
    for name, num in charge + [("export", DISCHARGE_SLOT)]:
        if not 1 <= num <= 6:
            problems.append(f"slots.{name} must be 1-6, not {num}")
    seen = {}
    for name, num in charge:
        if num in seen:
            problems.append(f"slots.{name} and slots.{seen[num]} are both charge slot {num}")
        seen[num] = name
    return problems


SLOT_PROBLEMS = _slot_problems()
for _p in SLOT_PROBLEMS:
    log.error("Slot config: %s", _p)

try:
    INV_TZ = ZoneInfo(INVERTER_TIMEZONE)
except Exception as e:
    log.warning("Invalid inverter_timezone '%s' (%s), falling back to UTC", INVERTER_TIMEZONE, e)
    INV_TZ = timezone.utc

# -----------------------------
# Runtime state (dicts so functions can mutate without `global`)
# -----------------------------
# What we last wrote to each slot we own. "unknown" means we don't know what's
# on the inverter (e.g. after a restart) and must write it. "ends" is when an
# enabled window stops discharging/charging by itself.
SLOT_DEFS = {
    "core": ("charge", CORE_SLOT),
    "ev": ("charge", EV_SLOT),
    "happy_hour": ("charge", HH_SLOT),
    "export": ("discharge", DISCHARGE_SLOT),
}


def _empty_program():
    return {"window": EMPTY_SLOT, "current": None, "soc": None, "enabled": False, "unknown": True,
            "ends": None}


_slots = {"core": _empty_program(), "ev": _empty_program(), "happy_hour": _empty_program(),
          "export": _empty_program()}
_cache = {"prefix": None, "entities": {}, "load_profile": None, "load_profile_at": None}
_flags = {"busy": False, "write_lock": False, "started": False, "last_reason": None,
          "last_write_ok": None, "pd_warned_at": None, "hh_warned_at": None, "axle_warned_at": None,
          "write_batches": 0, "hh_seeded": False, "others_cleared": False}
# Power Down sessions we joined ourselves (id -> session), and which we've announced.
_pd_joined = {"by_us": {}, "notified": set()}
# Happy hours we booked (id -> session) and when a booking last failed (id -> time).
_hh_joined = {"by_us": {}, "failed_at": {}}
# Happy hour windows we've announced, and the ones running (id -> record).
_hh_seen = {"announced": set(), "running": {}}
# EV-Follow: the programmed EV window as datetimes, and when charging was first seen.
_ev = {"start": None, "end": None, "charging_since": None, "debounced": False, "last_charging": None,
       "info": {}}
_last = {"plan": None, "export_window": None, "hh_window": None, "decision": None}
# Renault: the schedule we last set on the car (None = unknown, e.g. after a
# restart, so it's written), and the retry state for the one we want.
_car = {"applied": None, "applied_mode": None, "wanted": None, "failed_at": None, "failures": 0,
        "fail_notified": None, "last_ok": None}


# -----------------------------
# Solis Modbus client (shared by every feature)
# -----------------------------
def _entity_ids(domain):
    try:
        return list(hass.states.async_entity_ids(domain))
    except Exception:
        return []


def inverter_prefix():
    """
    The Solis Modbus entity ID prefix, e.g. "solis" for
    time.solis_grid_time_of_use_charge_start_slot_1. From inverter_prefix, or
    found from the time entities. Falls back to "solis".
    """
    if INVERTER_PREFIX:
        return INVERTER_PREFIX
    if _cache["prefix"]:
        return _cache["prefix"]
    found = []
    for entity_id in _entity_ids("time"):
        name = entity_id.split(".", 1)[-1]
        if name.endswith("_" + PREFIX_PROBE):
            found.append(name[:-len(PREFIX_PROBE) - 1])
    if len(found) == 1:
        _cache["prefix"] = found[0]
        log.info("Using Solis Modbus entities with prefix '%s'", found[0])
        return found[0]
    if len(found) > 1:
        log.error("Several Solis Modbus inverters found (%s); set inverter_prefix", ", ".join(sorted(found)))
    return "solis"


def resolve_entity(domain, prefix, suffix):
    """
    "<domain>.<prefix>_<suffix>" if it exists. Otherwise the one entity in the
    domain whose ID ends in "_<suffix>": Home Assistant keeps an entity's
    first ID, so entities created by older Solis Modbus versions can be named
    differently (e.g. switch.<prefix>_solis_grid_time_of_use_charging_period_1).
    """
    exact = f"{domain}.{prefix}_{suffix}"
    if exact in _cache["entities"]:
        return _cache["entities"][exact]
    if hass.states.get(exact) is not None:
        return exact
    candidates = []
    for entity_id in _entity_ids(domain):
        if entity_id.endswith("_" + suffix) or entity_id == f"{domain}.{suffix}":
            candidates.append(entity_id)
    if len(candidates) > 1:
        # Several inverters: keep the ones that look like ours.
        candidates = [e for e in candidates if e.split(".", 1)[1].startswith(prefix + "_")]
    if len(candidates) == 1:
        log.info("Using %s for %s", candidates[0], exact)
        _cache["entities"][exact] = candidates[0]
        return candidates[0]
    return exact


def slot_entity_ids(kind, num):
    """((start, end) time entities, current number, SOC number, on/off switch) for one slot."""
    p = inverter_prefix()
    if kind == "charge":
        names = ("charge_start", "charge_end", "charge_battery_current", "charge_cut_off_soc", "charging_period")
    else:
        names = ("discharge_start", "discharge_end", "discharge_battery_current", "discharge_cut_off_soc",
                 "discharge_period")
    start, end, current, soc, switch = names
    return ((resolve_entity("time", p, f"grid_time_of_use_{start}_slot_{num}"),
             resolve_entity("time", p, f"grid_time_of_use_{end}_slot_{num}")),
            resolve_entity("number", p, f"grid_time_of_use_{current}_slot_{num}"),
            resolve_entity("number", p, f"grid_time_of_use_{soc}_slot_{num}"),
            resolve_entity("switch", p, f"grid_time_of_use_{switch}_{num}"))


def _op_entities(op):
    return list(op[1]) if op[0] == "time" else [op[1]]


def missing_entities(entity_ids):
    out = []
    for entity_id in entity_ids:
        s = hass.states.get(entity_id)
        if s is None or str(s.state).lower() == "unavailable":
            out.append(entity_id)
    return out


async def _call_service(domain, name, data):
    """Call an HA service, retrying on errors. Returns True on success."""
    for attempt in range(1, CONTROL_RETRIES + 1):
        try:
            await hass.services.async_call(domain, name, data, blocking=True)
            return True
        except Exception as e:
            log.warning("%s.%s %s attempt %s/%s failed: %s", domain, name, data.get("entity_id"),
                        attempt, CONTROL_RETRIES, e)
            await asyncio.sleep(CONTROL_DELAY)
    log.error("%s.%s %s failed after %s attempts", domain, name, data.get("entity_id"), CONTROL_RETRIES)
    return False


async def _readback(entity_id, matches):
    if not VERIFY_READBACK:
        return
    await asyncio.sleep(CONTROL_DELAY)
    s = hass.states.get(entity_id)
    value = None if s is None else str(s.state)
    if value is None or not matches(value):
        log.warning("Readback %s: got '%s' after writing", entity_id, value)
    else:
        log.debug("Readback %s: %s", entity_id, value)


async def write_time(entity_id, hhmm):
    log.info("Setting %s to %s", entity_id, hhmm)
    if not await _call_service("time", "set_value", {"entity_id": entity_id, "time": f"{hhmm}:00"}):
        return False
    await _readback(entity_id, lambda v: v[:5] == hhmm)
    return True


async def write_window(entity_ids, window):
    """Write "HH:MM-HH:MM" to a slot's start and end time entities."""
    start, _, end = str(window).replace(" ", "").partition("-")
    if not await write_time(entity_ids[0], start):
        return False
    await asyncio.sleep(INTER_WRITE_DELAY)
    return await write_time(entity_ids[1], end)


def _same_number(a, b):
    try:
        return abs(float(a) - float(b)) < 0.05
    except (TypeError, ValueError):
        return False


async def write_number(entity_id, value):
    log.info("Setting %s to %s", entity_id, value)
    number = float(value)
    if not await _call_service("number", "set_value", {"entity_id": entity_id, "value": number}):
        return False
    await _readback(entity_id, lambda v: _same_number(v, number))
    return True


def switch_is(entity_id):
    """True/False for a switch that's on/off, None if unknown."""
    s = hass.states.get(entity_id)
    if s is None:
        return None
    value = str(s.state).lower()
    if value == "on":
        return True
    if value == "off":
        return False
    return None


async def set_slot_switch(entity_id, enabled):
    """Switch a slot on/off if it isn't already."""
    if switch_is(entity_id) is enabled:
        return True
    log.info("%s %s", "Enabling" if enabled else "Disabling", entity_id)
    if not await _call_service("switch", "turn_on" if enabled else "turn_off", {"entity_id": entity_id}):
        return False
    await _readback(entity_id, lambda v: v.lower() == ("on" if enabled else "off"))
    return True


async def solis_apply(ops):
    """
    Apply slot changes in order, under the write lock, through the Solis Modbus
    entities. `ops` is a list of ("time", (start, end), "HH:MM-HH:MM"),
    ("value", number_entity, value) and ("switch", switch_entity, enabled).
    Stops at the first failure (a slot is never switched on after a failed
    write). Returns True if everything succeeded.
    """
    if not ops:
        return True
    if DIAGNOSTICS_ONLY:
        log.warning("DIAGNOSTICS MODE: would write %s", ops)
        return True

    while _flags["write_lock"]:
        await asyncio.sleep(0.5)
    _flags["write_lock"] = True
    try:
        _flags["write_batches"] += 1
        needed = []
        for op in ops:
            needed.extend(_op_entities(op))
        missing = missing_entities(needed)
        if missing:
            log.error("Solis Modbus entities missing or unavailable: %s. Check the Solis Modbus "
                      "integration and inverter_prefix.", ", ".join(missing))
            return False

        for op in ops:
            if op[0] == "switch":
                ok = await set_slot_switch(op[1], op[2])
            elif op[0] == "time":
                ok = await write_window(op[1], op[2])
            else:
                ok = await write_number(op[1], op[2])
            if not ok:
                return False
            await asyncio.sleep(INTER_WRITE_DELAY)
        return True
    finally:
        _flags["write_lock"] = False


def check_inverter_entities():
    """Log once at startup whether the Solis Modbus slot entities this app needs exist."""
    needed = []
    for key in active_slot_keys():
        times, current, soc, switch = slot_entities(key)
        needed.extend(list(times) + [current, soc, switch])
    if not needed:
        return
    missing = missing_entities(needed)
    if missing:
        log.error("Solis Modbus entities not found (prefix '%s'): %s. Install the Solis Modbus "
                  "integration, check the inverter supports Grid Time of Use slots, or set "
                  "inverter_prefix.", inverter_prefix(), ", ".join(missing))
    else:
        log.info("Solis Modbus slot entities found (prefix '%s'). Time-of-use must be enabled on "
                 "the inverter for the slots to work.", inverter_prefix())


# -----------------------------
def _parse_window(text):
    start_s, end_s = text.split("-")
    return (datetime.strptime(start_s.strip(), "%H:%M").time(),
            datetime.strptime(end_s.strip(), "%H:%M").time())


def now_local():
    return datetime.now(INV_TZ)


def to_local(dt):
    return dt.astimezone(INV_TZ)


def floor_half_hour(dt):
    return dt.replace(minute=(dt.minute // 30) * 30, second=0, microsecond=0)


def parse_dt(value):
    """Datetime or ISO string -> tz-aware datetime (naive is taken as UTC), or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def periods(start, end):
    """
    Split [start, end) into pieces on half-hour boundaries.
    Returns [(piece_start_utc, hours)]. Works in UTC so DST changes are exact.
    """
    out = []
    t = start.astimezone(timezone.utc)
    end_utc = end.astimezone(timezone.utc)
    while t < end_utc:
        nxt = min(floor_half_hour(t) + timedelta(minutes=30), end_utc)
        out.append((t, (nxt - t).total_seconds() / 3600.0))
        t = nxt
    return out


def _at_local_time(day_dt, t):
    """`day_dt`'s date at local time `t` (DST-aware)."""
    local = to_local(day_dt)
    return datetime(local.year, local.month, local.day, t.hour, t.minute, tzinfo=INV_TZ)


def next_core_start(after):
    """The first core-window start at or after `after`."""
    for days in range(0, 3):
        candidate = _at_local_time(after + timedelta(days=days), CORE_START)
        if candidate >= after:
            return candidate
    return after


def in_core_window(dt):
    t = to_local(dt).time()
    if CORE_START <= CORE_END:
        return CORE_START <= t < CORE_END
    return t >= CORE_START or t < CORE_END


def last_core_end_between(after, before):
    """The latest core-window end in (after, before], or None."""
    best = None
    for days in range(-1, 3):
        candidate = _at_local_time(before - timedelta(days=days), CORE_END)
        if after < candidate <= before and (best is None or candidate > best):
            best = candidate
    return best


def fmt_window(start, end):
    return f"{to_local(start).strftime('%H:%M')}-{to_local(end).strftime('%H:%M')}"


def fmt_day(dt):
    return to_local(dt).strftime("%a %d %b")


def _state_obj(entity_id):
    if not entity_id:
        return None
    return hass.states.get(entity_id)


def _float_state(entity_id):
    s = _state_obj(entity_id)
    if s is None:
        return None
    try:
        return float(s.state)
    except (TypeError, ValueError):
        return None


def _attrs(entity_id):
    s = _state_obj(entity_id)
    if s is None:
        return {}
    return dict(getattr(s, "attributes", None) or {})


CORE_START, CORE_END = _parse_window(CORE_WINDOW)
PEAK_START, PEAK_END = _parse_window(PEAK_WINDOW)


def next_half_hour(dt):
    """The next :00/:30 strictly after dt."""
    return floor_half_hour(dt) + timedelta(minutes=30)


def axle_dispatch(window):
    """An Axle export event the app runs itself (Self-Dispatch)."""
    return AXLE_CONTROL and window is not None and window.get("kind") == "axle_export"


def stand_aside(window, now):
    """An Axle event Axle is driving itself: no inverter writes."""
    return (not AXLE_CONTROL) and window is not None and str(window.get("kind", "")).startswith("axle") \
        and window["start"] <= now


def overlaps(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end


async def notify(title, message):
    for target in NOTIFY_SERVICES:
        domain, _, name = target.partition(".")
        if not name:
            domain, name = "notify", domain
        data = {"title": title, "message": message}
        if NOTIFY_DATA:
            data["data"] = dict(NOTIFY_DATA)
        try:
            await hass.services.async_call(domain, name, data, blocking=False)
        except Exception as e:
            log.warning("Notify via %s failed: %s", target, e)


# -----------------------------
# Export planner: Axle, Power Down, peak windows
# -----------------------------
def pd_session_from_event(ev, joined):
    start = parse_dt(ev.get("start"))
    end = parse_dt(ev.get("end"))
    if start is None or end is None or end <= start:
        return None
    try:
        ppk = float(ev.get("octopoints_per_kwh") or 0)
    except (TypeError, ValueError):
        ppk = 0.0
    return {
        "id": str(ev.get("id")),
        "code": ev.get("code"),
        "start": start,
        "end": end,
        "octopoints_per_kwh": ppk,
        "joined": joined,
    }


def pd_sessions():
    """(joined, available) sessions from the power down events entity."""
    attrs = _attrs(PD_EVENTS_ENTITY)
    joined = []
    seen = set()
    for ev in attrs.get("joined_events") or []:
        s = pd_session_from_event(ev, True)
        if s is not None:
            joined.append(s)
            seen.add(s["id"])
    for sid, s in _pd_joined["by_us"].items():
        if sid not in seen:
            joined.append(s)
            seen.add(sid)

    available = []
    for ev in attrs.get("available_events") or []:
        s = pd_session_from_event(ev, False)
        if s is not None and s["id"] not in seen:
            available.append(s)
    return joined, available


def peak_switch_on():
    if not PEAK_SWITCH:
        return True
    s = hass.states.get(PEAK_SWITCH)
    return s is None or s.state != "off"


def peak_windows(now, honour_switch=True):
    """Daily peak export windows (yesterday to tomorrow) that haven't ended."""
    if not PEAK_EXPORT or (honour_switch and not peak_switch_on()):
        return []
    out = []
    for days in (-1, 0, 1):
        day = now + timedelta(days=days)
        start = _at_local_time(day, PEAK_START)
        end = _at_local_time(day, PEAK_END)
        if end <= start:
            end = _at_local_time(day + timedelta(days=1), PEAK_END)
        if end > now:
            out.append({"id": f"peak-{to_local(start).strftime('%Y-%m-%d')}", "kind": "peak",
                        "start": start, "end": end})
    return out


def export_rate_at(t):
    """The peak export rate (p/kWh) if `t` is in a peak window, else None."""
    for w in peak_windows(t, honour_switch=False):
        if w["start"] <= t < w["end"]:
            return PEAK_EXPORT_RATE
    return None


def axle_entities_status():
    """"disabled", "ok" or "missing" for the Axle Energy sensors."""
    if not AXLE_EVENTS:
        return "disabled"
    for entity in AXLE_SENSORS:
        if hass.states.get(entity) is None:
            return "missing"
    return "ok"


def axle_warning(status):
    if status == "missing":
        missing = [e for e in AXLE_SENSORS if hass.states.get(e) is None]
        return (f"Axle Energy sensor(s) {', '.join(missing)} don't exist, so Axle events are "
                f"ignored. Check the axle_event_*_sensor settings against Developer Tools -> States.")
    return None


def axle_event(now):
    """
    The current or next Axle event as a window, or None. Axle drives the
    battery itself during it; `end` includes a grace period after the event.
    """
    if axle_entities_status() != "ok":
        return None
    kind = str(hass.states.get(AXLE_TYPE_SENSOR).state).lower()
    if kind not in ("export", "import"):
        return None
    start = parse_dt(hass.states.get(AXLE_START_SENSOR).state)
    event_end = parse_dt(hass.states.get(AXLE_END_SENSOR).state)
    if start is None or event_end is None or event_end <= start:
        return None
    # No grace period when the app runs the event itself (Axle sends no commands).
    end = event_end + timedelta(minutes=0 if AXLE_CONTROL else AXLE_GRACE_MINUTES)
    if end <= now:
        return None
    return {"id": f"axle-{start.astimezone(timezone.utc).strftime('%Y%m%dT%H%M')}",
            "kind": "axle_" + kind, "start": start, "end": end, "event_end": event_end,
            "octopoints_per_kwh": 0.0}


def is_axle(window):
    return window is not None and str(window.get("kind", "")).startswith("axle")


def _cut(pieces, start, end):
    """Remove [start, end) from a list of (a, b) pieces."""
    cut = []
    for a, b in pieces:
        if end <= a or start >= b:
            cut.append((a, b))
            continue
        if a < start:
            cut.append((a, start))
        if end < b:
            cut.append((end, b))
    return cut


def export_windows(now):
    """
    The Axle event, joined Power Down sessions and daily peak windows that
    haven't ended, as non-overlapping segments sorted by start. Priority is
    Axle, then Power Down, then peak: each is cut around the ones above it.
    """
    segments = []
    axle = axle_event(now)
    if axle is not None:
        segments.append(axle)

    joined, _ = pd_sessions()
    # Every joined session cuts the peak window, even one that has just ended,
    # so the rest of the peak window keeps its own start time.
    power_downs = []
    for s in joined:
        power_downs.append(s)
        pieces = [(s["start"], s["end"])]
        if axle is not None:
            pieces = _cut(pieces, axle["start"], axle["end"])
        for a, b in pieces:
            if b > now:
                w = dict(s)
                w["kind"] = "power_down"
                w["start"], w["end"] = a, b
                if (a, b) != (s["start"], s["end"]):
                    w["id"] = f"{s['id']}-{to_local(a).strftime('%H%M')}"
                segments.append(w)

    for p in peak_windows(now):
        pieces = [(p["start"], p["end"])]
        if axle is not None:
            pieces = _cut(pieces, axle["start"], axle["end"])
        for d in power_downs:
            pieces = _cut(pieces, d["start"], d["end"])
        for a, b in pieces:
            if b > now:
                segments.append({"id": f"{p['id']}-{to_local(a).strftime('%H%M')}", "kind": "peak",
                                 "start": a, "end": b, "octopoints_per_kwh": 0.0})

    ordered = sorted([(w["start"].timestamp(), i) for i, w in enumerate(segments)])
    return [segments[i] for _, i in ordered]


def export_target_window(now):
    """
    (window, later): the export window running now or else the next one, and
    the windows after it that come before the next cheap window.
    """
    windows = export_windows(now)
    for i, w in enumerate(windows):
        if w["end"] > now:
            cutoff = next_core_start(w["end"])
            later = [x for x in windows[i + 1:] if x["start"] < cutoff]
            return w, later
    return None, []


def pd_events_status():
    """"ok", "not_configured" or "missing" for the power down events entity."""
    if not PD_EVENTS_ENTITY:
        return "not_configured"
    if hass.states.get(PD_EVENTS_ENTITY) is None:
        return "missing"
    return "ok"


def pd_events_warning(status):
    if status == "missing":
        return (f"Power down events entity '{PD_EVENTS_ENTITY}' doesn't exist, so Power Down sessions "
                f"are ignored. Check power_down_events_entity against Developer Tools -> States.")
    if status == "not_configured":
        return "power_down_events_entity isn't set, so Power Down sessions are ignored."
    return None


def pd_warn_events(status):
    """Log a missing/unset events entity at most once an hour."""
    if status == "ok":
        _flags["pd_warned_at"] = None
        return
    now = datetime.now(timezone.utc)
    last = _flags["pd_warned_at"]
    if last is not None and now - last < timedelta(hours=1):
        return
    _flags["pd_warned_at"] = now
    if status == "missing":
        log.error(pd_events_warning(status))
    else:
        log.warning(pd_events_warning(status))


def warn_axle(status):
    """Log missing Axle sensors at most once an hour."""
    if status != "missing":
        _flags["axle_warned_at"] = None
        return
    now = datetime.now(timezone.utc)
    last = _flags["axle_warned_at"]
    if last is not None and now - last < timedelta(hours=1):
        return
    _flags["axle_warned_at"] = now
    log.error(axle_warning(status))


async def pd_auto_join():
    if not PD_AUTO_JOIN or pd_events_status() != "ok":
        return
    now = now_local()
    _, available = pd_sessions()
    for s in available:
        if s["end"] <= now or not s["code"]:
            continue
        log.info("Joining Power Down session %s-%s (code %s, %s octopoints/kWh)",
                 s["start"].isoformat(), s["end"].isoformat(), s["code"], s["octopoints_per_kwh"])
        try:
            await hass.services.async_call(
                OCTOPUS_DOMAIN, PD_JOIN_SERVICE, {"event_code": s["code"]},
                blocking=True, target={"entity_id": PD_EVENTS_ENTITY},
            )
        except Exception as e:
            log.error("Failed to join Power Down session %s: %s (will retry)", s["code"], e)
            continue
        s["joined"] = True
        _pd_joined["by_us"][s["id"]] = s
        await notify(
            "Joined Octopus Power Down",
            f"{to_local(s['start']).strftime('%a %d %b')} {fmt_window(s['start'], s['end'])}, "
            f"{s['octopoints_per_kwh']:g} octopoints/kWh. The battery will export during it.",
        )


# -----------------------------
# Export prediction
# -----------------------------
async def fetch_load_profile():
    """
    {local hour: mean kW} for the load sensor over the last HISTORY_DAYS, from
    the recorder's hourly long-term statistics. Empty if unavailable.
    """
    if not LOAD_POWER_SENSOR:
        return {}
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=HISTORY_DAYS)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, end, {LOAD_POWER_SENSOR}, "hour", None, {"mean"}
    )
    unit = str(_attrs(LOAD_POWER_SENSOR).get("unit_of_measurement", "W"))
    scale = 1.0 if unit.lower() == "kw" else 0.001
    return build_load_profile(stats.get(LOAD_POWER_SENSOR) or [], scale)


def build_load_profile(rows, scale):
    sums = {}
    counts = {}
    for row in rows:
        mean = row.get("mean")
        start = row.get("start")
        if mean is None or start is None:
            continue
        if isinstance(start, (int, float)):
            start = datetime.fromtimestamp(start, tz=timezone.utc)
        start = parse_dt(start)
        hour = to_local(start).hour
        sums[hour] = sums.get(hour, 0.0) + max(0.0, float(mean)) * scale
        counts[hour] = counts.get(hour, 0) + 1
    profile = {}
    for hour in sums:
        profile[hour] = sums[hour] / counts[hour]
    return profile


async def ensure_load_profile():
    """Refresh the load profile at most every 6 hours."""
    at = _cache["load_profile_at"]
    if _cache["load_profile"] is not None and at is not None \
            and datetime.now(timezone.utc) - at < timedelta(hours=6):
        return _cache["load_profile"]
    try:
        profile = await fetch_load_profile()
        if LOAD_POWER_SENSOR and not profile:
            log.warning("No recorder statistics for %s; using default_load_kw=%s",
                        LOAD_POWER_SENSOR, DEFAULT_LOAD_KW)
    except Exception as e:
        log.warning("Load history unavailable (%s); using default_load_kw=%s", e, DEFAULT_LOAD_KW)
        profile = {}
    _cache["load_profile"] = profile
    _cache["load_profile_at"] = datetime.now(timezone.utc)
    return profile


def solar_forecast():
    """{half-hour start (UTC): kW} from Solcast-style detailedForecast attributes."""
    out = {}
    for entity in SOLAR_FORECAST_SENSORS:
        for item in _attrs(entity).get("detailedForecast") or []:
            start = parse_dt(item.get("period_start"))
            try:
                kw = float(item.get("pv_estimate") or 0)
            except (TypeError, ValueError):
                continue
            if start is not None:
                out[floor_half_hour(start.astimezone(timezone.utc))] = max(0.0, kw)
    return out


def predict(session, now, soc, voltage, load_profile, solar, later=None, drain_to_floor=False):
    """
    Plan the export for the window `session`. All energies in kWh, powers in kW.
    `soc` is the battery SOC now (%), `load_profile` {local hour: kW},
    `solar` {UTC half-hour: kW}, `later` the windows after it and before the
    cheap window (battery is held back for Power Down ones). `drain_to_floor`
    leaves out the evening reserve (Axle export events).
    """
    cap = BATTERY_CAPACITY_KWH

    def load_kw(t):
        return load_profile.get(to_local(t).hour, DEFAULT_LOAD_KW)

    def solar_kw(t):
        return solar.get(floor_half_hour(t.astimezone(timezone.utc)), 0.0)

    def net_drain(a, b):
        total = 0.0
        for t, h in periods(a, b):
            total += max(0.0, load_kw(t) - solar_kw(t)) * h
        return total

    volts = voltage if voltage and voltage > 0 else BATTERY_VOLTAGE

    def max_battery_kw(a, b):
        """The most the battery can usefully give in [a, b): inverter, current and export caps."""
        hrs = 0.0
        house = 0.0
        for t, h in periods(a, b):
            hrs += h
            house += (load_kw(t) - solar_kw(t)) * h
        if hrs <= 0:
            return 0.0, 0.0
        export_cap = max(0.0, (EXPORT_LIMIT_KW + house / hrs) / DISCHARGE_EFFICIENCY)
        return min(MAX_DISCHARGE_KW, MAX_DISCHARGE_AMPS * volts / 1000.0, export_cap), hrs

    start = max(session["start"], now)
    end = session["end"]

    # Battery at the start: from now, or from a full battery at the end of
    # the overnight window if one comes first.
    base_time, base_soc = now, soc
    core_end = last_core_end_between(now, start)
    if core_end is not None:
        base_time, base_soc = core_end, max(soc, CORE_CHARGE_SOC)
    battery_at_start = max(0.0, min(cap, base_soc / 100.0 * cap - net_drain(base_time, start)))

    # Reserve: the floor, plus the house from session end until the cheap window.
    floor_kwh = FLOOR_SOC / 100.0 * cap
    if drain_to_floor or in_core_window(end):
        evening_kwh = 0.0
    else:
        evening_kwh = net_drain(end, next_core_start(end))
    # Power Down first: keep what later Power Down sessions can export. Their
    # house load is already in the evening reserve.
    held_kwh = 0.0
    for w in later or []:
        if w.get("kind") in ("power_down", "axle_export"):
            kw, hrs = max_battery_kw(max(w["start"], end), w["end"])
            held_kwh += max(0.0, kw * hrs - net_drain(max(w["start"], end), w["end"]))
    reserve_kwh = min(cap, floor_kwh + RESERVE_MARGIN * evening_kwh + held_kwh)

    session_periods = periods(start, end)
    hours = 0.0
    load_kwh = 0.0
    solar_kwh = 0.0
    for t, h in session_periods:
        hours += h
        load_kwh += load_kw(t) * h
        solar_kwh += solar_kw(t) * h

    spare_kwh = max(0.0, battery_at_start - reserve_kwh)
    if hours > 0:
        # Battery power that would push exactly the export limit out.
        export_cap_kw = max(0.0, (EXPORT_LIMIT_KW + (load_kwh - solar_kwh) / hours) / DISCHARGE_EFFICIENCY)
        power_kw = min(spare_kwh / hours, MAX_DISCHARGE_KW, export_cap_kw)
    else:
        power_kw = 0.0

    current = 0
    if power_kw > 0:
        current = min(MAX_DISCHARGE_AMPS, int(math.ceil(power_kw * 1000.0 / volts - 1e-6)))
    power_kw = min(power_kw, current * volts / 1000.0)

    export_kwh = 0.0
    import_kwh = 0.0
    value_pence = 0.0
    for t, h in session_periods:
        net = power_kw * DISCHARGE_EFFICIENCY + solar_kw(t) - load_kw(t)
        exported = min(EXPORT_LIMIT_KW, max(0.0, net)) * h
        export_kwh += exported
        import_kwh += max(0.0, -net) * h
        rate = export_rate_at(t)
        if rate is not None:
            value_pence += exported * rate

    stop_soc = int(math.ceil(reserve_kwh / cap * 100.0 - 1e-6)) if cap > 0 else 100
    stop_soc = max(FLOOR_SOC, min(100, stop_soc))
    avoided_kwh = max(0.0, load_kwh - import_kwh)

    return {
        "session_start": session["start"].isoformat(),
        "session_end": session["end"].isoformat(),
        "window": fmt_window(session["start"], session["end"]),
        "window_kind": session.get("kind", "power_down"),
        "window_id": session.get("id"),
        "hours": round(hours, 3),
        "battery_at_start_kwh": round(battery_at_start, 2),
        "reserve_kwh": round(reserve_kwh, 2),
        "held_for_power_down_kwh": round(held_kwh, 2),
        "evening_load_kwh": round(evening_kwh, 2),
        "spare_kwh": round(spare_kwh, 2),
        "battery_used_kwh": round(power_kw * hours, 2),
        "discharge_kw": round(power_kw, 2),
        "discharge_current": current,
        "stop_soc": stop_soc,
        "predicted_house_load_kwh": round(load_kwh, 2),
        "predicted_solar_kwh": round(solar_kwh, 2),
        "predicted_export_kwh": round(export_kwh, 2),
        "predicted_export_kw": round(export_kwh / hours, 2) if hours > 0 else 0.0,
        "predicted_import_kwh": round(import_kwh, 2),
        "estimated_octopoints": int(round(avoided_kwh * session.get("octopoints_per_kwh", 0))),
        "estimated_export_value_pence": round(value_pence, 1),
    }


async def plan_for(session, now, later=None, drain_to_floor=False):
    soc = _float_state(SOC_SENSOR)
    if soc is None:
        log.warning("SOC sensor '%s' unavailable; can't plan", SOC_SENSOR)
        return None
    voltage = _float_state(BATTERY_VOLTAGE_SENSOR)
    profile = await ensure_load_profile()
    return predict(session, now, soc, voltage, profile, solar_forecast(), later, drain_to_floor)


def worth_exporting(plan):
    """
    Only force a discharge when it actually exports at a useful rate. A slot
    whose current is below the house load makes the grid cover the gap, which
    is worse than leaving the inverter in self-use.
    """
    return plan["discharge_current"] > 0 and plan["predicted_export_kw"] >= MIN_EXPORT_KW


def export_desired(session, plan, now):
    """What the discharge slot should hold right now."""
    if session is None or plan is None or (is_axle(session) and not axle_dispatch(session)):
        return {"window": EMPTY_SLOT, "current": None, "soc": None, "enabled": False}
    if now < session["start"] - timedelta(minutes=EXPORT_PREPARE_MINUTES) or now >= session["end"]:
        return {"window": EMPTY_SLOT, "current": None, "soc": None, "enabled": False}
    if not worth_exporting(plan):
        return {"window": EMPTY_SLOT, "current": None, "soc": None, "enabled": False}
    return {
        "window": fmt_window(session["start"], session["end"]),
        "current": plan["discharge_current"],
        "soc": plan["stop_soc"],
        "enabled": True,
    }


# -----------------------------
# Happy Hour / Power Up planner
# -----------------------------
def hh_session_from_event(ev):
    if not isinstance(ev, dict):
        return None
    start = parse_dt(ev.get("start"))
    end = parse_dt(ev.get("end"))
    if start is None or end is None or end <= start:
        return None
    return {
        "id": str(ev.get("id")),
        "code": ev.get("code"),
        "start": start,
        "end": end,
        "availability": str(ev.get("availability") or ""),
    }


def hh_sessions():
    """(booked, available) sessions from the power up events entity."""
    attrs = _attrs(HH_EVENTS_ENTITY)
    booked = []
    seen = set()
    for ev in attrs.get("events") or []:
        s = hh_session_from_event(ev)
        if s is not None:
            booked.append(s)
            seen.add(s["id"])
    for sid, s in _hh_joined["by_us"].items():
        if sid not in seen:
            booked.append(s)
            seen.add(sid)

    available = []
    for ev in attrs.get("available_events") or []:
        s = hh_session_from_event(ev)
        if s is not None and s["id"] not in seen:
            available.append(s)
    return booked, available


def _by_start(items):
    ordered = sorted([(s["start"].timestamp(), i) for i, s in enumerate(items)])
    return [items[i] for _, i in ordered]


def happy_hours():
    """Booked sessions as windows sorted by start, with overlapping / back-to-back ones merged."""
    booked, _ = hh_sessions()
    windows = []
    for s in _by_start(booked):
        if windows and s["start"] <= windows[-1]["end"]:
            if s["end"] > windows[-1]["end"]:
                windows[-1]["end"] = s["end"]
            windows[-1]["sessions"] += 1
            continue
        windows.append({"id": to_local(s["start"]).strftime("%Y-%m-%dT%H:%M"),
                        "start": s["start"], "end": s["end"], "sessions": 1})
    return windows


def hh_target_window(windows, now):
    """The happy hour running now, or else the next one."""
    for w in windows:
        if w["end"] > now:
            return w
    return None


def is_running(window, now):
    return window is not None and window["start"] <= now < window["end"]


def hh_events_status():
    """"ok", "not_configured" or "missing" for the power up events entity."""
    if not HH_EVENTS_ENTITY:
        return "not_configured"
    if hass.states.get(HH_EVENTS_ENTITY) is None:
        return "missing"
    return "ok"


def hh_events_warning(status):
    if status == "missing":
        return (f"Power up events entity '{HH_EVENTS_ENTITY}' doesn't exist, so happy hours are ignored. "
                f"It's disabled by default: enable it, then check power_up_events_entity against "
                f"Developer Tools -> States.")
    if status == "not_configured":
        return "power_up_events_entity isn't set, so happy hours are ignored."
    return None


def hh_warn_events(status):
    """Log a missing/unset events entity at most once an hour."""
    if status == "ok":
        _flags["hh_warned_at"] = None
        return
    now = datetime.now(timezone.utc)
    last = _flags["hh_warned_at"]
    if last is not None and now - last < timedelta(hours=1):
        return
    _flags["hh_warned_at"] = now
    if status == "missing":
        log.error(hh_events_warning(status))
    else:
        log.warning(hh_events_warning(status))


def tokens_available():
    """Weekend happy hour tokens left, or None if unknown."""
    return _float_state(HH_TOKENS_ENTITY)


async def hh_auto_join():
    """Book one available weekend happy hour (earliest or latest) if there's a token for it."""
    if not HH_AUTO_JOIN or hh_events_status() != "ok":
        return
    tokens = tokens_available()
    if tokens is not None and tokens < 1:
        log.debug("No weekend happy hour tokens left; not joining")
        return
    now = now_local()
    _, available = hh_sessions()
    candidates = []
    for s in available:
        if s["start"] <= now or not s["code"] or s["availability"].lower() == "full":
            continue
        failed = _hh_joined["failed_at"].get(s["id"])
        if failed is not None and now - failed < timedelta(hours=1):
            continue
        candidates.append(s)
    if not candidates:
        return
    candidates = _by_start(candidates)
    s = candidates[-1] if HH_JOIN_SELECTION == "latest" else candidates[0]

    log.info("Booking weekend happy hour %s %s (code %s, %s tokens left)",
             fmt_day(s["start"]), fmt_window(s["start"], s["end"]), s["code"], tokens)
    try:
        await hass.services.async_call(
            OCTOPUS_DOMAIN, HH_JOIN_SERVICE, {"event_code": s["code"]},
            blocking=True, target={"entity_id": HH_EVENTS_ENTITY},
        )
    except Exception as e:
        _hh_joined["failed_at"][s["id"]] = now
        log.error("Failed to book happy hour %s: %s (will retry in an hour)", s["code"], e)
        return
    _hh_joined["by_us"][s["id"]] = s


async def run_actions(actions, label):
    """Call each configured action. Failures are logged and don't stop the rest."""
    for a in actions:
        if DIAGNOSTICS_ONLY:
            log.warning("DIAGNOSTICS MODE: would call %s.%s data=%s target=%s (%s)",
                        a["domain"], a["service"], a["data"], a["target"], label)
            continue
        log.info("Happy hour %s: calling %s.%s", label, a["domain"], a["service"])
        try:
            await hass.services.async_call(a["domain"], a["service"], dict(a["data"]),
                                           blocking=False, target=a["target"])
        except Exception as e:
            log.error("Happy hour %s action %s.%s failed: %s", label, a["domain"], a["service"], e)


def hh_plan_summary():
    """What happens during a happy hour, for notifications."""
    parts = []
    if HH_BATTERY_CHARGE:
        parts.append(f"battery charging to {HH_CHARGE_SOC}%")
    if HOT_WATER_BOOST:
        parts.append(f"hot water boosting to {HOT_WATER_TEMPERATURE:g}°C")
    if RENAULT_ENABLED:
        parts.append(f"{RENAULT_NAME} set to charge")
    if START_ACTIONS:
        parts.append(f"{len(START_ACTIONS)} other action{'s' if len(START_ACTIONS) != 1 else ''}")
    if not parts:
        return ""
    text = ", ".join(parts)
    return text[0].upper() + text[1:] + "."


def _diag_suffix():
    return " (diagnostics only: nothing will be changed)" if DIAGNOSTICS_ONLY else ""


def boost_minutes(window, now):
    """
    Boost length in whole 15 minutes (the service's step), up to the end of the
    window. A run a minute or two late still rounds up to the full length.
    """
    remaining = (window["end"] - max(now, window["start"])).total_seconds() / 60.0
    return min(12 * 60, int((remaining + 2) // 15) * 15)


async def boost_hot_water(window, now):
    """Boost the water heater until the end of the window. Returns when the boost ends, or None."""
    if not HOT_WATER_BOOST:
        return None
    minutes = boost_minutes(window, now)
    if minutes < 15:
        log.info("Less than 15 minutes of happy hour left; not boosting hot water")
        return None
    hours, mins = divmod(minutes, 60)
    data = {"hours": hours, "minutes": mins, "target_temperature": HOT_WATER_TEMPERATURE}
    if DIAGNOSTICS_ONLY:
        log.warning("DIAGNOSTICS MODE: would call %s.%s on %s with %s",
                    OCTOPUS_DOMAIN, BOOST_SERVICE, WATER_HEATER, data)
    else:
        log.info("Boosting %s to %s°C for %s min", WATER_HEATER, HOT_WATER_TEMPERATURE, minutes)
        try:
            await hass.services.async_call(OCTOPUS_DOMAIN, BOOST_SERVICE, data,
                                           blocking=True, target={"entity_id": WATER_HEATER})
        except Exception as e:
            log.error("Hot water boost on %s failed: %s", WATER_HEATER, e)
            return None
    return max(now, window["start"]) + timedelta(minutes=minutes)


async def hh_announce_new(windows, now):
    """Notify about happy hours we haven't seen before (not after a restart: see hh_seed())."""
    for w in windows:
        if w["end"] <= now or w["id"] in _hh_seen["announced"]:
            continue
        _hh_seen["announced"].add(w["id"])
        if NOTIFY_SCHEDULED and w["start"] > now:
            await notify("Happy Hour booked",
                         f"{fmt_day(w['start'])} {fmt_window(w['start'], w['end'])}. "
                         f"{hh_plan_summary()}{_diag_suffix()}")


async def hh_start_window(window, now):
    """Run the start of a happy hour once; top up the boost if the window grew."""
    rec = _hh_seen["running"].get(window["id"])
    if rec is not None:
        rec["window"] = window
        if HOT_WATER_BOOST and not rec["resumed"] and rec["boost_attempts"] < 3:
            until = rec["boost_until"]
            if until is None or window["end"] - until >= timedelta(minutes=15):
                rec["boost_attempts"] += 1
                rec["boost_until"] = await boost_hot_water(window, now) or until
        return

    rec = {"window": window, "boost_until": None, "boost_attempts": 1, "resumed": False}
    _hh_seen["running"][window["id"]] = rec
    log.info("Happy hour %s started", fmt_window(window["start"], window["end"]))
    rec["boost_until"] = await boost_hot_water(window, now)
    await run_actions(START_ACTIONS, "start")
    summary = hh_plan_summary()
    await notify("Happy Hour started",
                 f"Free electricity until {to_local(window['end']).strftime('%H:%M')}."
                 f"{' ' + summary if summary else ''}{_diag_suffix()}")


async def hh_finish_ended(windows, now):
    """Run the end of every started happy hour that has finished (or been cancelled)."""
    current = {}
    for w in windows:
        current[w["id"]] = w
    for wid in list(_hh_seen["running"].keys()):
        w = current.get(wid)
        if w is not None and now < w["end"]:
            continue
        rec = _hh_seen["running"].pop(wid)
        log.info("Happy hour %s ended", fmt_window(rec["window"]["start"], rec["window"]["end"]))
        await run_actions(END_ACTIONS, "end")
        if NOTIFY_END:
            await notify("Happy Hour over", f"The {fmt_window(rec['window']['start'], rec['window']['end'])} "
                                            f"happy hour has finished.")


def hh_seed(now):
    """
    After a restart, treat known happy hours as already announced, and one
    that's already running as already started (its boost is still running on
    the Cosy and the start actions have run), so nothing fires twice.
    """
    for w in happy_hours():
        if w["end"] <= now:
            continue
        _hh_seen["announced"].add(w["id"])
        if is_running(w, now):
            _hh_seen["running"][w["id"]] = {"window": w, "boost_until": None, "boost_attempts": 0,
                                         "resumed": True}
    _flags["hh_seeded"] = True


def hh_desired(window, now):
    """What the charge slot should hold right now."""
    empty = {"window": EMPTY_SLOT, "current": None, "soc": None, "enabled": False}
    if not HH_BATTERY_CHARGE or window is None:
        return empty
    if now < window["start"] - timedelta(minutes=HH_PREPARE_MINUTES) or now >= window["end"]:
        return empty
    return {
        "window": fmt_window(window["start"], window["end"]),
        "current": HH_CHARGE_CURRENT,
        "soc": HH_CHARGE_SOC,
        "enabled": True,
    }


# -----------------------------
# Renault: move the car's charge schedule into happy hours
# -----------------------------
RENAULT_DOMAIN = "renault"
RENAULT_SERVICE = "charge_set_schedules"
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
RENAULT_NORMAL_START, RENAULT_NORMAL_END = _parse_window(RENAULT_NORMAL_WINDOW)


def _renault_time(dt):
    """Renault schedules are in UTC, as "THH:MMZ"."""
    return dt.astimezone(timezone.utc).strftime("T%H:%MZ")


def _floor_quarter(dt):
    return dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)


def car_happy_schedule(window):
    """(start, minutes) covering the happy hour, in the car's 15-minute steps."""
    start = _floor_quarter(window["start"])
    minutes = int(math.ceil((window["end"] - start).total_seconds() / 60.0 / 15.0)) * 15
    return start, max(15, minutes)


def car_normal_schedule(now):
    """(start, minutes) of the normal window, using today's UTC offset."""
    start = _at_local_time(now, RENAULT_NORMAL_START)
    s = RENAULT_NORMAL_START.hour * 60 + RENAULT_NORMAL_START.minute
    e = RENAULT_NORMAL_END.hour * 60 + RENAULT_NORMAL_END.minute
    return start, (e - s) % (24 * 60) or 24 * 60


def car_desired(window, now):
    """("happy_hour" | "normal", start, minutes) the car's schedule should hold now."""
    if window is not None and window["start"] - timedelta(minutes=RENAULT_PREPARE_MINUTES) <= now < window["end"]:
        start, minutes = car_happy_schedule(window)
        return "happy_hour", start, minutes
    start, minutes = car_normal_schedule(now)
    return "normal", start, minutes


def car_schedule_data(start, minutes):
    """The renault.charge_set_schedules data: the same window every day of the week."""
    day = {"startTime": _renault_time(start), "duration": minutes}
    schedule = {"id": RENAULT_SCHEDULE_ID, "activated": True}
    for name in WEEKDAYS:
        schedule[name] = dict(day)
    return {"vehicle": RENAULT_VEHICLE, "schedules": [schedule]}


def car_window_text(start, minutes):
    return fmt_window(start, start + timedelta(minutes=minutes))


async def car_schedule_sync(window, now):
    """
    Set the car's charge schedule to the happy hour (from prepare_minutes before
    it) and back to the normal window when it's over. Only changes are sent.
    A failed write is retried every retry_minutes.
    """
    mode, start, minutes = car_desired(window, now)
    key = f"{_renault_time(start)}/{minutes}"
    if key == _car["applied"]:
        return
    if _car["wanted"] != key:
        _car["wanted"], _car["failed_at"], _car["failures"] = key, None, 0
    if _car["failed_at"] is not None and now - _car["failed_at"] < timedelta(minutes=RENAULT_RETRY_MINUTES):
        return

    text = car_window_text(start, minutes)
    data = car_schedule_data(start, minutes)
    label = "the happy hour" if mode == "happy_hour" else "its normal time"
    if DIAGNOSTICS_ONLY:
        log.warning("DIAGNOSTICS MODE: would call %s.%s with %s (%s charging %s)",
                    RENAULT_DOMAIN, RENAULT_SERVICE, data, RENAULT_NAME, text)
    else:
        log.info("Setting %s charge schedule %s to %s (%s)", RENAULT_NAME, RENAULT_SCHEDULE_ID, text, label)
        try:
            await hass.services.async_call(RENAULT_DOMAIN, RENAULT_SERVICE, data, blocking=True)
        except Exception as e:
            _car["failed_at"] = now
            _car["failures"] += 1
            log.error("Setting %s charge schedule to %s failed: %s (retrying in %s min)",
                      RENAULT_NAME, text, e, RENAULT_RETRY_MINUTES)
            if RENAULT_NOTIFY and _car["failures"] >= 3 and _car["fail_notified"] != key:
                _car["fail_notified"] = key
                await notify(f"{RENAULT_NAME} schedule not set",
                             f"Couldn't set the charge schedule to {text} ({label}). Still retrying.")
            return
    previous = _car["applied_mode"]
    _car["applied"], _car["applied_mode"] = key, mode
    _car["failed_at"], _car["failures"], _car["last_ok"] = None, 0, now
    if RENAULT_NOTIFY and mode == "normal" and previous == "happy_hour":
        await notify(f"{RENAULT_NAME} schedule restored",
                     f"Charging back to {text}.{_diag_suffix()}")


def car_status():
    if not RENAULT_ENABLED:
        return None
    mode, start, minutes = car_desired(_last["hh_window"], now_local())
    return {
        "vehicle": RENAULT_VEHICLE,
        "schedule_id": RENAULT_SCHEDULE_ID,
        "mode": _car["applied_mode"],
        "wanted": mode,
        "window": car_window_text(start, minutes),
        "in_sync": _car["applied"] == f"{_renault_time(start)}/{minutes}",
        "failures": _car["failures"],
        "last_set": _car["last_ok"].isoformat() if _car["last_ok"] else None,
    }


# -----------------------------
# EV-Follow planner: charge only while the car is really being smart-charged
# -----------------------------
def ev_in_core_window(dt):
    if not MANAGE_CORE_SLOT:
        return False
    return in_core_window(dt)


def fmt_slot(start, end):
    if start is None or end is None:
        return EMPTY_SLOT
    return fmt_window(start, end)


def _state(entity_id):
    s = _state_obj(entity_id)
    return s.state if s else None


def car_charging():
    """True when the charger is drawing current during a *started* Octopus dispatch."""
    switch_state = _state(EV_CHARGER_SWITCH)
    dispatch_state = _state(EV_DISPATCH_SENSOR)
    current_raw = _state(EV_CHARGER_CURRENT)
    try:
        amps = float(current_raw)
    except (TypeError, ValueError):
        amps = 0.0
    charging = switch_state == "on" and dispatch_state == "on" and amps >= EV_MIN_CURRENT_AMPS
    info = {"charger_switch": switch_state, "charger_amps": amps, "dispatch": dispatch_state}
    return charging, info


def ev_charging_confirmed(now):
    """
    (confirmed, info): charging that has lasted at least debounce_seconds, so
    a blip (pre-heating, a short top-up) doesn't start the battery.
    """
    charging, info = car_charging()
    if not charging:
        _ev["charging_since"] = None
        _ev["debounced"] = False
        return False, info
    if _ev["charging_since"] is None:
        _ev["charging_since"] = now
    confirmed = _ev.get("debounced") or \
        (now - _ev["charging_since"]).total_seconds() >= EV_DEBOUNCE_SECONDS
    return bool(confirmed), info


def ev_desired_slot(now, charging):
    """Return the (start, end) the EV slot should hold right now, or (None, None)."""
    active = _ev["end"] is not None and now < _ev["end"]
    lookahead = now + timedelta(minutes=EV_EXTEND_LEAD_MINUTES)

    if charging and not ev_in_core_window(lookahead):
        # End at the next boundary after now+lead, so a few minutes before
        # :00/:30 this rolls on by another half-hour.
        end = next_half_hour(lookahead)
        start = _ev["start"] if active else floor_half_hour(now)
        if active and _ev["end"] > end:
            end = _ev["end"]
        return start, end

    if active:
        # Car stopped (or core window reached): let the current half-hour run out.
        return _ev["start"], _ev["end"]
    return None, None


# -----------------------------
# Slots: what each feature's slot should hold, and the writes to get there
# -----------------------------
def empty_want():
    return {"window": EMPTY_SLOT, "current": None, "soc": None, "enabled": False}


def slot_entities(key):
    """((start, end) times, current, soc, switch) Solis Modbus entities for one of our slots."""
    kind, num = SLOT_DEFS[key]
    return slot_entity_ids(kind, num)


def slot_ops(key, want, current_step=1):
    """
    The writes that take slot `key` from what we last wrote to `want`, in a
    safe order: switch off before clearing, switch on only after the time,
    current and SOC are in place. current/soc of None are left alone.
    """
    have = _slots[key]
    unknown = have["unknown"]
    time_ids, current_id, soc_id, switch_id = slot_entities(key)
    ops = []

    if not want["enabled"]:
        if unknown or have["enabled"]:
            ops.append(("switch", switch_id, False))
        if unknown or have["window"] != EMPTY_SLOT:
            ops.append(("time", time_ids, EMPTY_SLOT))
        return ops

    fresh = unknown or not have["enabled"]
    moved = fresh or have["window"] != want["window"]
    if moved:
        ops.append(("time", time_ids, want["window"]))
    if want["current"] is not None and (
            moved or have["current"] is None
            or abs(want["current"] - have["current"]) >= max(1, current_step)):
        ops.append(("value", current_id, str(want["current"])))
    if want["soc"] is not None and (moved or want["soc"] != have["soc"]):
        ops.append(("value", soc_id, str(want["soc"])))
    if fresh:
        ops.append(("switch", switch_id, True))
    return ops


def apply_slot_ops(key, ops, ends=None):
    """Record successful writes for slot `key`."""
    program = _slots[key]
    time_ids, current_id, soc_id, switch_id = slot_entities(key)
    for op in ops:
        if op[0] == "switch":
            program["enabled"] = op[2]
        elif op[1] == time_ids:
            program["window"] = op[2]
        elif op[1] == current_id:
            program["current"] = int(op[2])
        elif op[1] == soc_id:
            program["soc"] = int(op[2])
    if not program["enabled"] and program["window"] == EMPTY_SLOT:
        program["current"] = None
        program["soc"] = None
        program["ends"] = None
    elif ends is not None:
        program["ends"] = ends
    program["unknown"] = False


def still_running(key, now):
    """Our slot is enabled and its window hasn't finished by itself yet."""
    program = _slots[key]
    if not program["enabled"] or program["unknown"]:
        return False
    return program["ends"] is None or program["ends"] > now


def other_charge_slots():
    """Charge slots none of our features use."""
    used = set()
    for key in active_slot_keys():
        kind, num = SLOT_DEFS[key]
        if kind == "charge":
            used.add(num)
    out = []
    for num in range(1, 7):
        if num not in used:
            out.append(num)
    return out


def active_slot_keys():
    keys = []
    if not INVERTER_CONTROL:
        return keys
    if MANAGE_CORE_SLOT:
        keys.append("core")
    if EV_ENABLED:
        keys.append("ev")
    if HH_BATTERY_CHARGE:
        keys.append("happy_hour")
    if PD_ENABLED or PEAK_EXPORT:
        keys.append("export")
    return keys


# -----------------------------
# Arbiter: one decision for every slot, one batch of writes
# -----------------------------
def plan_ev(now):
    """(want, info, on): the EV slot's window, the charger info, and whether it's charging now."""
    if not EV_ENABLED:
        return empty_want(), {}, False
    charging, info = ev_charging_confirmed(now)
    _ev["last_charging"] = charging
    _ev["info"] = info
    start, end = ev_desired_slot(now, charging)
    if start is None:
        return empty_want(), info, False
    want = {"window": fmt_slot(start, end), "current": None, "soc": None, "enabled": True,
            "start": start, "end": end}
    return want, info, start <= now < end


def core_want():
    return {"window": CORE_WINDOW.replace(" ", ""), "current": None, "soc": None, "enabled": True}


def arbitrate(now, ev_want, ev_on, hh_window, hh_want, export_window, export_want):
    """
    Apply the priority order to the features' wishes. Returns (ev_want, hh_want,
    export_want, notes). Axle events Axle drives itself are handled by the
    caller (no writes at all).
    """
    notes = []
    if export_want["enabled"] and axle_dispatch(export_window):
        # An Axle export we run comes first: no grid charging into it.
        a_start, a_end = export_window["start"], export_window["end"]
        if ev_want["enabled"] and overlaps(ev_want["start"], ev_want["end"], a_start, a_end):
            if _ev["end"] is not None and now < _ev["end"] <= a_start and _slots["ev"]["enabled"]:
                # Let the half-hour that's already running finish, but don't extend into the event.
                ev_want = dict(ev_want, window=fmt_slot(_ev["start"], _ev["end"]),
                               start=_ev["start"], end=_ev["end"])
            else:
                ev_want = empty_want()
            notes.append("EV charging paused: Axle export")
        if hh_want["enabled"] and overlaps(hh_window["start"], hh_window["end"], a_start, a_end):
            if hh_window["start"] < a_start and now < a_start:
                # Free charging right up to the event, then stop.
                hh_want = dict(hh_want, window=fmt_window(hh_window["start"], a_start))
            else:
                hh_want = empty_want()
            notes.append("happy hour charging paused: Axle export")
        return ev_want, hh_want, export_want, notes
    if export_want["enabled"] and ev_on:
        export_want = empty_want()
        notes.append("export paused: EV charging")
    if export_want["enabled"] and hh_want["enabled"] and export_window is not None \
            and overlaps(hh_window["start"], hh_window["end"], export_window["start"], export_window["end"]):
        if export_window["kind"] == "power_down":
            hh_want = empty_want()
            notes.append("happy hour charging paused: Power Down")
        else:
            export_want = empty_want()
            notes.append("peak export paused: happy hour")
    return ev_want, hh_want, export_want, notes


def order_ops(per_slot):
    """All slots' writes, with switch-offs and clears first so nothing overlaps."""
    first = []
    then = []
    for key in active_slot_keys():
        ops = per_slot.get(key) or []
        if ops and ops[0][0] == "switch" and ops[0][2] is False:
            first.extend(ops)
        else:
            then.extend(ops)
    return first + then


def clear_other_ops():
    ops = []
    for num in other_charge_slots():
        time_ids, _, _, switch_id = slot_entity_ids("charge", num)
        ops.append(("switch", switch_id, False))
        ops.append(("time", time_ids, EMPTY_SLOT))
    return ops


async def reconcile(reason):
    if not _flags["started"]:
        log.debug("[%s] not started yet", reason)
        return
    # Serialise: pyscript runs on the event loop, so check-and-set is atomic.
    while _flags["busy"]:
        await asyncio.sleep(1)
    _flags["busy"] = True
    try:
        await _reconcile(reason)
    finally:
        _flags["busy"] = False


async def _reconcile(reason):
    now = now_local()
    pd_status = pd_events_status() if PD_ENABLED else "disabled"
    if PD_ENABLED:
        pd_warn_events(pd_status)
    hh_status = hh_events_status() if HH_ENABLED else "disabled"
    if HH_ENABLED:
        hh_warn_events(hh_status)
    axle_status = axle_entities_status()
    warn_axle(axle_status)

    # What each feature wants.
    ev_want, ev_info, ev_on = plan_ev(now)
    hh_windows = happy_hours() if HH_ENABLED else []
    hh_window = hh_target_window(hh_windows, now)
    hh_want = hh_desired(hh_window, now)
    export_window, later = export_target_window(now)
    plan = None
    if export_window is not None and (not is_axle(export_window) or axle_dispatch(export_window)):
        plan = await plan_for(export_window, now, later,
                              drain_to_floor=axle_dispatch(export_window) and not AXLE_KEEP_RESERVE)
    export_want = export_desired(export_window, plan, now)

    axle_running = stand_aside(export_window, now)
    notes = []
    wants = {"core": core_want(), "ev": ev_want, "happy_hour": hh_want, "export": export_want}
    per_slot = {}
    if axle_running:
        # Axle is driving the battery: stay off the inverter. The only write
        # allowed is switching off a slot of ours that would still be running.
        notes.append(f"standing aside: Axle {export_window['kind'][len('axle_'):]} event")
        for key in active_slot_keys():
            if key != "core" and still_running(key, now):
                per_slot[key] = slot_ops(key, empty_want())
                wants[key] = empty_want()
    else:
        ev_want, hh_want, export_want, notes = arbitrate(now, ev_want, ev_on, hh_window, hh_want,
                                                         export_window, export_want)
        wants["ev"] = ev_want
        wants["happy_hour"] = hh_want
        wants["export"] = export_want
        for key in active_slot_keys():
            step = CURRENT_STEP_AMPS if key == "export" else 1
            per_slot[key] = slot_ops(key, wants[key], step)
        if not wants["export"]["enabled"] and export_window is not None \
                and (not is_axle(export_window) or axle_dispatch(export_window)) \
                and plan is not None and not notes and not worth_exporting(plan):
            notes.append("export skipped: not enough spare energy")

    ops = order_ops(per_slot)
    if CLEAR_OTHER_SLOTS and not _flags["others_cleared"] and not axle_running:
        ops = clear_other_ops() + ops

    if ops:
        log.info("[%s] writing %s (%s)", reason, ops, "; ".join(notes) or "no conflicts")
        ok = await solis_apply(ops)
        _flags["last_write_ok"] = ok
        if ok:
            if CLEAR_OTHER_SLOTS and not axle_running:
                _flags["others_cleared"] = True
            ends = {"ev": ev_want.get("end"), "happy_hour": hh_window["end"] if hh_window else None,
                    "export": export_window["end"] if export_window else None}
            for key in per_slot:
                if per_slot[key]:
                    apply_slot_ops(key, per_slot[key], ends.get(key))
            if per_slot.get("ev"):
                # EV-Follow remembers the window it programmed, so it can extend it.
                if wants["ev"]["enabled"]:
                    _ev["start"], _ev["end"] = wants["ev"]["start"], wants["ev"]["end"]
                else:
                    _ev["start"], _ev["end"] = None, None
            await after_write(now, wants, export_window, plan)
        else:
            # Partial writes leave the inverter in an unknown state; rewrite it all next time.
            for key in per_slot:
                if per_slot[key]:
                    _slots[key]["unknown"] = True
            log.error("Failed to write the inverter; will retry on next check")
    else:
        log.debug("[%s] no change (%s)", reason, "; ".join(notes) or "no conflicts")

    # Happy hour side effects (hot water, actions, notifications) don't touch the inverter.
    if HH_ENABLED:
        await hh_announce_new(hh_windows, now)
        await hh_finish_ended(hh_windows, now)
        if is_running(hh_window, now):
            await hh_start_window(hh_window, now)
    if RENAULT_ENABLED:
        await car_schedule_sync(hh_window, now)

    _last["plan"] = plan
    _last["export_window"] = export_window
    _last["hh_window"] = hh_window
    _last["decision"] = "; ".join(notes) or None
    _flags["last_reason"] = reason
    publish_all(now, export_window, plan, hh_window, hh_windows, ev_info,
                pd_status, hh_status, axle_status)


async def after_write(now, wants, export_window, plan):
    """Notifications once an export is armed."""
    if not wants["export"]["enabled"] or export_window is None or plan is None:
        return
    if export_window["id"] in _pd_joined["notified"]:
        return
    kind = export_window["kind"]
    if kind == "peak" and not NOTIFY_PEAK:
        return
    _pd_joined["notified"].add(export_window["id"])
    title = {"power_down": "Power Down export planned", "axle_export": "Axle export planned"}.get(
        kind, "Peak export planned")
    kept = "for the evening" if kind != "axle_export" or AXLE_KEEP_RESERVE else "in the battery"
    await notify(
        title,
        f"{plan['window']}: exporting ~{plan['predicted_export_kwh']} kWh at "
        f"{plan['discharge_current']} A, stopping at {plan['stop_soc']}% "
        f"({plan['reserve_kwh']} kWh kept {kept}).",
    )


# -----------------------------
# Status entities
# -----------------------------
def publish_all(now, export_window, plan, hh_window, hh_windows, ev_info,
                pd_status, hh_status, axle_status):
    publish_overview(now, export_window, hh_window)
    if EV_ENABLED:
        publish_ev_status(ev_info)
    if HH_ENABLED:
        publish_hh_status(hh_window, hh_windows, now, hh_status)
    if PD_ENABLED or PEAK_EXPORT or AXLE_EVENTS:
        publish_export_status(export_window, plan, now, pd_status, axle_status)


def _slot_attrs(key):
    program = _slots[key]
    kind, num = SLOT_DEFS[key]
    return {"slot": f"{kind} {num}", "window": program["window"], "enabled": program["enabled"],
            "current": program["current"], "soc": program["soc"], "unknown": program["unknown"]}


def publish_overview(now, export_window, hh_window):
    if stand_aside(export_window, now):
        state_value = "axle_event"
    elif _slots["export"]["enabled"] and export_window is not None and export_window["start"] <= now:
        state_value = "exporting"
    elif (_slots["ev"]["enabled"] and _ev["end"] is not None and now < _ev["end"]) or \
            (_slots["happy_hour"]["enabled"] and is_running(hh_window, now)):
        state_value = "charging"
    else:
        state_value = "idle"
    slots = {}
    for key in active_slot_keys():
        slots[key] = _slot_attrs(key)
    state.set(OVERVIEW_SENSOR, state_value, new_attributes={
        "decision": _last["decision"],
        "slots": slots,
        "features": {"ev_follow": EV_ENABLED, "happy_hour": HH_ENABLED, "power_down": PD_ENABLED,
                     "peak_export": PEAK_EXPORT, "axle": AXLE_EVENTS,
                     "renault": RENAULT_ENABLED},
        "slot_problems": SLOT_PROBLEMS or None,
        "inverter_prefix": inverter_prefix() if INVERTER_CONTROL else None,
        "last_reason": _flags["last_reason"],
        "last_write_ok": _flags["last_write_ok"],
        "diagnostics_only": DIAGNOSTICS_ONLY,
        "version": VERSION,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "friendly_name": "Solis Octopus",
        "icon": "mdi:home-battery",
    })


def publish_ev_status(info):
    program = _slots["ev"]
    state.set(
        EV_STATUS_SENSOR,
        "charging" if program["enabled"] and _ev["end"] is not None else "idle",
        new_attributes={
            "ev_slot": EV_SLOT,
            "programmed_slot": program["window"],
            "slot_state_unknown": program["unknown"],
            "ev_charging": _ev["last_charging"],
            "charger_switch": info.get("charger_switch"),
            "charger_amps": info.get("charger_amps"),
            "dispatch_state": info.get("dispatch"),
            "core_window": CORE_WINDOW if MANAGE_CORE_SLOT else None,
            "last_reason": _flags["last_reason"],
            "last_write_ok": _flags["last_write_ok"],
            "diagnostics_only": DIAGNOSTICS_ONLY,
            "version": VERSION,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "friendly_name": "Solis EV charge slot",
            "icon": "mdi:home-battery",
        },
    )


def publish_hh_status(window, windows, now, events_status):
    program = _slots["happy_hour"]
    running = is_running(window, now)
    if window is None:
        status = "idle"
    elif running:
        status = "active"
    elif program["enabled"]:
        status = "armed"
    else:
        status = "scheduled"

    rec = _hh_seen["running"].get(window["id"]) if running else None
    upcoming = []
    for w in windows:
        if w["end"] > now:
            upcoming.append(f"{fmt_day(w['start'])} {fmt_window(w['start'], w['end'])}")

    attrs = {
        "window": fmt_window(window["start"], window["end"]) if window else None,
        "start": window["start"].isoformat() if window else None,
        "end": window["end"].isoformat() if window else None,
        "minutes_to_start": max(0, int((window["start"] - now).total_seconds() // 60)) if window else None,
        "booked": upcoming,
        "tokens": tokens_available(),
        "auto_join": HH_AUTO_JOIN,
        "battery_charge": HH_BATTERY_CHARGE,
        "charge_slot": HH_SLOT if HH_BATTERY_CHARGE else None,
        "programmed_window": program["window"] if HH_BATTERY_CHARGE else None,
        "programmed_current": program["current"] if HH_BATTERY_CHARGE else None,
        "programmed_soc": program["soc"] if HH_BATTERY_CHARGE else None,
        "slot_enabled": program["enabled"] if HH_BATTERY_CHARGE else None,
        "slot_state_unknown": program["unknown"] if HH_BATTERY_CHARGE else None,
        "last_write_ok": _flags["last_write_ok"],
        "hot_water_boost": HOT_WATER_BOOST,
        "water_heater": WATER_HEATER or None,
        "hot_water_temperature": HOT_WATER_TEMPERATURE if HOT_WATER_BOOST else None,
        "boost_until": rec["boost_until"].isoformat() if rec and rec["boost_until"] else None,
        "start_actions": len(START_ACTIONS),
        "end_actions": len(END_ACTIONS),
        "car_schedule": car_status(),
        "last_reason": _flags["last_reason"],
        "power_up_events": events_status,
        "diagnostics_only": DIAGNOSTICS_ONLY,
        "version": VERSION,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "friendly_name": "Octopus Happy Hour",
        "icon": "mdi:glass-cocktail",
    }
    warning = hh_events_warning(events_status)
    if warning:
        attrs["warning"] = warning
    state.set(HH_STATUS_SENSOR, status, new_attributes=attrs)
    state.set(HH_ACTIVE_SENSOR, "on" if running else "off", new_attributes={
        "friendly_name": "Octopus Happy Hour active",
        "icon": "mdi:glass-cocktail",
    })


def publish_export_status(session, plan, now, events_status="ok", axle_status="disabled"):
    program = _slots["export"]
    running = session is not None and session["start"] <= now < session["end"]
    active = running and session["kind"] == "power_down"
    if session is None:
        status = "idle"
    elif is_axle(session) and not axle_dispatch(session):
        status = "axle_event" if running else "axle_upcoming"
    elif _last["decision"] and "export paused" in _last["decision"]:
        status = "paused"
    elif plan is not None and not worth_exporting(plan):
        status = "no_spare_energy"
    elif running and program["enabled"]:
        status = "exporting"
    elif program["enabled"]:
        status = "armed"
    else:
        status = "planned"

    attrs = {
        "session_id": session["id"] if session else None,
        "session_start": session["start"].isoformat() if session else None,
        "session_end": session["end"].isoformat() if session else None,
        "octopoints_per_kwh": session["octopoints_per_kwh"] if session else None,
        "discharge_slot": DISCHARGE_SLOT,
        "programmed_window": program["window"],
        "programmed_current": program["current"],
        "programmed_stop_soc": program["soc"],
        "slot_enabled": program["enabled"],
        "slot_state_unknown": program["unknown"],
        "decision": _last["decision"],
        "last_reason": _flags["last_reason"],
        "last_write_ok": _flags["last_write_ok"],
        "power_down_events": events_status,
        "axle_events": axle_status,
        "diagnostics_only": DIAGNOSTICS_ONLY,
        "version": VERSION,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "friendly_name": "Solis Power Down",
        "icon": "mdi:transmission-tower-export",
    }
    if is_axle(session):
        attrs["window_kind"] = session["kind"]
        attrs["axle_event_type"] = session["kind"][len("axle_"):]
        attrs["axle_event_start"] = to_local(session["start"]).isoformat()
        attrs["axle_event_end"] = to_local(session["event_end"]).isoformat()
        attrs["axle_control"] = AXLE_CONTROL
    warnings = []
    for w in (pd_events_warning(events_status) if PD_ENABLED else None, axle_warning(axle_status)):
        if w:
            warnings.append(w)
    if warnings:
        attrs["warning"] = " ".join(warnings)
    if plan:
        for key, value in plan.items():
            if key not in ("session_start", "session_end"):
                attrs[key] = value
    state.set(STATUS_SENSOR, status, new_attributes=attrs)
    state.set(ACTIVE_SENSOR, "on" if active else "off", new_attributes={
        "friendly_name": "Solis Power Down active",
        "icon": "mdi:transmission-tower-export",
    })


# -----------------------------
# Triggers
# -----------------------------
if EV_ENABLED and EV_DISPATCH_SENSOR:
    EV_T1, EV_T2, EV_T3 = EV_CHARGER_SWITCH, EV_CHARGER_CURRENT, EV_DISPATCH_SENSOR
else:
    EV_T1 = EV_T2 = EV_T3 = "input_boolean.solis_octopus_ev_unconfigured"


@time_trigger("startup")
async def solis_octopus_startup():
    log.info("=== Solis Octopus v%s starting (ev_follow=%s, happy_hour=%s, power_down=%s, "
             "peak_export=%s, axle=%s, renault=%s, tz %s, diagnostics_only=%s) ===", VERSION, EV_ENABLED,
             HH_ENABLED, PD_ENABLED, PEAK_EXPORT,
             ("self-dispatch" if AXLE_CONTROL else "stand aside") if AXLE_EVENTS else False,
             RENAULT_ENABLED, INVERTER_TIMEZONE, DIAGNOSTICS_ONLY)
    if SLOT_PROBLEMS:
        log.error("Not starting: fix the slots config first (%s)", "; ".join(SLOT_PROBLEMS))
        return
    if EV_ENABLED and not EV_DISPATCH_SENSOR:
        log.error("ev_follow.dispatch_sensor isn't set; EV-Follow won't charge the battery")
    if PD_ENABLED and (not PD_EVENTS_ENTITY or hass.states.get(PD_EVENTS_ENTITY) is None):
        log.warning("Power down events entity '%s' not found yet - check power_down.power_down_events_entity",
                    PD_EVENTS_ENTITY)
    if HH_ENABLED and (not HH_EVENTS_ENTITY or hass.states.get(HH_EVENTS_ENTITY) is None):
        log.warning("Power up events entity '%s' not found yet - check happy_hour.power_up_events_entity "
                    "(it's disabled by default in the Octopus integration)", HH_EVENTS_ENTITY)
    if HOT_WATER_BOOST and hass.states.get(WATER_HEATER) is None:
        log.warning("Water heater '%s' not found yet - check happy_hour.heat_pump_id / water_heater_entity",
                    WATER_HEATER)
    if (PD_ENABLED or PEAK_EXPORT) and not SOC_SENSOR:
        log.error("battery.soc_sensor not configured - can't plan exports")
    # Let the Octopus / Solis / Axle integrations finish loading.
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    if HH_ENABLED:
        hh_seed(now_local())
    check_inverter_entities()
    await pd_auto_join()
    await hh_auto_join()
    _flags["started"] = True
    # Every slot starts unknown, so this also puts the inverter in a known state.
    await reconcile("startup")


@state_trigger(EV_T1, EV_T2, EV_T3)
async def solis_octopus_ev_changed(var_name=None, value=None, old_value=None):
    charging, _ = car_charging()
    if not EV_ENABLED or charging == _ev["last_charging"]:
        return
    if not charging:
        log.info("Car stopped charging (%s=%s); the battery finishes the current half-hour", var_name, value)
        await reconcile("car stopped")
        return
    # Debounce: only one pending start at a time; re-check after the wait.
    task.unique("solis_octopus_ev_debounce", kill_me=True)
    log.info("Car charging detected (%s=%s); confirming in %ss", var_name, value, EV_DEBOUNCE_SECONDS)
    await asyncio.sleep(EV_DEBOUNCE_SECONDS)
    still_charging, _ = car_charging()
    if still_charging:
        _ev["debounced"] = True
        await reconcile("charging started")
    else:
        log.info("Charging did not persist for %ss - ignoring", EV_DEBOUNCE_SECONDS)


# Not state_triggers: in pyscript expressions `event` is its own built-in, so
# `event.<entity>` can't be used there. state_changed covers state and
# attribute changes for any domain.
@event_trigger("state_changed", f"entity_id == '{PD_EVENTS_ENTITY}'")
async def solis_octopus_power_down_changed(entity_id=None, **kwargs):
    task.unique("solis_octopus_power_down", kill_me=True)
    await pd_auto_join()
    await reconcile("power down events changed")


@event_trigger("state_changed", f"entity_id == '{HH_EVENTS_ENTITY}'")
async def solis_octopus_happy_hour_changed(entity_id=None, **kwargs):
    task.unique("solis_octopus_happy_hour", kill_me=True)
    await hh_auto_join()
    await reconcile("happy hour events changed")


@event_trigger("state_changed", "entity_id in " + repr(AXLE_SENSORS))
async def solis_octopus_axle_changed(entity_id=None, **kwargs):
    task.unique("solis_octopus_axle", kill_me=True)
    await reconcile("axle event changed")


@state_trigger(f"{PEAK_SWITCH or 'input_boolean.solis_peak_export_unconfigured'}")
async def solis_octopus_peak_switch_changed(var_name=None, value=None):
    log.info("Peak export switch %s is now %s", var_name, value)
    await reconcile("peak switch")


@time_trigger("cron(* * * * *)")
async def solis_octopus_periodic():
    # Every minute: happy hour starts are on time, EV slots extend just before
    # :00/:30, and missed changes are caught. Only changes reach the inverter.
    await reconcile("periodic")


@time_trigger("cron(7 * * * *)")
async def solis_octopus_join_sweep():
    await pd_auto_join()
    await hh_auto_join()


# -----------------------------
# Services (for testing from Developer Tools -> Actions)
# -----------------------------
@service
async def solis_octopus_reconcile():
    """Join sessions (if auto_join), re-plan every feature and update the inverter now."""
    await pd_auto_join()
    await hh_auto_join()
    await reconcile("manual")


@service
async def solis_octopus_clear():
    """Switch off and clear every slot this app uses (they're rewritten on the next check)."""
    ops = []
    for key in active_slot_keys():
        _slots[key]["unknown"] = True
        ops.extend(slot_ops(key, empty_want()))
    ok = await solis_apply(ops)
    if ok:
        for key in active_slot_keys():
            _slots[key] = _empty_program()
            _slots[key]["unknown"] = False
        _ev["start"], _ev["end"] = None, None


@service
async def solis_octopus_preview():
    """Log what every feature would do next, without writing anything."""
    now = now_local()
    export_window, later = export_target_window(now)
    if export_window is None and PD_ENABLED:
        _, available = pd_sessions()
        for s in available:
            if s["end"] > now and (export_window is None or s["start"] < export_window["start"]):
                export_window = s
    if export_window is None:
        log.info("Preview: no upcoming export window")
    elif is_axle(export_window) and not axle_dispatch(export_window):
        log.info("Preview: next is an Axle %s event %s; the app stands aside",
                 export_window["kind"][len("axle_"):], fmt_window(export_window["start"], export_window["end"]))
    else:
        plan = await plan_for(export_window, now, later,
                              drain_to_floor=axle_dispatch(export_window) and not AXLE_KEEP_RESERVE)
        log.info("Preview export %s: %s", fmt_window(export_window["start"], export_window["end"]), plan)
    if HH_ENABLED:
        windows = happy_hours()
        _, hh_available = hh_sessions()
        window = hh_target_window(windows, now)
        upcoming = []
        for w in windows:
            if w["end"] > now:
                upcoming.append(f"{fmt_day(w['start'])} {fmt_window(w['start'], w['end'])}")
        bookable = []
        for s in _by_start(hh_available):
            if s["end"] > now:
                bookable.append(f"{fmt_day(s['start'])} {fmt_window(s['start'], s['end'])} "
                                f"({s['code']}, {s['availability'] or 'unknown'})")
        log.info("Preview happy hours: booked=%s available=%s tokens=%s", upcoming or "none",
                 bookable or "none", tokens_available())
        if window is None:
            log.info("Preview: no happy hour booked")
        else:
            log.info("Preview happy hour %s %s: charge slot %s, hot water boost %s. %s",
                     fmt_day(window["start"]), fmt_window(window["start"], window["end"]),
                     hh_desired(window, window["start"]) if HH_BATTERY_CHARGE else "off",
                     f"{boost_minutes(window, window['start'])} min at {HOT_WATER_TEMPERATURE:g}°C"
                     if HOT_WATER_BOOST else "off", hh_plan_summary())
        if RENAULT_ENABLED:
            mode, start, minutes = car_desired(window, now)
            log.info("Preview %s charge schedule: %s %s now%s", RENAULT_NAME, mode,
                     car_window_text(start, minutes),
                     "" if window is None else
                     f", {car_window_text(*car_happy_schedule(window))} from "
                     f"{to_local(window['start'] - timedelta(minutes=RENAULT_PREPARE_MINUTES)).strftime('%H:%M')}")
    if EV_ENABLED:
        charging, info = car_charging()
        log.info("Preview EV-Follow: charging=%s %s, programmed %s", charging, info, _slots["ev"]["window"])
