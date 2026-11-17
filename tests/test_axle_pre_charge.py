"""Axle Self-Dispatch pre-charge: top the battery up from the grid just before an Axle export event."""
import unittest

from tests.harness import (App, CHARGE_CURRENTS, CHARGE_SOCS, CHARGE_SWITCHES, CHARGE_TIMES, EXPORT_CURRENT,
                           EXPORT_SOC, EXPORT_SWITCH, EXPORT_TIME, HH_CONFIG, local)

PC_TIME, PC_CURRENT, PC_SOC, PC_SWITCH = CHARGE_TIMES[3], CHARGE_CURRENTS[3], CHARGE_SOCS[3], CHARGE_SWITCHES[3]
PC_OFF = [("switch", PC_SWITCH, False), ("time", PC_TIME, "00:00-00:00")]


def pc_app(axle=None, *more):
    """
    Axle Self-Dispatch with pre-charge; 10 kWh battery, 10% floor, 0.5 kW house,
    50 V, 100 A (5 kW) both ways, 5 kW export limit, no losses (see harness).
    """
    cfg = {"control": True}
    cfg.update(axle or {})
    app = App({"axle_events": True, "axle": cfg, "battery": {"charge_efficiency": 1.0}}, *more)
    app.capture_ops()
    app.known()
    app.g["_slots"]["core"].update({"window": "23:30-05:30", "enabled": True})
    app.set_events()
    app.set_axle("export", "17:30", "18:30")
    app.at("12:00")
    return app


def arm_charge(window, soc, current=100):
    return [("time", PC_TIME, window), ("value", PC_CURRENT, str(current)),
            ("value", PC_SOC, str(soc)), ("switch", PC_SWITCH, True)]


def charge_ops(app):
    """Only the writes to the pre-charge slot, batch by batch."""
    out = []
    for batch in app.ops:
        mine = [op for op in batch if op[1] in (PC_TIME, PC_CURRENT, PC_SOC, PC_SWITCH)]
        if mine:
            out.append(mine)
    return out


class PlanTests(unittest.TestCase):
    def test_sizes_the_charge_to_finish_at_the_event_start(self):
        app = pc_app()
        app.set_soc(40)
        ev = app.axle_event(local("12:00"))
        plan = app.predict(ev, local("12:00"), 40, None, {}, {}, [], True, None, True)
        # 1.25 kWh left at 17:30 without charging. Wanted: where the export
        # finishes (the 10% floor + the 3% stop margin) + 5 kW for the hour = 6.3 kWh.
        self.assertEqual(plan["battery_at_start_without_pre_charge_kwh"], 1.25)
        self.assertEqual(plan["pre_charge_needed_kwh"], 5.05)
        self.assertEqual(plan["pre_charge_soc"], 63)
        # The grid covers the house while charging, so the battery stops draining:
        # 5.05 kWh short = 5 kW charge + 0.5 kW drain avoided, ~55 min, from 16:30.
        self.assertEqual(plan["pre_charge_window"], "16:30-17:30")
        self.assertEqual(plan["pre_charge_kwh"], 4.55)
        # The export is planned as if the charge happened: full rate for the whole event.
        self.assertEqual(plan["battery_at_start_kwh"], 6.3)
        self.assertEqual(plan["export_window"], "17:30-18:30")
        self.assertEqual(plan["discharge_current"], 100)

    def test_no_charge_when_the_battery_has_enough(self):
        app = pc_app()
        ev = app.axle_event(local("12:00"))
        plan = app.predict(ev, local("12:00"), 100, None, {}, {}, [], True, None, True)
        self.assertEqual(plan["pre_charge_needed_kwh"], 0)
        self.assertIsNone(plan["pre_charge_window"])

    def test_small_shortfall_is_ignored(self):
        app = pc_app()
        ev = app.axle_event(local("12:00"))
        # 8.8 kWh now, 6.05 at 17:30: 0.25 kWh short, under pre_charge_min_kwh.
        plan = app.predict(ev, local("12:00"), 88, None, {}, {}, [], True, None, True)
        self.assertEqual(plan["pre_charge_needed_kwh"], 0.25)
        self.assertIsNone(plan["pre_charge_window"])

    def test_keeps_the_evening_reserve_when_asked(self):
        app = pc_app({"keep_evening_reserve": True})
        ev = app.axle_event(local("12:00"))
        plan = app.predict(ev, local("12:00"), 40, None, {}, {}, [], False, None, True)
        # Floor + 18:30-23:30 at 0.5 kW + 5 kWh for the event.
        self.assertEqual(plan["pre_charge_soc"], 85)

    def test_charge_power_capped_by_current(self):
        app = pc_app({"pre_charge_current": 50})
        ev = app.axle_event(local("12:00"))
        plan = app.predict(ev, local("12:00"), 40, None, {}, {}, [], True, None, True)
        self.assertEqual(plan["pre_charge_current"], 50)
        # 2.5 kW: 5.05 kWh short = 3 kW x ~101 min, from 15:45.
        self.assertEqual(plan["pre_charge_window"], "15:45-17:30")


class SlotTests(unittest.TestCase):
    def test_armed_before_runs_to_the_event_and_cleared_at_the_start(self):
        app = pc_app()
        app.set_soc(20)
        app.at("16:19")
        app.reconcile()
        self.assertEqual(app.ops, [])
        # Charge 16:35-17:30, written prepare_minutes (15) ahead.
        app.at("16:20")
        app.reconcile()
        self.assertEqual(app.ops, [arm_charge("16:35-17:30", 63)])
        self.assertEqual(app.overview()[2]["axle_pre_charge"]["programmed_window"], "16:35-17:30")
        # Charging: the battery's on its way up. The window stays put.
        app.ops.clear()
        app.at("17:00")
        app.set_soc(36)
        app.reconcile()
        self.assertEqual(app.ops, [])
        self.assertEqual(app.overview()[1], "charging")
        # The export is armed for full rate across the whole event.
        app.at("17:15")
        app.set_soc(51)
        app.reconcile()
        self.assertEqual(app.ops, [[("time", EXPORT_TIME, "17:30-18:30"), ("value", EXPORT_CURRENT, "100"),
                                    ("value", EXPORT_SOC, "10"), ("switch", EXPORT_SWITCH, True)]])
        # Full already: the slot stays on, holding the SOC until the event.
        app.ops.clear()
        app.at("17:25")
        app.set_soc(63)
        app.reconcile()
        self.assertEqual(app.ops, [])
        # The event starts: the charge slot goes.
        app.at("17:30")
        app.reconcile()
        self.assertEqual(charge_ops(app), [PC_OFF])
        self.assertEqual(app.slot("axle_charge"), "00:00-00:00")
        self.assertEqual(app.overview()[1], "exporting")

    def test_never_moved_later_but_dropped_if_no_longer_needed(self):
        app = pc_app()
        app.set_soc(20)
        app.at("16:20")
        app.reconcile()
        app.ops.clear()
        # Solar: a bit more in the battery than predicted. The plan would start later.
        app.at("16:25")
        app.set_soc(26)
        app.reconcile()
        self.assertEqual(app.ops, [])
        # Lots more: no charge needed before it has begun.
        app.at("16:30")
        app.set_soc(90)
        app.reconcile()
        self.assertEqual(charge_ops(app), [PC_OFF])

    def test_moved_earlier_when_the_house_uses_more(self):
        app = pc_app()
        app.set_soc(20)
        app.at("16:20")
        app.reconcile()
        app.ops.clear()
        app.at("16:25")
        app.set_soc(10)
        app.reconcile()
        self.assertEqual(charge_ops(app)[0][0], ("time", PC_TIME, "16:25-17:30"))

    def test_short_notice_event_charges_what_it_can(self):
        app = pc_app()
        app.set_axle()
        app.set_soc(20)
        app.at("17:00")
        app.reconcile()
        self.assertEqual(app.ops, [])
        app.set_axle("export", "17:30", "18:30")
        app.reconcile()
        # 4.55 kWh short, but only 30 minutes at 5 kW left: 2.5 kWh.
        self.assertEqual(charge_ops(app), [arm_charge("17:00-17:30", 63)])
        plan = app.status()[2]
        self.assertEqual(plan["pre_charge_needed_kwh"], 4.55)
        self.assertEqual(plan["pre_charge_kwh"], 2.5)
        self.assertEqual(plan["battery_at_start_kwh"], 4.5)

    def test_notification_once(self):
        app = pc_app(None, {"notify_service": "notify.phone"})
        app.set_soc(20)
        for t in ("16:20", "16:25", "16:30"):
            app.at(t)
            app.reconcile()
        titles = app.services.titles()
        self.assertEqual(titles.count("Axle pre-charge planned"), 1)

    def test_failed_write_is_retried(self):
        app = pc_app()
        app.capture_ops(ok=[False, True])
        app.set_soc(20)
        app.at("16:20")
        app.reconcile()
        self.assertTrue(app.g["_slots"]["axle_charge"]["unknown"])
        app.at("16:21")
        app.reconcile()
        self.assertEqual(app.ops[-1], arm_charge("16:35-17:30", 63))
        self.assertEqual(app.g["_pre_charge_run"]["start"], local("16:35"))


class ModeTests(unittest.TestCase):
    def test_off_in_events_only_mode(self):
        app = pc_app({"control": False})
        self.assertFalse(app.AXLE_PRE_CHARGE)
        self.assertNotIn("axle_charge", app.active_slot_keys())
        app.set_soc(20)
        app.at("16:20")
        app.reconcile()
        self.assertEqual(charge_ops(app), [])

    def test_can_be_switched_off(self):
        app = pc_app({"pre_charge": False})
        self.assertNotIn("axle_charge", app.active_slot_keys())
        app.set_soc(20)
        app.at("16:20")
        app.reconcile()
        self.assertEqual(charge_ops(app), [])

    def test_import_event_never_pre_charges(self):
        app = pc_app()
        app.set_axle("import", "17:30", "18:30")
        app.set_soc(20)
        app.at("16:20")
        app.reconcile()
        self.assertEqual(charge_ops(app), [])

    def test_slot_and_export_slot_active_with_axle_only(self):
        app = pc_app(None, {"power_down": None})
        self.assertEqual(app.active_slot_keys(), ["export", "axle_charge"])
        app.set_soc(40)
        app.at("17:15")
        app.reconcile()
        self.assertTrue(app.g["_slots"]["export"]["enabled"])

    def test_slot_clash_reported(self):
        app = pc_app({}, HH_CONFIG, {"slots": {"axle_charge": 3}})
        self.assertIn("slots.axle_charge and slots.happy_hour are both charge slot 3", app.SLOT_PROBLEMS)


class OtherWindowTests(unittest.TestCase):
    def test_after_a_peak_window(self):
        app = pc_app(None, {"peak_export": True})
        app.set_soc(20)
        app.at("16:20")
        app.reconcile()
        attrs = app.status()[2]
        self.assertEqual(attrs["window"], "16:00-17:30")
        self.assertEqual(attrs["axle_pre_charge"]["programmed_window"], "16:35-17:30")
        self.assertEqual(charge_ops(app), [arm_charge("16:35-17:30", 63)])
        self.assertFalse(app.g["_slots"]["export"]["enabled"])

    def test_peak_export_paused_while_pre_charging(self):
        app = pc_app()
        peak = {"id": "peak", "kind": "peak", "start": local("16:00"), "end": local("17:30")}
        pc = {"window": "16:35-17:30", "current": 100, "soc": 60, "enabled": True,
              "start": local("16:35"), "end": local("17:30")}
        export = {"window": "16:00-17:30", "current": 50, "soc": 40, "enabled": True, "start": local("16:00")}
        pc2, export2, note = app.pre_charge_clash(pc, peak, export, local("16:20"))
        self.assertEqual(pc2, pc)
        self.assertFalse(export2["enabled"])
        self.assertEqual(note, "peak export paused: Axle pre-charge")

    def test_no_grid_charging_during_power_down(self):
        app = pc_app()
        pd = {"id": "pd", "kind": "power_down", "start": local("16:00"), "end": local("17:00")}
        pc = {"window": "16:35-17:30", "current": 100, "soc": 60, "enabled": True,
              "start": local("16:35"), "end": local("17:30")}
        export = {"window": "16:30-17:00", "current": 50, "soc": 40, "enabled": True, "start": local("16:30")}
        pc2, export2, note = app.pre_charge_clash(pc, pd, export, local("16:50"))
        self.assertEqual(pc2["window"], "17:00-17:30")
        self.assertTrue(export2["enabled"])
        self.assertEqual(note, "Axle pre-charge waits for Power Down")
        # A session running right up to the event: no pre-charge at all.
        pd["end"] = local("17:30")
        pc2, _, note = app.pre_charge_clash(pc, pd, export, local("16:50"))
        self.assertFalse(pc2["enabled"])
        self.assertEqual(note, "Axle pre-charge skipped: Power Down")


if __name__ == "__main__":
    unittest.main()
