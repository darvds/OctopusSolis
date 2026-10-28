# Solis Octopus

One [pyscript](https://github.com/custom-components/pyscript) app for Home Assistant that runs a **Solis** home battery for everything **Octopus** (and **Axle Energy**) related. It replaces three separate apps, which each drove the inverter on their own:

- [Solis EV-Follow](https://github.com/darvds/SolisOctopusEVFollow): charge the battery while the car is really being smart-charged.
- [Octopus Happy Hour](https://github.com/darvds/OctopusHappyHour): charge during Happy Hours / Power Ups, boost the Cosy hot water, run your actions.
- [Solis Power Down](https://github.com/darvds/SolisOctopusPowerDown): export during Octopus Power Down sessions, a daily peak window and around Axle Energy events.

[Cosy WC Tuner](https://github.com/darvds/OctopusCosyWCTuner) never touches the battery, so it stays a separate app.

## Why one app

Separately, the three apps could get in each other's way:

- **Wiped slots.** EV-Follow cleared Happy Hour's charge slot every time it restarted.
- **Charging and exporting at once.** A happy hour or EV dispatch during the peak window left the inverter with a charge slot and a discharge slot at the same time.
- **Axle ignored.** Only Power Down stayed off the inverter during Axle events.
- **Lost switch changes.** Each app read the 12 slot on/off states and then wrote its own. Two apps doing that at once could undo each other's change.
- **Three logins.** Each app logged in to SolisCloud and synced the clock separately.

Now one app decides what every slot should hold, in one pass, with one priority order. It sends the changes to the inverter in one batch, through one login, with a lock around every write.

## Priority

| | Feature | What happens |
|---|---|---|
| 1 | **Axle Energy event** | Axle drives the battery. The app makes no inverter writes during the event and its grace period. The one exception: switching off one of its own slots that was still running when the event was announced. |
| 2 | **EV-Follow charging** | While the car is smart-charging, the battery charges too, and any export window it overlaps is paused for those half-hours. |
| 3 | **Octopus Power Down** | Exports everything above the evening reserve. It beats happy hour charging if they ever overlap. |
| 4 | **Happy Hour / Power Up** | Charges at full speed and pauses any overlapping peak export. It can charge alongside EV-Follow. |
| 5 | **Daily peak export** | Exports what's spare in the peak window (e.g. 16:00–19:00). |
| 6 | **Core overnight window** | Always programmed in its own charge slot. |

Before an Axle export event or a Power Down session, earlier export windows **hold battery back** for it. That covers as much as the battery could export at full power, and shows as `held_for_power_down_kwh`.

## The features

**EV-Follow (charge slot 2).** When the charger draws current during a *started* Octopus dispatch, for at least `debounce_seconds`, the slot is set to the current half-hour. A few minutes before each `:00`/`:30` it's extended if the car is still charging. When the car stops, the battery finishes the half-hour, which Octopus bills at the cheap rate, and then stops. The core window `23:30-05:30` lives in charge slot 1.

**Happy Hour / Power Up (charge slot 3).** Booked sessions, plus Weekend Happy Hours booked automatically if `auto_join` is on, are charged at `charge_current` up to `charge_soc`. When one starts, the Cosy hot water is boosted, your `start_actions` run and you get a notification. `end_actions` run when it ends.

**Export (discharge slot 1).** Power Down sessions (joined automatically) and the daily peak window. For each window the app predicts:
- house load, from your load sensor's history at that time of day
- solar, from an optional Solcast forecast
- the battery at the start
- the reserve the house needs until the cheap window

Everything above the reserve is exported, capped by your inverter, battery current and export limit. The slot is written `prepare_minutes` before the window. During the window it's re-planned every minute from the real SOC.

**Axle Energy.** The app reads the [Axle Energy integration](https://www.home-assistant.io/integrations/axle_energy/)'s event type, start and end sensors.

**Fail-safe.** Every slot has a fixed time window and stop SOC, so the inverter starts and stops by itself. If Home Assistant, pyscript or SolisCloud goes down, nothing runs past its window.

## Requirements

- Home Assistant with [pyscript](https://github.com/custom-components/pyscript) (HACS).
- A Solis hybrid inverter:
  - **6-slot firmware** (HMI ≥ `4B00`)
  - **time-of-use / timed charge-discharge enabled**
  - SolisCloud API access
- The [Octopus Energy integration](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy):
  - **EV-Follow:** the intelligent dispatching sensor set to "started dispatches only".
  - **Happy Hour:** the power up events entity **enabled**. It's disabled by default.
  - **Power Down:** Octoplus enrolment.
- For Axle: the Axle Energy integration.
- For the prediction: a house load sensor with long-term statistics, and a battery SOC sensor.

Each feature can be switched off. Leave its section out, or set `enabled: false`.

## Moving over from the three apps

1. Copy `pyscript/apps/solis_octopus.py` to `<config>/pyscript/apps/`.
2. **Delete** `solis_ev_follow.py`, `octopus_happy_hour.py` and `solis_power_down.py` from `<config>/pyscript/apps/`.
3. In `configuration.yaml`, replace their three blocks with one `solis_octopus:` block. [`configuration.example.yaml`](configuration.example.yaml) shows every option. The settings keep their old names inside each section. These ones moved:

   | Old | New |
   |---|---|
   | `ev_slot` / `charge_slot` / `discharge_slot` | `slots: ev / happy_hour / export` |
   | `battery_capacity_kwh` | `battery: capacity_kwh` |
   | `battery_voltage` / `battery_voltage_sensor` | `battery: voltage` / `voltage_sensor` |
   | `peak_export`, `peak_window`, `peak_export_rate`, `peak_export_switch`, `notify_peak` | `peak_export: enabled, window, rate, switch, notify` |
   | `axle_events`, `axle_event_*_sensor`, `axle_grace_minutes` | `axle: enabled, event_*_sensor, grace_minutes` |
   | `clear_other_slots` (EV-Follow) | `slots: clear_other_charge_slots` (off by default) |

4. Restart Home Assistant with `diagnostics_only: true`. The app then logs what it *would* write, boost and run, without changing anything. Check the log and `sensor.solis_octopus` for an evening, then set it to `false` and restart.

Your dashboards keep working. `sensor.solis_ev_charge_slot`, `sensor.octopus_happy_hour` (+ `binary_sensor.octopus_happy_hour_active`) and `sensor.solis_power_down` (+ `binary_sensor.solis_power_down_active`) keep their names.

## Configuration

**Top level**

| Option | Default | Description |
|---|---|---|
| `key_id`, `secret`, `username`, `password`, `plantId` | **required** | SolisCloud API credentials. Without them the inverter isn't controlled. |
| `inverter_sn` / `inverter_id` | auto | Only needed with more than one storage inverter |
| `inverter_timezone` | HA time zone | Time zone of the inverter clock and slots |
| `core_window` | `23:30-05:30` | Octopus cheap overnight window |
| `notify_service` | none | e.g. `notify.mobile_app_phone`, or a list |
| `notify_data` | none | Extra `data` for notifications |
| `diagnostics_only` | `false` | Log instead of writing, boosting or running actions |
| `sync_inverter_time` | `true` | Sync the inverter clock at most once a day |
| `startup_delay_seconds` | `60` | Wait for other integrations after startup |
| `control_retries`, `verify_readback` | `3`, `false` | SolisCloud write behaviour |

**`slots:`**

| Option | Default | Description |
|---|---|---|
| `core` | `1` | Charge slot for the core window |
| `ev` | `2` | Charge slot for EV-Follow |
| `happy_hour` | `3` | Charge slot for happy hours |
| `export` | `1` | Discharge slot for exports |
| `clear_other_charge_slots` | `false` | Clear unused charge slots once at startup. Leave it off if Axle or anything else uses them. |

**`battery:`**

| Option | Default | Description |
|---|---|---|
| `soc_sensor` | **required for exports** | Battery SOC (%) |
| `capacity_kwh` | `10` | Usable capacity |
| `floor_soc` | `15` | Lowest SOC an export discharges to |
| `core_charge_soc` | `100` | SOC reached by the end of the cheap window |
| `load_power_sensor` | none | House load (W or kW) for the history |
| `history_days` | `14` | Days of history to average |
| `default_load_kw` | `0.5` | Load for hours without history |
| `reserve_margin` | `1.2` | Multiplier on the evening load |
| `solar_forecast_sensors` | none | Solcast `detailedForecast` sensors |
| `voltage_sensor` / `voltage` | none / `51.2` | Battery voltage, used to convert kW to amps |
| `max_discharge_kw` | `5.0` | Inverter / battery limit |
| `max_discharge_amps` | `100` | Discharge current limit |
| `export_limit_kw` | `3.68` | Your export limit |
| `discharge_efficiency` | `0.95` | Battery-to-AC efficiency |
| `min_export_kw` | `0.2` | Only export when it exports at least this rate |
| `current_step_amps` | `5` | Minimum change before the current is rewritten |
| `prepare_minutes` | `15` | Write the discharge slot this long before a window |

**`ev_follow:`**

| Option | Default | Description |
|---|---|---|
| `dispatch_sensor` | **required** | `binary_sensor.octopus_energy_<id>_intelligent_dispatching` |
| `charger_switch` | `switch.hypervolt_charging` | Charger on/off |
| `charger_current` | `sensor.hypervolt_charger_current` | Charger current (A) |
| `min_current_amps` | `3` | Minimum current that counts as charging |
| `debounce_seconds` | `60` | How long charging must last before the battery starts |
| `extend_lead_minutes` | `3` | Extend the slot this early before `:00`/`:30` |
| `manage_core_slot` | `true` | Program the core window |

**`happy_hour:`**

| Option | Default | Description |
|---|---|---|
| `power_up_events_entity` | **required** | `event.octopus_energy_<account>_octoplus_power_up_events` |
| `weekend_happy_hours_entity` | derived | Token count sensor |
| `auto_join` | `false` | Book available Weekend Happy Hours while you have tokens |
| `join_selection` | `earliest` | `earliest` or `latest` |
| `battery_charge` | `true` | Charge the battery during happy hours |
| `charge_current`, `charge_soc` | `100`, `100` | Charge current (A) and target SOC |
| `prepare_minutes` | `5` | Write the charge slot this long before |
| `heat_pump_id` / `water_heater_entity` | none | The Cosy water heater |
| `hot_water_boost`, `hot_water_temperature` | `true` with a water heater, `50` | Boost during happy hours |
| `start_actions`, `end_actions` | none | HA actions to run |
| `notify_scheduled`, `notify_end` | `true`, `true` | Notify on booking and at the end |

**`power_down:`**

| Option | Default | Description |
|---|---|---|
| `power_down_events_entity` | **required** | `event.octopus_energy_<account>_octoplus_power_down_events` |
| `auto_join` | `true` | Join every available session |

**`peak_export:`**

| Option | Default | Description |
|---|---|---|
| `window` | `16:00-19:00` | Daily peak window |
| `rate` | `16` | Peak export rate (p/kWh), used for the value estimate |
| `switch` | none | `input_boolean` that turns peak exports off when it's off |
| `notify` | `false` | Notify when a peak export is armed |

**`axle:`**

| Option | Default | Description |
|---|---|---|
| `event_type_sensor` | `sensor.axle_energy_event_type` | Axle event type (`export` / `import`) |
| `event_start_sensor` | `sensor.axle_energy_event_start` | Axle event start |
| `event_end_sensor` | `sensor.axle_energy_event_end` | Axle event end |
| `grace_minutes` | `10` | Stay off the inverter this long after an event |

## Monitoring

- **`sensor.solis_octopus`**
  - **State:** `idle`, `charging`, `exporting` or `axle_event`.
  - **Attributes:**
    - `decision`: e.g. "export paused: EV charging"
    - `slots`: what each slot holds
    - which features are on
    - any slot config problems
- **`sensor.solis_power_down`** (+ `_active`): the export plan.
  - **States:** `planned`, `armed`, `exporting`, `paused`, `no_spare_energy`, `axle_upcoming`, `axle_event`.
  - **Attributes:** the prediction, `held_for_power_down_kwh`, `estimated_export_value_pence`, and `power_down_events` / `axle_events` health.
- **`sensor.octopus_happy_hour`** (+ `_active`): booked happy hours, tokens, the charge slot, the boost, and `power_up_events` health.
- **`sensor.solis_ev_charge_slot`**: the EV slot, charger amps and dispatch state.
- **Actions** (Developer Tools → Actions):
  - `pyscript.solis_octopus_reconcile`: join sessions and re-plan now.
  - `pyscript.solis_octopus_preview`: log what every feature would do next, without writing.
  - `pyscript.solis_octopus_clear`: switch off and clear every slot the app uses. They're rewritten at the next check.
- **Debug logging:**
  ```yaml
  logger:
    logs:
      pyscript.solis_octopus: debug
  ```

## Troubleshooting

- **A feature isn't doing anything.** Check the `features` attribute on `sensor.solis_octopus`. Then check the `*_events` health attributes on its own sensor. A typo in an entity ID shows as `missing`, with a `warning`.
- **"Not starting: fix the slots config first".** Two features are set to the same charge slot. The details are in `slot_problems` on `sensor.solis_octopus`.
- **Slots at the wrong times.** Check the inverter clock and `inverter_timezone`.
- **Axle's settings get overwritten.** Make sure Axle doesn't use the same Solis slots as this app, and change `slots:` if it does.
- **Before sharing logs**, remove inverter serial numbers and your Octopus account ID.

## Development

The tests use only the Python standard library (3.9+), plus PyYAML for the example-config test:

```bash
python3 -m unittest discover -s tests -t .
```

`tests/harness.py` loads the app with fakes for pyscript's globals, the clock, HA states and services, recorder history and SolisCloud. The test suites come from the three original apps (EV-Follow, Happy Hour, Power Down), run against the combined app. On top of those, `tests/test_arbiter.py` runs every feature at once and covers:
- each clash listed under [Why one app](#why-one-app)
- one login and one switch read per pass
- the write lock

CI runs the suite on every push.

## Credits

- [Moondevil-ha/solis-smart-charging](https://github.com/Moondevil-ha/solis-smart-charging) and [stevegal/solis_control](https://github.com/stevegal/solis_control) (MIT): SolisCloud API signing, login and control writes.
- [hultenvp/solis-sensor](https://github.com/hultenvp/solis-sensor) and [mkuthan/solis-cloud-control](https://github.com/mkuthan/solis-cloud-control): slot CIDs and the `yuanzhi` bitmask.
- [BottlecapDave/HomeAssistant-OctopusEnergy](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy): Octoplus, dispatch and Cosy entities and actions.
- The Axle Energy integration in Home Assistant core.

Not affiliated with Solis, Octopus Energy, Axle Energy or Home Assistant.

## License

[MIT](LICENSE)
