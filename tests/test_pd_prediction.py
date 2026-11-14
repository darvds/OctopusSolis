import unittest
from datetime import timezone

from tests.harness import App, local, session


def plan(app, sess, now, soc=80, voltage=None, profile=None, solar=None):
    return app.predict(sess, now, soc, voltage, profile or {}, solar or {})


class PredictionTests(unittest.TestCase):
    """10 kWh battery, 10% floor, 0.5 kW house, 50 V, 23:30 cheap window (see harness)."""

    def test_keeps_evening_reserve_and_exports_the_rest(self):
        app = App()
        p = plan(app, session("17:00", "18:00"), local("12:00"))
        # 8 kWh now - 5 h x 0.5 kW before the session
        self.assertEqual(p["battery_at_start_kwh"], 5.5)
        # 1 kWh floor + 18:00-23:30 at 0.5 kW
        self.assertEqual(p["evening_load_kwh"], 2.75)
        self.assertEqual(p["reserve_kwh"], 3.75)
        # Self-use until 17:40 (the battery covers the house), then full rate:
        # 1.75 kWh spare - 0.33 kWh of house = 1.42 kWh in 20 minutes.
        self.assertEqual(p["export_window"], "17:40-18:00")
        self.assertEqual(p["export_start"], local("17:40").isoformat())
        self.assertEqual(p["discharge_kw"], 4.25)
        self.assertEqual(p["discharge_current"], 85)
        self.assertEqual(p["battery_used_kwh"], 1.75)
        self.assertEqual(p["stop_soc"], 38)
        self.assertEqual(p["predicted_house_load_kwh"], 0.5)
        self.assertEqual(p["predicted_export_kwh"], 1.25)
        self.assertEqual(p["predicted_import_kwh"], 0)
        self.assertEqual(p["estimated_octopoints"], 400)
        self.assertEqual(p["window"], "17:00-18:00")

    def test_reserve_margin_scales_evening_load(self):
        app = App({"reserve_margin": 1.2})
        p = plan(app, session("17:00", "18:00"), local("12:00"))
        self.assertEqual(p["reserve_kwh"], 4.3)

    def test_capped_by_export_limit(self):
        app = App({"export_limit_kw": 1.0})
        p = plan(app, session("17:00", "18:00"), local("12:00"))
        # Full rate (1 kW out + 0.5 kW house) for the whole hour still fits the spare.
        self.assertEqual(p["export_window"], "17:00-18:00")
        self.assertEqual(p["discharge_kw"], 1.5)
        self.assertEqual(p["discharge_current"], 30)
        self.assertEqual(p["predicted_export_kwh"], 1.0)

    def test_capped_by_max_current(self):
        app = App({"max_discharge_amps": 20})
        p = plan(app, session("17:00", "18:00"), local("12:00"))
        self.assertEqual(p["discharge_current"], 20)
        self.assertEqual(p["discharge_kw"], 1.0)
        self.assertEqual(p["predicted_export_kwh"], 0.5)

    def test_capped_by_max_power(self):
        app = App({"max_discharge_kw": 1.2})
        p = plan(app, session("17:00", "18:00"), local("12:00"), soc=100)
        self.assertEqual(p["discharge_current"], 24)

    def test_voltage_sensor_used_for_current(self):
        app = App()
        p = plan(app, session("17:00", "18:00"), local("12:00"), voltage=35.0)
        # 100 A at 35 V is 3.5 kW, so full rate starts earlier.
        self.assertEqual(p["discharge_current"], 100)
        self.assertEqual(p["discharge_kw"], 3.5)
        self.assertEqual(p["export_window"], "17:35-18:00")

    def test_nothing_to_spare(self):
        app = App()
        p = plan(app, session("17:00", "18:00"), local("12:00"), soc=40)
        self.assertEqual(p["discharge_current"], 0)
        self.assertEqual(p["predicted_export_kwh"], 0)
        self.assertEqual(p["predicted_import_kwh"], 0.5)
        self.assertEqual(p["estimated_octopoints"], 0)

    def test_overnight_window_before_session_assumes_full_battery(self):
        app = App()
        sess = session("17:00", "18:00", day=6)
        p = plan(app, sess, local("22:00", day=5), soc=30)
        # Full at 05:30, then 11.5 h x 0.5 kW
        self.assertEqual(p["battery_at_start_kwh"], 4.25)
        # 0.5 kWh spare is just the house's 0.5 kWh: self-use, no forced slot.
        self.assertEqual(p["discharge_current"], 0)
        self.assertIsNone(p["export_window"])

    def test_load_profile_from_history(self):
        app = App()
        p = plan(app, session("17:00", "18:00"), local("12:00"), profile={17: 2.0})
        self.assertEqual(p["predicted_house_load_kwh"], 2.0)
        # Battery gives 1.75 kW, house needs 2 kW: a little import, nothing exported.
        self.assertEqual(p["predicted_export_kwh"], 0)
        self.assertEqual(p["predicted_import_kwh"], 0.25)
        self.assertEqual(p["estimated_octopoints"], 1400)

    def test_solar_forecast_adds_to_export(self):
        app = App()
        solar = {local("17:00").astimezone(timezone.utc): 1.0}
        p = plan(app, session("17:00", "18:00"), local("12:00"), solar=solar)
        self.assertEqual(p["predicted_solar_kwh"], 0.5)
        self.assertEqual(p["predicted_export_kwh"], 1.75)

    def test_session_ending_in_cheap_window_keeps_only_floor(self):
        app = App()
        p = plan(app, session("23:00", "23:45"), local("22:00"), soc=50)
        self.assertEqual(p["reserve_kwh"], 1.0)
        self.assertEqual(p["stop_soc"], 10)

    def test_replan_mid_session_uses_remaining_time(self):
        app = App()
        p = plan(app, session("17:00", "18:00"), local("17:30"), soc=50)
        self.assertEqual(p["hours"], 0.5)
        self.assertEqual(p["battery_at_start_kwh"], 5.0)
        # 1.25 kWh spare: 0.125 kWh of house to 17:45, then 1.125 kWh in 15 minutes.
        self.assertEqual(p["export_window"], "17:45-18:00")
        self.assertEqual(p["discharge_kw"], 4.5)
        self.assertEqual(p["discharge_current"], 90)

    def test_spare_only_covers_the_house_forces_nothing(self):
        # The old plan forced 10 A here: exactly the house load, so any spike
        # above it came from the grid. Self-use covers the house anyway.
        app = App()
        p = plan(app, session("17:00", "18:00"), local("17:30"), soc=40)
        self.assertEqual(p["spare_kwh"], 0.25)
        self.assertEqual(p["discharge_current"], 0)
        self.assertEqual(p["predicted_import_kwh"], 0)

    def test_full_rate_whatever_the_house_load_profile(self):
        # Your evening: the hourly mean hides the oven and the kettle. The
        # slot runs at full rate, so the grid only covers loads above ~4.4 kW.
        app = App({"export_limit_kw": 3.68, "discharge_efficiency": 0.95, "reserve_margin": 1.2})
        profile = {h: 0.5 for h in range(24)}
        profile.update({16: 0.8, 17: 1.2, 18: 0.9})
        p = plan(app, session("16:00", "19:00"), local("15:50"), soc=75, profile=profile)
        self.assertEqual(p["export_window"], "18:45-19:00")
        self.assertEqual(p["discharge_current"], 84)
        self.assertGreater(p["discharge_kw"], 4.0)
        self.assertEqual(p["battery_used_kwh"], p["spare_kwh"])

    def test_programmed_start_kept_when_plan_moves_a_little_later(self):
        app = App()
        sess = session("16:00", "19:00")
        # On its own the plan would start at 18:05 at 99 A. The slot keeps
        # 17:55 at 85 A (over 80% of full rate), still ending at the reserve.
        self.assertEqual(plan(app, sess, local("16:00"), soc=88)["export_window"], "18:05-19:00")
        p = app.predict(sess, local("16:00"), 88, None, {}, {}, programmed_start=local("17:55"))
        self.assertEqual(p["export_window"], "17:55-19:00")
        self.assertEqual(p["discharge_current"], 85)
        self.assertEqual(p["battery_used_kwh"], p["spare_kwh"])

    def test_programmed_start_moved_later_rather_than_run_slow(self):
        app = App()
        sess = session("17:00", "18:00")
        # Keeping 17:35 would mean 54 A: too slow, so the start moves to 17:45.
        p = app.predict(sess, local("17:20"), 50, None, {}, {}, programmed_start=local("17:35"))
        self.assertEqual(p["export_window"], "17:45-18:00")
        self.assertEqual(p["discharge_current"], 84)

    def test_programmed_start_moves_earlier(self):
        app = App()
        sess = session("17:00", "18:00")
        p = app.predict(sess, local("17:20"), 60, None, {}, {}, programmed_start=local("17:45"))
        self.assertEqual(p["export_window"], "17:30-18:00")

    def test_programmed_start_kept_once_running(self):
        app = App()
        sess = session("17:00", "18:00")
        p = app.predict(sess, local("17:50"), 45, None, {}, {}, programmed_start=local("17:40"))
        # 0.75 kWh spare over the last 10 minutes, capped at full rate.
        self.assertEqual(p["export_window"], "17:40-18:00")
        self.assertEqual(p["discharge_current"], 90)

    def test_dst_change_overnight(self):
        # Clocks go back on 2026-10-25: 23:30 -> 05:30 is 7 real hours.
        app = App()
        p = plan(app, session("17:00", "18:00", day=25), local("22:00", day=24), soc=30)
        # Full at 05:30 on the 25th, then 11.5 h of 0.5 kW
        self.assertEqual(p["battery_at_start_kwh"], 4.25)


class LoadProfileTests(unittest.TestCase):
    def test_hourly_means_averaged_per_local_hour(self):
        app = App()
        rows = [
            {"start": local("17:00", day=1).timestamp(), "mean": 1000},
            {"start": local("17:00", day=2).timestamp(), "mean": 3000},
            {"start": local("18:00", day=2), "mean": 500},
            {"start": local("19:00", day=2), "mean": None},
        ]
        self.assertEqual(app.build_load_profile(rows, 0.001), {17: 2.0, 18: 0.5})

    def test_missing_history_falls_back(self):
        app = App()

        async def broken():
            raise RuntimeError("no recorder")

        app.g["fetch_load_profile"] = broken
        self.assertEqual(app.run(app.ensure_load_profile()), {})


if __name__ == "__main__":
    unittest.main()
