import unittest

from tests.hh_harness import App, available, booked, local


class SessionTests(unittest.TestCase):
    def test_parses_booked_and_available(self):
        app = App()
        app.set_events(booked=[booked("14:00", "15:00", eid="1")],
                       available=[available("14:00", "15:00", eid="1"), available("16:00", "17:00", eid="2")])
        got_booked, got_available = app.sessions()
        self.assertEqual([s["id"] for s in got_booked], ["1"])
        self.assertEqual(got_booked[0]["start"], local("14:00"))
        self.assertEqual([s["code"] for s in got_available], ["HH301"])

    def test_iso_strings(self):
        app = App()
        app.set_events(booked=[booked("14:00", "15:00", as_datetime=False)])
        self.assertEqual(app.happy_hours()[0]["start"], local("14:00"))

    def test_ignores_bad_events(self):
        app = App()
        app.set_events(booked=[{"id": "x", "start": None, "end": None},
                               {"id": "y", "start": "nope", "end": "nope"}, "junk",
                               booked("15:00", "14:00", eid="z")])
        self.assertEqual(app.sessions(), ([], []))

    def test_merges_back_to_back_and_overlapping(self):
        app = App()
        app.set_events(booked=[booked("15:00", "16:00", eid="2"), booked("14:00", "15:00", eid="1"),
                               booked("15:30", "16:30", eid="3"), booked("18:00", "19:00", eid="4")])
        windows = app.happy_hours()
        self.assertEqual([(app.fmt_window(w["start"], w["end"]), w["sessions"]) for w in windows],
                         [("14:00-16:30", 3), ("18:00-19:00", 1)])

    def test_target_is_current_or_next(self):
        app = App()
        app.set_events(booked=[booked("18:00", "19:00", eid="2"), booked("14:00", "15:00", eid="1")])
        windows = app.happy_hours()
        self.assertEqual(app.target_window(windows, local("12:00"))["start"], local("14:00"))
        self.assertEqual(app.target_window(windows, local("14:30"))["start"], local("14:00"))
        self.assertEqual(app.target_window(windows, local("15:00"))["start"], local("18:00"))
        self.assertIsNone(app.target_window(windows, local("19:00")))

    def test_missing_entity(self):
        app = App()
        self.assertEqual(app.happy_hours(), [])


class EventsEntityTests(unittest.TestCase):
    def test_missing_entity_warns_once_an_hour(self):
        app = App()
        app.capture_ops()
        with self.assertLogs("pyscript.solis_octopus", level="ERROR") as logs:
            app.reconcile()
            app.reconcile()
        self.assertEqual(len([l for l in logs.output if l.startswith("ERROR")]), 1)
        attrs = app.status()[2]
        self.assertEqual(attrs["power_up_events"], "missing")
        self.assertIn("disabled by default", attrs["warning"])

    def test_not_configured(self):
        app = App({"power_up_events_entity": None})
        app.capture_ops()
        app.reconcile()
        self.assertEqual(app.status()[2]["power_up_events"], "not_configured")

    def test_tokens_entity_derived_from_events_entity(self):
        app = App()
        self.assertEqual(app.TOKENS_ENTITY, "sensor.octopus_energy_a_test_octoplus_weekend_happy_hours")
        self.assertEqual(App({"weekend_happy_hours_entity": "sensor.x"}).TOKENS_ENTITY, "sensor.x")


if __name__ == "__main__":
    unittest.main()
