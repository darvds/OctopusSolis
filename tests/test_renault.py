"""Renault: the car's charge schedule moves into happy hours, then back to normal."""
import unittest

from tests.hh_harness import App, booked, local

DEVICE = "abc123renaultdevice"
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def renault_app(extra=None, start="14:00", end="15:00", day=31, **kw):
    cfg = {"renault": dict({"vehicle": DEVICE, "name": "Renault 5"}, **(extra or {}))}
    cfg.update(kw)
    app = App(cfg)
    app.capture_ops()
    app.set_events(booked=[booked(start, end, day=day)] if start else [])
    app.seed(local("09:00", day))
    return app


def schedule_calls(app):
    return [c for c in app.services.calls if c["domain"] == "renault"]


def windows(app):
    """(startTime, duration) of each schedule write, checking every day matches."""
    out = []
    for c in schedule_calls(app):
        assert c["service"] == "charge_set_schedules"
        assert c["data"]["vehicle"] == DEVICE
        (sched,) = c["data"]["schedules"]
        assert sched["id"] == 1 and sched["activated"] is True
        days = {(sched[d]["startTime"], sched[d]["duration"]) for d in DAYS}
        assert len(days) == 1, days
        out.append(days.pop())
    return out


class RenaultScheduleTests(unittest.TestCase):
    def test_happy_hour_then_back_to_normal(self):
        app = renault_app()  # 31 Oct: GMT, so UTC == local
        steps = [
            ("12:00", [("T23:30Z", 360)]),     # startup: unknown, so normal is written
            ("13:49", []),
            ("13:50", [("T14:00Z", 60)]),      # prepare_minutes (10) before
            ("14:30", []),
            ("15:00", [("T23:30Z", 360)]),     # over: back to 23:30-05:30
            ("15:01", []),
        ]
        for hhmm, expected in steps:
            with self.subTest(time=hhmm):
                app.services.calls.clear()
                app.at(hhmm)
                app.reconcile()
                self.assertEqual(windows(app), expected)

    def test_notifications(self):
        app = renault_app()
        for hhmm in ("12:00", "13:50", "14:00", "15:00"):
            app.at(hhmm)
            app.reconcile()
        self.assertEqual(app.services.titles(),
                         ["Happy Hour started", "Happy Hour over", "Renault 5 schedule restored"])
        self.assertIn("Renault 5 set to charge", app.services.notifications()[0]["data"]["message"])
        self.assertEqual(app.services.notifications()[2]["data"]["message"], "Charging back to 23:30-05:30.")

    def test_summer_time_is_sent_in_utc(self):
        app = renault_app(day=6)  # 6 Oct: BST (UTC+1)
        app.at("12:00", day=6)
        app.reconcile()
        app.at("13:55", day=6)
        app.reconcile()
        self.assertEqual(windows(app), [("T22:30Z", 360), ("T13:00Z", 60)])

    def test_clock_change_rewrites_normal_schedule(self):
        app = renault_app(start=None, day=24)
        app.at("12:00", day=24)  # BST
        app.reconcile()
        app.at("12:00", day=26)  # GMT
        app.reconcile()
        app.reconcile()
        self.assertEqual(windows(app), [("T22:30Z", 360), ("T23:30Z", 360)])

    def test_late_start_and_odd_minutes_round_to_quarters(self):
        app = renault_app(start="14:10", end="15:20")
        app.at("14:30")
        app.reconcile()
        self.assertEqual(windows(app), [("T14:00Z", 90)])

    def test_cancelled_happy_hour_reverts(self):
        app = renault_app()
        app.at("14:00")
        app.reconcile()
        app.set_events(booked=[])
        app.at("14:20")
        app.reconcile()
        self.assertEqual(windows(app), [("T14:00Z", 60), ("T23:30Z", 360)])

    def test_back_to_back_happy_hours_are_one_schedule(self):
        app = renault_app()
        app.set_events(booked=[booked("14:00", "15:00"), booked("15:00", "16:00", eid="2")])
        for hhmm in ("14:00", "15:00", "16:00"):
            app.at(hhmm)
            app.reconcile()
        self.assertEqual(windows(app), [("T14:00Z", 120), ("T23:30Z", 360)])

    def test_failure_retries_and_notifies_once(self):
        app = renault_app()
        app.services.fail.add(("renault", "charge_set_schedules"))
        app.at("14:00")
        with self.assertLogs("pyscript.solis_octopus", level="ERROR"):
            for minute in range(0, 20):
                app.at(f"14:{minute:02d}")
                app.reconcile()
        self.assertEqual(len(schedule_calls(app)), 4)  # 14:00, 14:05, 14:10, 14:15
        self.assertEqual(app.services.titles().count("Renault 5 schedule not set"), 1)
        app.services.fail.clear()
        app.services.calls.clear()
        app.at("14:20")
        app.reconcile()
        self.assertEqual(windows(app), [("T14:00Z", 60)])

    def test_custom_normal_window_and_schedule_id(self):
        app = renault_app({"normal_window": "00:30-04:30", "schedule_id": 2}, start=None)
        app.reconcile()
        (call,) = schedule_calls(app)
        sched = call["data"]["schedules"][0]
        self.assertEqual(sched["id"], 2)
        self.assertEqual((sched["monday"]["startTime"], sched["monday"]["duration"]), ("T00:30Z", 240))

    def test_diagnostics_only_logs_instead(self):
        app = renault_app(diagnostics_only=True)
        app.at("14:00")
        with self.assertLogs("pyscript.solis_octopus", level="WARNING") as logs:
            app.reconcile()
        self.assertEqual(schedule_calls(app), [])
        self.assertTrue(any("renault.charge_set_schedules" in line for line in logs.output))

    def test_status_attribute(self):
        app = renault_app()
        app.at("14:00")
        app.reconcile()
        car = app.status()[2]["car_schedule"]
        self.assertEqual(car["mode"], "happy_hour")
        self.assertEqual(car["window"], "14:00-15:00")
        self.assertTrue(car["in_sync"])

    def test_needs_a_vehicle(self):
        with self.assertLogs("pyscript.solis_octopus", level="ERROR"):
            app = App({"renault": {"name": "Renault 5"}})
        self.assertFalse(app.RENAULT_ENABLED)

    def test_off_without_section(self):
        app = App()
        app.capture_ops()
        app.reconcile()
        self.assertFalse(app.RENAULT_ENABLED)
        self.assertEqual(schedule_calls(app), [])


if __name__ == "__main__":
    unittest.main()
