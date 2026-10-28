"""The arbiter: every feature on at once, and the clashes the separate apps used to have."""
import unittest

from tests.harness import (App, CHARGE_SWITCH_CIDS, CHARGE_TIME_CIDS, CURRENT_CID, EV_CONFIG, HH_CONFIG,
                           SOC_CID, SWITCH_CID, TIME_CID, event, hh_event)

EV_TIME, EV_SWITCH = CHARGE_TIME_CIDS[1], CHARGE_SWITCH_CIDS[1]
HH_TIME, HH_SWITCH = CHARGE_TIME_CIDS[2], CHARGE_SWITCH_CIDS[2]
EXPORT_OFF = [("switch", SWITCH_CID, False), ("value", TIME_CID, "00:00-00:00")]
EV_OFF = [("switch", EV_SWITCH, False), ("value", EV_TIME, "00:00-00:00")]


def all_app(extra=None, day=6):
    """Every feature on, with every slot known (core window already programmed)."""
    app = App(EV_CONFIG, HH_CONFIG, {
        "peak_export": True, "axle_events": True, "battery_capacity_kwh": 16.1,
        "ev_follow": {"debounce_seconds": 0},
    }, extra)
    app.capture_ops()
    app.known()
    app.g["_slots"]["core"].update({"window": "23:30-05:30", "enabled": True})
    app.set_events()
    app.set_happy_hours()
    app.set_axle()
    app.set_charger(dispatch="off")
    app.set_soc(100)
    app.at("12:00", day=day)
    return app


def cids(batch):
    return [op[1] for op in batch]


class EvVersusExportTests(unittest.TestCase):
    def test_ev_dispatch_pauses_peak_export_and_resumes_after(self):
        app = all_app()
        app.at("15:45")
        app.reconcile()
        self.assertEqual(app.ops[-1][0], ("value", TIME_CID, "16:00-19:00"))

        # Car starts charging mid-peak: export off first, then the EV slot on, in one batch.
        app.ops.clear()
        app.set_charger()
        app.at("16:40")
        app.reconcile()
        self.assertEqual(app.ops, [EXPORT_OFF + [("value", EV_TIME, "16:30-17:00"), ("switch", EV_SWITCH, True)]])
        self.assertEqual(app.status()[1], "paused")
        self.assertIn("EV charging", app.overview()[2]["decision"])

        # Car stops: the EV slot finishes its half-hour, export stays paused until then.
        app.ops.clear()
        app.set_charger(dispatch="off")
        app.at("16:50")
        app.reconcile()
        self.assertEqual(app.ops, [])

        # Half-hour over: EV slot cleared and the rest of the peak window exported.
        app.at("17:00")
        app.reconcile()
        batch = app.ops[0]
        self.assertEqual(batch[:2], EV_OFF)
        self.assertEqual(batch[2], ("value", TIME_CID, "16:00-19:00"))
        self.assertEqual(batch[-1], ("switch", SWITCH_CID, True))
        self.assertEqual(app.status()[1], "exporting")

    def test_ev_dispatch_pauses_power_down(self):
        app = all_app({"peak_export": False})
        app.set_events(joined=[event("17:00", "18:00")])
        app.set_charger()
        app.at("17:10")
        app.reconcile()
        self.assertEqual(app.ops, [[("value", EV_TIME, "17:00-17:30"), ("switch", EV_SWITCH, True)]])
        self.assertEqual(app.status()[1], "paused")
        self.assertEqual(app.active(), "on")  # still a Power Down session, just not exporting

    def test_no_ev_dispatch_exports_normally(self):
        app = all_app({"peak_export": False})
        app.set_events(joined=[event("17:00", "18:00")])
        app.at("16:45")
        app.reconcile()
        self.assertEqual(cids(app.ops[0]), [TIME_CID, CURRENT_CID, SOC_CID, SWITCH_CID])


class AxleTests(unittest.TestCase):
    def test_ev_dispatch_during_axle_event_writes_nothing(self):
        app = all_app()
        app.set_axle("export", "17:30", "18:30")
        app.set_charger()
        app.at("17:45")
        app.reconcile()
        self.assertEqual(app.ops, [])
        self.assertEqual(app.overview()[1], "axle_event")
        self.assertIn("Axle", app.overview()[2]["decision"])

    def test_running_ev_slot_switched_off_once_when_axle_starts(self):
        app = all_app()
        app.set_charger()
        app.at("17:20")
        app.reconcile()
        self.assertEqual(app.slot("ev"), "17:00-17:30")
        app.ops.clear()
        app.set_axle("export", "17:22", "18:30")
        app.at("17:23")
        app.reconcile()
        self.assertEqual(app.ops, [EV_OFF])
        app.ops.clear()
        app.at("17:24")
        app.reconcile()
        self.assertEqual(app.ops, [])

    def test_happy_hour_side_effects_still_run_during_axle(self):
        app = all_app(day=10)
        app.set_happy_hours(booked=[hh_event("14:00", "15:00")])
        app.set_axle("import", "13:30", "16:00", day=10)
        app.at("14:00", day=10)
        app.reconcile()
        self.assertEqual(app.ops, [])
        self.assertEqual(len(app.services.boosts()), 1)


class HappyHourTests(unittest.TestCase):
    def test_happy_hour_pauses_overlapping_peak_export(self):
        app = all_app(day=10)
        app.set_happy_hours(booked=[hh_event("16:30", "17:30")])
        app.at("15:45", day=10)
        app.reconcile()
        app.ops.clear()
        app.at("16:25", day=10)
        app.reconcile()
        batch = app.ops[0]
        self.assertEqual(batch[:2], EXPORT_OFF)
        self.assertEqual(batch[2], ("value", HH_TIME, "16:30-17:30"))
        self.assertEqual(batch[-1], ("switch", HH_SWITCH, True))
        self.assertIn("happy hour", app.overview()[2]["decision"])

    def test_power_down_beats_happy_hour_charging(self):
        app = all_app({"peak_export": False}, day=10)
        app.set_events(joined=[event("17:00", "18:00", day=10)])
        app.set_happy_hours(booked=[hh_event("17:00", "18:00")])
        app.at("16:58", day=10)
        app.reconcile()
        self.assertNotIn(HH_SWITCH, cids(app.ops[0]))
        self.assertIn(SWITCH_CID, cids(app.ops[0]))
        self.assertIn("Power Down", app.overview()[2]["decision"])

    def test_ev_and_happy_hour_both_charge(self):
        app = all_app({"peak_export": False}, day=10)
        app.set_happy_hours(booked=[hh_event("14:00", "15:00")])
        app.set_charger()
        app.at("14:05", day=10)
        app.reconcile()
        switches = [op for op in app.ops[0] if op[0] == "switch"]
        self.assertEqual(switches, [("switch", EV_SWITCH, True), ("switch", HH_SWITCH, True)])


class SharedClientTests(unittest.TestCase):
    def real_cloud_app(self):
        app = App(EV_CONFIG, HH_CONFIG, {"peak_export": True, "sync_inverter_time": False,
                                         "ev_follow": {"debounce_seconds": 0}})
        app.set_events()
        app.set_happy_hours()
        app.set_charger()
        app.at("14:12")
        return app

    def test_one_login_and_one_switch_read_per_pass(self):
        app = self.real_cloud_app()
        app.reconcile("startup")
        # Every slot was unknown: core, EV (charging now), happy hour and export all written.
        written = [w[0] for w in app.cloud.control_writes()]
        for cid in (CHARGE_TIME_CIDS[0], EV_TIME, HH_TIME, TIME_CID):
            self.assertIn(cid, written)
        self.assertEqual(len(app.cloud.logins()), 1)
        self.assertEqual(app.cloud.paths().count("/v2/api/atReadBatch"), 1)

    def test_switch_masks_chain_within_a_batch(self):
        app = self.real_cloud_app()
        app.reconcile("startup")
        switch_writes = [w for w in app.cloud.control_writes() if w[0] in CHARGE_SWITCH_CIDS + [SWITCH_CID]]
        # Each switch write carries the mask including the ones before it in the batch.
        self.assertEqual([w[0] for w in switch_writes], [CHARGE_SWITCH_CIDS[0], EV_SWITCH])
        masks = [int(w[2]) for w in switch_writes]
        bits = {CHARGE_SWITCH_CIDS[0]: 1 << 0, EV_SWITCH: 1 << 1}
        expected = 0
        for (cid, value, _), mask in zip(switch_writes, masks):
            self.assertEqual(mask, expected)
            if value == "1":
                expected |= bits.get(cid, 0)
            else:
                expected &= ~bits.get(cid, 0)

    def test_write_lock_waits_for_the_other_writer(self):
        app = self.real_cloud_app()
        app.g["_flags"]["write_lock"] = True

        def release(_):
            app.g["_flags"]["write_lock"] = False

        app.asyncio.on_sleep = release
        ok = app.run(app.solis_apply([("value", TIME_CID, "00:00-00:00")]))
        self.assertTrue(ok)
        self.assertEqual(app.asyncio.sleeps[0], 0.5)
        self.assertFalse(app.g["_flags"]["write_lock"])

    def test_lock_released_after_failure(self):
        app = self.real_cloud_app()
        app.cloud.fail_cids.add(TIME_CID)
        app.g["_flags"]["write_lock"] = False
        ok = app.run(app.solis_apply([("value", TIME_CID, "00:00-00:00")]))
        self.assertFalse(ok)
        self.assertFalse(app.g["_flags"]["write_lock"])

    def test_restart_does_not_wipe_happy_hour_slot(self):
        app = App(EV_CONFIG, HH_CONFIG, {"slots": {"clear_other_charge_slots": True}})
        self.assertEqual(app.other_charge_slots(), [4, 5, 6])

    def test_slot_clash_is_reported(self):
        app = App(EV_CONFIG, HH_CONFIG, {"slots": {"happy_hour": 2}})
        self.assertIn("slots.happy_hour and slots.ev are both charge slot 2", app.SLOT_PROBLEMS)


class OverviewTests(unittest.TestCase):
    def test_overview_lists_slots_and_features(self):
        app = all_app()
        app.reconcile()
        _, value, attrs = app.overview()
        self.assertEqual(value, "idle")
        self.assertEqual(set(attrs["slots"]), {"core", "ev", "happy_hour", "export"})
        self.assertEqual(attrs["slots"]["core"]["window"], "23:30-05:30")
        self.assertTrue(all(attrs["features"].values()))


if __name__ == "__main__":
    unittest.main()
