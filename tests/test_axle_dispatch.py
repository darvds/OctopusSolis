"""Axle Self-Dispatch (axle.control: true): the app exports during Axle export events itself."""
import unittest

from tests.harness import (App, CHARGE_SWITCH_CIDS, CHARGE_TIME_CIDS, CURRENT_CID, EV_CONFIG, HH_CONFIG,
                           SOC_CID, SWITCH_CID, TIME_CID, hh_event, local)

EV_TIME, EV_SWITCH = CHARGE_TIME_CIDS[1], CHARGE_SWITCH_CIDS[1]
HH_TIME, HH_SWITCH = CHARGE_TIME_CIDS[2], CHARGE_SWITCH_CIDS[2]
EV_OFF = [("switch", EV_SWITCH, False), ("value", EV_TIME, "00:00-00:00")]


def dispatch_app(axle=None, extra=None, *more, day=6):
    """Axle Self-Dispatch on; 10 kWh battery, 10% floor, 0.5 kW house, 50 V (see harness)."""
    cfg = {"control": True}
    cfg.update(axle or {})
    app = App({"axle_events": True, "axle": cfg}, extra, *more)
    app.capture_ops()
    app.known()
    app.g["_slots"]["core"].update({"window": "23:30-05:30", "enabled": True})
    app.set_events()
    app.set_axle()
    app.at("12:00", day=day)
    return app


def arm(window, current, soc):
    return [("value", TIME_CID, window), ("value", CURRENT_CID, str(current)),
            ("value", SOC_CID, str(soc)), ("switch", SWITCH_CID, True)]


class WindowTests(unittest.TestCase):
    def test_window_is_exactly_the_event(self):
        app = dispatch_app()
        app.set_axle("export", "17:30", "18:30")
        ev = app.axle_event(local("12:00"))
        self.assertEqual((ev["start"], ev["end"]), (local("17:30"), local("18:30")))
        self.assertTrue(app.axle_dispatch(ev))

    def test_stand_aside_off_with_control(self):
        app = dispatch_app()
        app.set_axle("export", "17:30", "18:30")
        ev = app.axle_event(local("17:45"))
        self.assertFalse(app.stand_aside(ev, local("17:45")))


class ExportTests(unittest.TestCase):
    def test_arms_before_and_exports_to_the_floor(self):
        app = dispatch_app()
        app.set_axle("export", "17:30", "18:30")
        app.set_soc(80)
        app.at("17:14")
        app.reconcile()
        self.assertEqual(app.ops, [])
        # 7.875 kWh at 17:30, keep only the 1 kWh floor: 5 kW cap for the hour.
        app.at("17:15")
        app.reconcile()
        self.assertEqual(app.ops, [arm("17:30-18:30", 100, 10)])
        _, status, attrs = app.status()
        self.assertEqual(status, "armed")
        self.assertEqual(attrs["window_kind"], "axle_export")
        self.assertTrue(attrs["axle_control"])
        self.assertEqual(attrs["reserve_kwh"], 1.0)
        self.assertEqual(attrs["evening_load_kwh"], 0)
        app.ops.clear()
        app.at("17:45")
        app.set_soc(60)
        app.reconcile()
        self.assertEqual(app.status()[1], "exporting")
        self.assertEqual(app.overview()[1], "exporting")
        # After the event: cleared straight away, no grace period.
        app.ops.clear()
        app.at("18:30")
        app.reconcile()
        self.assertEqual(app.ops, [[("switch", SWITCH_CID, False), ("value", TIME_CID, "00:00-00:00")]])

    def test_keep_evening_reserve_option(self):
        drain = dispatch_app()
        keep = dispatch_app({"keep_evening_reserve": True})
        for app in (drain, keep):
            app.set_axle("export", "17:30", "18:30")
            app.set_soc(80)
            app.at("17:15")
            app.reconcile()
        d, k = drain.status()[2], keep.status()[2]
        # Keeping 18:30-23:30 at 0.5 kW on top of the floor.
        self.assertEqual(k["reserve_kwh"], 3.5)
        self.assertEqual(k["stop_soc"], 35)
        self.assertEqual(k["discharge_current"], 88)
        self.assertGreater(d["predicted_export_kwh"], k["predicted_export_kwh"])

    def test_earlier_peak_holds_back(self):
        app = dispatch_app(extra={"peak_export": True})
        app.set_axle("export", "17:30", "18:30")
        app.set_soc(100)
        app.at("15:45")
        app.reconcile()
        attrs = app.status()[2]
        self.assertEqual(attrs["window"], "16:00-17:30")
        self.assertEqual(attrs["held_for_power_down_kwh"], 4.5)

    def test_notification(self):
        app = dispatch_app(extra={"notify_service": "notify.phone"})
        app.set_axle("export", "17:30", "18:30")
        app.at("17:15")
        app.reconcile()
        notes = app.services.notifications()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["data"]["title"], "Axle export planned")
        self.assertIn("kept in the battery", notes[0]["data"]["message"])


class PriorityTests(unittest.TestCase):
    def ev_app(self):
        app = dispatch_app(None, EV_CONFIG, {"ev_follow": {"debounce_seconds": 0}})
        app.set_axle("export", "17:30", "18:30")
        app.set_charger(dispatch="off")
        return app

    def test_ev_dispatch_during_axle_export_does_not_charge(self):
        app = self.ev_app()
        app.at("17:15")
        app.reconcile()
        app.ops.clear()
        app.set_charger()
        app.at("17:40")
        app.reconcile()
        self.assertEqual(app.ops, [])
        self.assertEqual(app.status()[1], "exporting")
        self.assertIn("EV charging paused: Axle export", app.overview()[2]["decision"])

    def test_running_ev_half_hour_finishes_but_is_not_extended(self):
        app = self.ev_app()
        app.set_charger()
        app.at("17:05")
        app.reconcile()
        self.assertEqual(app.slot("ev"), "17:00-17:30")
        app.at("17:15")
        app.reconcile()  # export armed, EV slot left alone
        self.assertEqual(app.slot("ev"), "17:00-17:30")
        self.assertEqual(app.slot("export"), "17:30-18:30")
        app.ops.clear()
        app.at("17:27")
        app.reconcile()  # would normally extend to 18:00
        self.assertEqual(app.ops, [])
        self.assertEqual(app.slot("ev"), "17:00-17:30")
        app.at("17:30")
        app.reconcile()
        self.assertEqual(app.ops, [EV_OFF])

    def test_ev_charges_again_after_the_event(self):
        app = self.ev_app()
        app.set_charger()
        app.at("18:31")
        app.reconcile()
        self.assertEqual(app.slot("ev"), "18:30-19:00")

    def test_happy_hour_charges_up_to_the_axle_export(self):
        app = dispatch_app(None, HH_CONFIG, day=10)
        app.set_happy_hours(booked=[hh_event("17:00", "18:00")])
        app.set_axle("export", "17:30", "18:30", day=10)
        app.at("16:55", day=10)
        app.reconcile()
        self.assertIn(("switch", HH_SWITCH, True), app.ops[-1])
        app.ops.clear()
        # Export arms at 17:15: the happy hour slot is trimmed to end at 17:30.
        app.at("17:15", day=10)
        app.reconcile()
        batch = app.ops[0]
        self.assertIn(("value", HH_TIME, "17:00-17:30"), batch)
        self.assertNotIn(("switch", HH_SWITCH, False), batch)
        self.assertIn(("switch", SWITCH_CID, True), batch)
        self.assertIn("happy hour charging paused: Axle export", app.overview()[2]["decision"])

    def test_happy_hour_inside_axle_export_does_not_charge(self):
        app = dispatch_app(None, HH_CONFIG, day=10)
        app.set_happy_hours(booked=[hh_event("17:40", "18:10")])
        app.set_axle("export", "17:30", "18:30", day=10)
        app.at("17:36", day=10)
        app.reconcile()
        self.assertNotIn(HH_SWITCH, [op[1] for batch in app.ops for op in batch])


class ImportEventTests(unittest.TestCase):
    def test_no_export_but_charging_allowed(self):
        app = dispatch_app(None, EV_CONFIG, {"peak_export": True, "ev_follow": {"debounce_seconds": 0}})
        app.set_axle("import", "16:30", "17:30")
        app.set_charger()
        app.set_soc(100)
        app.at("16:40")
        app.reconcile()
        batch = app.ops[0]
        self.assertIn(("switch", EV_SWITCH, True), batch)
        self.assertNotIn(SWITCH_CID, [op[1] for op in batch])
        self.assertNotIn("standing aside", app.overview()[2]["decision"] or "")
        self.assertEqual(app.status()[2]["axle_event_type"], "import")


class EventsOnlyTests(unittest.TestCase):
    def test_control_off_still_stands_aside(self):
        app = dispatch_app({"control": False})
        app.set_axle("export", "17:30", "18:30")
        ev = app.axle_event(local("12:00"))
        self.assertEqual(ev["end"], local("18:40"))  # grace period
        app.at("17:45")
        app.reconcile()
        self.assertEqual(app.ops, [])
        self.assertEqual(app.status()[1], "axle_event")

    def test_control_needs_axle_enabled(self):
        app = App({"axle": {"enabled": False, "control": True}})
        self.assertFalse(app.AXLE_CONTROL)


if __name__ == "__main__":
    unittest.main()
