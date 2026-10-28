"""
Solis Octopus (pyscript app)

One app that owns the Solis battery for everything Octopus (and Axle) related,
so features never overwrite each other on the inverter:

- EV-Follow: charge the battery from the grid only while the car is really
  being smart-charged by Octopus Intelligent (charge slot 2), with the core
  overnight window kept in charge slot 1.
- Happy Hour: charge at full speed during Octoplus Happy Hours / Power Ups
  (charge slot 3), boost the Cosy hot water, run your start/end actions.
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

All SolisCloud traffic goes through one client: one login per pass, one time
sync a day, and one lock around every write so the slot switch bitmask
("yuanzhi") is always read and written as a pair.

Requires 6-slot Solis firmware (HMI >= 4B00) with time-of-use (timed
charge/discharge) enabled, the Octopus Energy integration, and optionally the
Axle Energy integration.
"""
import json
import re
import math
import asyncio
import logging
import hashlib
import hmac
import base64
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from zoneinfo import ZoneInfo
from homeassistant.helpers.aiohttp_client import async_get_clientsession

log = logging.getLogger("pyscript.solis_octopus")
log.setLevel(logging.DEBUG)

VERSION = "1.0.0"

# -----------------------------
# SolisCloud endpoints / constants
# -----------------------------
BASE_URL = "https://www.soliscloud.com:13333"
VERB = "POST"
LOGIN_URL = "/v2/api/login"
CONTROL_URL = "/v2/api/control"
AT_READ_URL = "/v2/api/atRead"
AT_READ_BATCH_URL = "/v2/api/atReadBatch"
INVERTER_LIST_URL = "/v1/api/inverterList"

# 6-slot firmware (HMI >= 4B00) slot CIDs, slot 1..6
CHARGE_TIME_CIDS = ["5946", "5949", "5952", "5955", "5958", "5961"]
CHARGE_CURRENT_CIDS = ["5948", "5951", "5954", "5957", "5960", "5963"]
CHARGE_SOC_CIDS = ["5928", "5929", "5930", "5931", "5932", "5933"]
DISCHARGE_TIME_CIDS = ["5964", "5968", "5972", "5976", "5980", "5987"]
DISCHARGE_CURRENT_CIDS = ["5967", "5971", "5975", "5979", "5983", "5986"]
DISCHARGE_SOC_CIDS = ["5965", "5969", "5973", "5977", "5981", "5984"]
STORAGE_MODE_CID = "636"

# Per-slot on/off switches ("1"/"0"). Writes must include "yuanzhi": a bitmask
# of all 12 switch states before the change, bit = index in SLOT_SWITCH_CIDS
# (charge slots 1-6 -> bits 0-5, discharge slots 1-6 -> bits 6-11).
CHARGE_SWITCH_CIDS = ["5916", "5917", "5918", "5919", "5920", "5921"]
DISCHARGE_SWITCH_CIDS = ["5922", "5923", "5924", "5925", "5926", "5927"]
SLOT_SWITCH_CIDS = CHARGE_SWITCH_CIDS + DISCHARGE_SWITCH_CIDS
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
SYNC_INVERTER_TIME = _cfg_bool("sync_inverter_time", True)
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

_missing_credentials = [k for k in ("key_id", "secret", "username", "password", "plantId") if not CFG.get(k)]
INVERTER_CONTROL = not _missing_credentials
if _missing_credentials:
    log.error("SolisCloud credentials missing (%s); the inverter won't be controlled",
              ", ".join(_missing_credentials))
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
_cache = {"inverter_id": None, "inverter_sn": None, "last_time_sync": None,
          "load_profile": None, "load_profile_at": None}
_flags = {"busy": False, "write_lock": False, "started": False, "last_reason": None,
          "last_write_ok": None, "pd_warned_at": None, "hh_warned_at": None, "axle_warned_at": None,
          "logins": 0, "hh_seeded": False, "others_cleared": False}
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


# -----------------------------
# SolisCloud client (shared by every feature)
# -----------------------------
def digest(body: str) -> str:
    return base64.b64encode(hashlib.md5(body.encode("utf-8")).digest()).decode("utf-8")


def passwordEncode(password: str) -> str:
    return hashlib.md5(password.encode("utf-8")).hexdigest()


def prepare_header(config, body: str, canonicalized_resource: str):
    content_md5 = digest(body)
    content_type = "application/json"
    now = datetime.now(timezone.utc)
    date = now.strftime("%a, %d %b %Y %H:%M:%S GMT")
    encrypt_str = (
        VERB + "\n" + content_md5 + "\n" + content_type + "\n" + date + "\n" + canonicalized_resource
    )
    hmac_obj = hmac.new(
        config["secret"].encode("utf-8"),
        msg=encrypt_str.encode("utf-8"),
        digestmod=hashlib.sha1,
    )
    sign = base64.b64encode(hmac_obj.digest())
    authorization = "API " + str(config["key_id"]) + ":" + sign.decode("utf-8")
    return {
        "Content-MD5": content_md5,
        "Content-Type": content_type,
        "Date": date,
        "Authorization": authorization,
    }


def _clean_json_text(text: str) -> str:
    # SolisCloud sometimes returns JSON with trailing commas.
    return re.sub(r'("(?:\\?.)*?")|,\s*([}\]])', r"\1\2", text)


async def solis_post(session, url_path, body_dict, token=None):
    body = json.dumps(body_dict, separators=(",", ":"))
    headers = prepare_header(CFG, body, url_path)
    if token:
        headers["token"] = token
    return await session.post(BASE_URL + url_path, data=body, headers=headers)


async def resp_json(resp):
    text = await resp.text()
    return json.loads(_clean_json_text(text))


async def solis_login(session):
    body = {"userInfo": str(CFG["username"]), "password": passwordEncode(str(CFG["password"]))}
    resp = await solis_post(session, LOGIN_URL, body)
    if resp.status != HTTPStatus.OK:
        log.error("Login failed with status %s", resp.status)
        return None
    data = await resp_json(resp)
    token = data.get("csrfToken")
    if not token:
        log.error("Login succeeded but csrfToken missing: %s", data)
    return token


async def get_inverter(session):
    """Return (inverter_id, inverter_sn), cached after the first lookup."""
    if _cache["inverter_id"] and _cache["inverter_sn"]:
        return _cache["inverter_id"], _cache["inverter_sn"]

    resp = await solis_post(session, INVERTER_LIST_URL, {"stationId": str(CFG["plantId"])})
    if resp.status != HTTPStatus.OK:
        log.error("inverterList failed status %s", resp.status)
        return None, None
    data = await resp_json(resp)
    records = (data.get("data") or {}).get("page", {}).get("records", []) or []
    if not records:
        log.error("No inverters returned from inverterList")
        return None, None

    cfg_sn = str(CFG.get("inverter_sn", "")).strip()
    cfg_id = str(CFG.get("inverter_id", "")).strip()
    if cfg_sn.lower() in ("unknown", "unavailable", "none"):
        cfg_sn = ""
    if cfg_id.lower() in ("unknown", "unavailable", "none"):
        cfg_id = ""

    chosen = None
    if cfg_sn or cfg_id:
        for r in records:
            if (cfg_sn and str(r.get("sn")) == cfg_sn) or (cfg_id and str(r.get("id")) == cfg_id):
                chosen = r
                break
    else:
        storage_records = []
        for r in records:
            if str(r.get("productModel")) == "2":
                storage_records.append(r)
        if len(storage_records) == 1:
            chosen = storage_records[0]
        elif len(storage_records) == 0 and len(records) == 1:
            chosen = records[0]

    if not chosen:
        log.error("Could not pick an inverter; set inverter_sn or inverter_id. Available:")
        for r in records:
            log.error("  - ID: %s, SN: %s, Name: %s, ProductModel: %s",
                      r.get("id"), r.get("sn"), r.get("name"), r.get("productModel"))
        return None, None

    _cache["inverter_id"] = chosen.get("id")
    _cache["inverter_sn"] = chosen.get("sn")
    log.info("Using inverter ID=%s SN=%s Name=%s",
             _cache["inverter_id"], _cache["inverter_sn"], chosen.get("name"))
    return _cache["inverter_id"], _cache["inverter_sn"]


async def get_control_value(session, token, inverter_sn, cid):
    """Read one CID. Returns its value as a string, or None."""
    for attempt in range(1, CONTROL_RETRIES + 1):
        r = await solis_post(session, AT_READ_URL,
                             {"inverterSn": str(inverter_sn), "cid": str(cid)}, token=token)
        if r.status != HTTPStatus.OK:
            await asyncio.sleep(0.2)
            continue
        try:
            data = await resp_json(r)
            payload = data.get("data")
            if isinstance(payload, list) and payload:
                payload = payload[0]
            if str(data.get("code")) == "0" and isinstance(payload, dict) and "msg" in payload:
                return str(payload["msg"])
        except Exception as e:
            log.warning("AT_READ cid=%s attempt %s parse error: %s", cid, attempt, e)
        await asyncio.sleep(0.2)
    return None


async def write_control(session, token, inverter_sn, cid, value, old_value=None):
    body = {"inverterSn": str(inverter_sn), "cid": str(cid), "value": str(value)}
    if old_value is not None:
        body["yuanzhi"] = str(old_value)
    last_text = None
    for attempt in range(1, CONTROL_RETRIES + 1):
        r = await solis_post(session, CONTROL_URL, body, token=token)
        try:
            last_text = await r.text()
        except Exception:
            last_text = None

        if r.status != HTTPStatus.OK:
            log.warning("CONTROL cid=%s attempt %s/%s http=%s", cid, attempt, CONTROL_RETRIES, r.status)
            await asyncio.sleep(0.3)
            continue
        try:
            data = json.loads(_clean_json_text(last_text or ""))
            payload = data.get("data") or []
            if str(data.get("code")) == "0" and payload and str(payload[0].get("code")) == "0":
                if VERIFY_READBACK:
                    await asyncio.sleep(CONTROL_DELAY)
                    rb = await get_control_value(session, token, inverter_sn, cid)
                    if rb is not None and rb != str(value):
                        log.warning("Readback cid=%s: wrote '%s' but read '%s'", cid, value, rb)
                    else:
                        log.debug("Readback cid=%s: %s", cid, rb)
                return True
        except Exception as e:
            log.warning("CONTROL cid=%s attempt %s/%s parse error: %s", cid, attempt, CONTROL_RETRIES, e)
        await asyncio.sleep(0.3)

    log.error("CONTROL cid=%s failed after %s attempts. Last response: %s", cid, CONTROL_RETRIES, last_text)
    return False


async def read_slot_switches(session, token, inverter_sn):
    """Return {cid: "0"/"1"} for the 12 slot switches; unreadable ones are left out."""
    states = {}
    try:
        r = await solis_post(session, AT_READ_BATCH_URL,
                             {"inverterSn": str(inverter_sn), "cids": ",".join(SLOT_SWITCH_CIDS)},
                             token=token)
        if r.status == HTTPStatus.OK:
            data = await resp_json(r)
            if str(data.get("code")) == "0":
                for outer in data.get("data") or []:
                    items = outer if isinstance(outer, list) else [outer]
                    for item in items:
                        if isinstance(item, dict) and "cid" in item and "msg" in item:
                            states[str(item["cid"])] = str(item["msg"])
        else:
            log.warning("atReadBatch for slot switches failed http=%s", r.status)
    except Exception as e:
        log.warning("atReadBatch for slot switches failed: %s", e)

    for cid in SLOT_SWITCH_CIDS:
        if cid not in states:
            value = await get_control_value(session, token, inverter_sn, cid)
            if value is not None:
                states[cid] = value
    log.debug("Slot switch states: %s", states)
    return states


def slot_switch_mask(states):
    """The "yuanzhi" bitmask for the current switch states, or None if any is unknown."""
    mask = 0
    for bit, cid in enumerate(SLOT_SWITCH_CIDS):
        if cid not in states:
            return None
        if states[cid] == "1":
            mask |= 1 << bit
    return str(mask)


async def sync_inverter_time(session, token, inverter_id):
    value = datetime.now(INV_TZ).strftime("%Y-%m-%d %H:%M:%S")
    body = f'{{"inverterId":"{inverter_id}","cid":"56","value":"{value}"}}'
    headers = prepare_header(CFG, body, CONTROL_URL)
    headers["token"] = token
    try:
        resp = await session.post(BASE_URL + CONTROL_URL, data=body, headers=headers)
        if resp.status == HTTPStatus.OK:
            data = await resp_json(resp)
            if str(data.get("code")) == "0":
                log.info("Synced inverter time to %s (%s)", value, INVERTER_TIMEZONE)
                _cache["last_time_sync"] = datetime.now(timezone.utc)
                return
            log.warning("Time sync returned code=%s msg=%s", data.get("code"), data.get("msg"))
        else:
            log.warning("Time sync HTTP status: %s", resp.status)
    except Exception as e:
        log.error("Time sync failed: %s", e)


def switch_name(cid):
    """'charge slot 2' / 'discharge slot 1' for a switch CID."""
    if cid in CHARGE_SWITCH_CIDS:
        return f"charge slot {CHARGE_SWITCH_CIDS.index(cid) + 1}"
    if cid in DISCHARGE_SWITCH_CIDS:
        return f"discharge slot {DISCHARGE_SWITCH_CIDS.index(cid) + 1}"
    return f"CID {cid}"


async def set_slot_switch(session, token, inverter_sn, states, cid, enabled):
    """Switch a slot on/off if it isn't already. Updates `states` on success."""
    want = "1" if enabled else "0"
    if states.get(cid) == want:
        return True
    old_value = slot_switch_mask(states)
    if old_value is None:
        log.warning("Slot switch states unknown; writing CID %s without yuanzhi", cid)
    log.info("%s %s (CID %s)", "Enabling" if enabled else "Disabling", switch_name(cid), cid)
    ok = await write_control(session, token, inverter_sn, cid, want, old_value=old_value)
    if ok:
        states[cid] = want
    return ok


async def solis_connect():
    """Log in and pick the inverter. Returns (session, token, inverter_id, inverter_sn) or None."""
    session = async_get_clientsession(hass)
    _flags["logins"] += 1
    token = await solis_login(session)
    if not token:
        return None
    inverter_id, inverter_sn = await get_inverter(session)
    if not inverter_sn:
        return None
    return session, token, inverter_id, inverter_sn


async def solis_apply(ops):
    """
    Apply slot changes in order, in one SolisCloud session and under the write
    lock, so the switch bitmask is read and written as a pair. `ops` is a list
    of ("value", cid, value) and ("switch", switch_cid, enabled). Stops at the
    first failure (a slot is never switched on after a failed write). Returns
    True if everything succeeded.
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
        conn = await solis_connect()
        if conn is None:
            return False
        session, token, inverter_id, inverter_sn = conn

        if SYNC_INVERTER_TIME:
            last = _cache["last_time_sync"]
            if last is None or datetime.now(timezone.utc) - last > timedelta(hours=24):
                await sync_inverter_time(session, token, inverter_id)

        switches = None
        for op in ops:
            if op[0] == "switch" and switches is None:
                switches = await read_slot_switches(session, token, inverter_sn)

        for op in ops:
            if op[0] == "switch":
                if not await set_slot_switch(session, token, inverter_sn, switches, op[1], op[2]):
                    return False
            else:
                log.info("Writing CID %s = %s", op[1], op[2])
                if not await write_control(session, token, inverter_sn, op[1], op[2]):
                    return False
            await asyncio.sleep(INTER_WRITE_DELAY)
        return True
    finally:
        _flags["write_lock"] = False


async def log_storage_mode():
    """Read the storage mode once so the log shows whether TOU is likely enabled."""
    if DIAGNOSTICS_ONLY:
        return
    try:
        conn = await solis_connect()
        if conn is None:
            return
        session, token, _, inverter_sn = conn
        value = await get_control_value(session, token, inverter_sn, STORAGE_MODE_CID)
        log.info("Inverter storage mode (CID %s) = %s. Time-of-use / timed charge-discharge "
                 "must be enabled for the slots to work.", STORAGE_MODE_CID, value)
    except Exception as e:
        log.warning("Could not read storage mode: %s", e)


# -----------------------------
# Time and HA state helpers
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
    end = event_end + timedelta(minutes=AXLE_GRACE_MINUTES)
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


def predict(session, now, soc, voltage, load_profile, solar, later=None):
    """
    Plan the export for the window `session`. All energies in kWh, powers in kW.
    `soc` is the battery SOC now (%), `load_profile` {local hour: kW},
    `solar` {UTC half-hour: kW}, `later` the windows after it and before the
    cheap window (battery is held back for Power Down ones).
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
    if in_core_window(end):
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


async def plan_for(session, now, later=None):
    soc = _float_state(SOC_SENSOR)
    if soc is None:
        log.warning("SOC sensor '%s' unavailable; can't plan", SOC_SENSOR)
        return None
    voltage = _float_state(BATTERY_VOLTAGE_SENSOR)
    profile = await ensure_load_profile()
    return predict(session, now, soc, voltage, profile, solar_forecast(), later)


def worth_exporting(plan):
    """
    Only force a discharge when it actually exports at a useful rate. A slot
    whose current is below the house load makes the grid cover the gap, which
    is worse than leaving the inverter in self-use.
    """
    return plan["discharge_current"] > 0 and plan["predicted_export_kw"] >= MIN_EXPORT_KW


def export_desired(session, plan, now):
    """What the discharge slot should hold right now."""
    if session is None or plan is None or is_axle(session):
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


def slot_cids(key):
    """(time, current, soc, switch) CIDs for one of our slots."""
    kind, num = SLOT_DEFS[key]
    i = num - 1
    if kind == "charge":
        return CHARGE_TIME_CIDS[i], CHARGE_CURRENT_CIDS[i], CHARGE_SOC_CIDS[i], CHARGE_SWITCH_CIDS[i]
    return DISCHARGE_TIME_CIDS[i], DISCHARGE_CURRENT_CIDS[i], DISCHARGE_SOC_CIDS[i], DISCHARGE_SWITCH_CIDS[i]


def slot_ops(key, want, current_step=1):
    """
    The writes that take slot `key` from what we last wrote to `want`, in a
    safe order: switch off before clearing, switch on only after the time,
    current and SOC are in place. current/soc of None are left alone.
    """
    have = _slots[key]
    unknown = have["unknown"]
    time_cid, current_cid, soc_cid, switch_cid = slot_cids(key)
    ops = []

    if not want["enabled"]:
        if unknown or have["enabled"]:
            ops.append(("switch", switch_cid, False))
        if unknown or have["window"] != EMPTY_SLOT:
            ops.append(("value", time_cid, EMPTY_SLOT))
        return ops

    fresh = unknown or not have["enabled"]
    moved = fresh or have["window"] != want["window"]
    if moved:
        ops.append(("value", time_cid, want["window"]))
    if want["current"] is not None and (
            moved or have["current"] is None
            or abs(want["current"] - have["current"]) >= max(1, current_step)):
        ops.append(("value", current_cid, str(want["current"])))
    if want["soc"] is not None and (moved or want["soc"] != have["soc"]):
        ops.append(("value", soc_cid, str(want["soc"])))
    if fresh:
        ops.append(("switch", switch_cid, True))
    return ops


def apply_slot_ops(key, ops, ends=None):
    """Record successful writes for slot `key`."""
    program = _slots[key]
    time_cid, current_cid, soc_cid, switch_cid = slot_cids(key)
    for op in ops:
        if op[0] == "switch":
            program["enabled"] = op[2]
        elif op[1] == time_cid:
            program["window"] = op[2]
        elif op[1] == current_cid:
            program["current"] = int(op[2])
        elif op[1] == soc_cid:
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


def arbitrate(now, ev_on, hh_window, hh_want, export_window, export_want):
    """
    Apply the priority order to the features' wishes. Returns (hh_want,
    export_want, notes). Axle is handled by the caller (no writes at all).
    """
    notes = []
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
    return hh_want, export_want, notes


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
        ops.append(("switch", CHARGE_SWITCH_CIDS[num - 1], False))
        ops.append(("value", CHARGE_TIME_CIDS[num - 1], EMPTY_SLOT))
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
    if export_window is not None and not is_axle(export_window):
        plan = await plan_for(export_window, now, later)
    export_want = export_desired(export_window, plan, now)

    axle_running = is_axle(export_window) and export_window["start"] <= now
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
        hh_want, export_want, notes = arbitrate(now, ev_on, hh_window, hh_want, export_window, export_want)
        wants["happy_hour"] = hh_want
        wants["export"] = export_want
        for key in active_slot_keys():
            step = CURRENT_STEP_AMPS if key == "export" else 1
            per_slot[key] = slot_ops(key, wants[key], step)
        if not wants["export"]["enabled"] and export_window is not None and not is_axle(export_window) \
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
    is_power_down = export_window["kind"] == "power_down"
    if not is_power_down and not NOTIFY_PEAK:
        return
    _pd_joined["notified"].add(export_window["id"])
    await notify(
        "Power Down export planned" if is_power_down else "Peak export planned",
        f"{plan['window']}: exporting ~{plan['predicted_export_kwh']} kWh at "
        f"{plan['discharge_current']} A, stopping at {plan['stop_soc']}% "
        f"({plan['reserve_kwh']} kWh kept for the evening).",
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
    if is_axle(export_window) and export_window["start"] <= now:
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
                     "peak_export": PEAK_EXPORT, "axle": AXLE_EVENTS},
        "slot_problems": SLOT_PROBLEMS or None,
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
    elif is_axle(session):
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
EV_TRIGGER_ENTITIES = [EV_CHARGER_SWITCH, EV_CHARGER_CURRENT, EV_DISPATCH_SENSOR] \
    if EV_ENABLED and EV_DISPATCH_SENSOR else ["input_boolean.solis_octopus_ev_unconfigured"]


@time_trigger("startup")
async def solis_octopus_startup():
    log.info("=== Solis Octopus v%s starting (ev_follow=%s, happy_hour=%s, power_down=%s, "
             "peak_export=%s, axle=%s, tz %s, diagnostics_only=%s) ===", VERSION, EV_ENABLED,
             HH_ENABLED, PD_ENABLED, PEAK_EXPORT, AXLE_EVENTS, INVERTER_TIMEZONE, DIAGNOSTICS_ONLY)
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
    await log_storage_mode()
    await pd_auto_join()
    await hh_auto_join()
    _flags["started"] = True
    # Every slot starts unknown, so this also puts the inverter in a known state.
    await reconcile("startup")


@state_trigger(*EV_TRIGGER_ENTITIES)
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
    elif is_axle(export_window):
        log.info("Preview: next is an Axle %s event %s; the app stands aside",
                 export_window["kind"][len("axle_"):], fmt_window(export_window["start"], export_window["end"]))
    else:
        plan = await plan_for(export_window, now, later)
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
    if EV_ENABLED:
        charging, info = car_charging()
        log.info("Preview EV-Follow: charging=%s %s, programmed %s", charging, info, _slots["ev"]["window"])
