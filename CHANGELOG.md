# Changelog

The version is shown as `version` on `sensor.solis_power_down` and in the startup log line.

## 2.4.0 (2026-11-20)

- Power Down sessions always export at least `power_down: min_export_kwh` (0.5 kWh) to earn the session's token, using battery held for a later Axle event that the pre-charge refills.
- The app checks its slots' switch, current and stop SOC against the inverter every minute, and rewrites a slot that something else changed.
- The discharge stop SOC is never written below `battery: min_cut_off_soc` (7%, the lowest Solis accepts).
- Exports that finish at the floor stop at the floor, not 3% above it.
- Renault: going back to the normal charge window backs off retries (up to `max_retry_minutes`, 60) while the Renault servers fail.
- Fixed: every check crashed under pyscript (`not implemented ast ast_generatorexp`).

## 2.3.0

- Axle pre-charge: top the battery up just before an Axle export event.
