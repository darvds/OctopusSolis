"""EV-Follow: ported from SolisOctopusEVFollow, on the combined app."""
import unittest

from tests.harness import (App, CHARGE_SWITCH_CIDS, CHARGE_TIME_CIDS, CHARGER_CURRENT, CHARGER_SWITCH,
                           DISPATCH, EV_CONFIG, local)

EV_TIME, EV_SWITCH = CHARGE_TIME_CIDS[1], CHARGE_SWITCH_CIDS[1]
CORE_TIME, CORE_SWITCH = CHARGE_TIME_CIDS[0], CHARGE_SWITCH_CIDS[0]


def ev_app(ev=None, extra=None, debounce=0):
    """EV-Follow only, with the core window already programmed and no debounce."""
    cfg = dict(EV_CONFIG["ev_follow"])
    cfg["debounce_seconds"] = debounce
    cfg.update(ev or {})
    app = App({"power_down": None, "ev_follow": cfg}, extra)
    app.capture_ops()
    app.known()
    app.g["_slots"]["core"].update({"window": "23:30-05:30", "enabled": True})
    return app


def start(window):
    return [("value", EV_TIME, window), ("switch", EV_SWITCH, True)]


def extend(window):
    return [("value", EV_TIME, window)]


CLEAR = [("switch", EV_SWITCH, False), ("value", EV_TIME, "00:00-00:00")]


class RollingSlotScenarioTests(unittest.TestCase):
    """The behaviour EV-Follow exists for, step by step through an afternoon."""

    def test_afternoon_dispatch(self):
        app = ev_app()
        steps = [
            # time,   charging, expected batch (None = no write), programmed slot after
            ("14:00", False, None, "00:00-00:00"),
            ("14:12", True, start("14:00-14:30"), "14:00-14:30"),
            ("14:20", True, None, "14:00-14:30"),
            ("14:27", True, extend("14:00-15:00"), "14:00-15:00"),
            ("14:45", True, None, "14:00-15:00"),
            ("14:57", True, extend("14:00-15:30"), "14:00-15:30"),
            ("15:10", False, None, "14:00-15:30"),  # car stopped: finish the half-hour
            ("15:25", False, None, "14:00-15:30"),
            ("15:30", False, CLEAR, "00:00-00:00"),  # expired: clear it
            ("15:35", False, None, "00:00-00:00"),
        ]
        for hhmm, charging, expected, slot in steps:
            with self.subTest(time=hhmm):
                app.ops.clear()
                app.at(hhmm)
                app.set_charger(dispatch="on" if charging else "off")
                app.reconcile()
                self.assertEqual(app.ops, [expected] if expected else [])
                self.assertEqual(app.slot("ev"), slot)

    def test_charging_resumes_after_slot_expired(self):
        app = ev_app()
        app.set_charger()
        app.at("14:12")
        app.reconcile()
        app.set_charger(dispatch="off")
        app.at("14:30")
        app.reconcile()
        self.assertEqual(app.slot("ev"), "00:00-00:00")
        app.set_charger()
        app.at("14:41")
        app.reconcile()
        self.assertEqual(app.slot("ev"), "14:30-15:00")

    def test_charging_resumes_before_slot_expired_keeps_start(self):
        app = ev_app()
        app.set_charger()
        app.at("14:05")
        app.reconcile()
        app.set_charger(dispatch="off")
        app.at("14:15")
        app.reconcile()
        app.set_charger()
        app.at("14:27")
        app.reconcile()
        self.assertEqual(app.slot("ev"), "14:00-15:00")


class ExtensionTimingTests(unittest.TestCase):
    def test_extends_exactly_at_lead_time(self):
        app = ev_app()
        app.set_charger()
        app.at("14:05")
        app.reconcile()
        app.at("14:26", second=59)
        app.reconcile()
        self.assertEqual(app.slot("ev"), "14:00-14:30")
        app.at("14:27")
        app.reconcile()
        self.assertEqual(app.slot("ev"), "14:00-15:00")

    def test_custom_lead_time(self):
        app = ev_app({"extend_lead_minutes": 5})
        app.set_charger()
        app.at("14:05")
        app.reconcile()
        app.at("14:25")
        app.reconcile()
        self.assertEqual(app.slot("ev"), "14:00-15:00")

    def test_slot_not_shortened_by_next_check(self):
        app = ev_app()
        app.set_charger()
        app.at("14:57")
        app.reconcile()
        self.assertEqual(app.slot("ev"), "14:30-15:30")
        app.at("15:01")
        app.reconcile()
        self.assertEqual(app.slot("ev"), "14:30-15:30")

    def test_slot_never_shrinks_if_clock_goes_backwards(self):
        app = ev_app()
        app.set_charger()
        app.at("14:57")
        app.reconcile()
        app.ops.clear()
        app.at("14:50")
        app.reconcile()
        self.assertEqual(app.slot("ev"), "14:30-15:30")
        self.assertEqual(app.ops, [])

    def test_desired_slot_directly(self):
        app = ev_app()
        self.assertEqual(app.ev_desired_slot(local("14:12"), False), (None, None))
        self.assertEqual(app.ev_desired_slot(local("14:12"), True), (local("14:00"), local("14:30")))


class CoreWindowInteractionTests(unittest.TestCase):
    def test_not_extended_into_core_window(self):
        app = ev_app()
        app.set_charger()
        app.at("23:12")
        app.reconcile()
        self.assertEqual(app.slot("ev"), "23:00-23:30")
        app.at("23:27")
        app.reconcile()
        self.assertEqual(app.slot("ev"), "23:00-23:30")
        app.at("23:35")
        app.reconcile()
        self.assertEqual(app.ops[-1], CLEAR)

    def test_no_slot_inside_core_window(self):
        app = ev_app()
        app.set_charger()
        app.at("02:00", day=7)
        app.reconcile()
        self.assertEqual(app.ops, [])

    def test_slot_starts_at_end_of_core_window(self):
        app = ev_app()
        app.set_charger()
        app.at("05:27", day=7)
        app.reconcile()
        self.assertEqual(app.ops, [start("05:00-06:00")])

    def test_without_core_management_charges_overnight(self):
        app = ev_app({"manage_core_slot": False})
        app.set_charger()
        app.at("02:10", day=7)
        app.reconcile()
        self.assertEqual(app.ops, [start("02:00-02:30")])


class WriteBehaviourTests(unittest.TestCase):
    def test_no_write_when_idle_and_known(self):
        app = ev_app()
        app.set_charger(dispatch="off")
        app.reconcile()
        app.reconcile()
        self.assertEqual(app.ops, [])

    def test_unknown_inverter_state_is_cleared(self):
        app = ev_app()
        app.g["_slots"]["ev"]["unknown"] = True
        app.set_charger(dispatch="off")
        app.reconcile()
        self.assertEqual(app.ops, [CLEAR])
        self.assertFalse(app.g["_slots"]["ev"]["unknown"])
        app.reconcile()
        self.assertEqual(len(app.ops), 1)

    def test_failed_write_is_retried(self):
        app = ev_app()
        app.capture_ops(ok=[False, True])
        app.set_charger()
        app.at("14:12")
        app.reconcile()
        self.assertTrue(app.g["_slots"]["ev"]["unknown"])
        self.assertFalse(app.g["_flags"]["last_write_ok"])
        app.at("14:15")
        app.reconcile()
        self.assertEqual(app.ops, [start("14:00-14:30"), start("14:00-14:30")])
        self.assertEqual(app.slot("ev"), "14:00-14:30")
        self.assertTrue(app.g["_flags"]["last_write_ok"])

    def test_custom_ev_slot(self):
        app = ev_app(extra={"slots": {"ev": 5}})
        app.set_charger()
        app.at("14:12")
        app.reconcile()
        self.assertEqual(app.ops, [[("value", CHARGE_TIME_CIDS[4], "14:00-14:30"),
                                    ("switch", CHARGE_SWITCH_CIDS[4], True)]])

    def test_busy_flag_released_after_error(self):
        app = ev_app()

        async def broken(_):
            raise RuntimeError("boom")

        app.g["solis_apply"] = broken
        app.set_charger()
        with self.assertRaises(RuntimeError):
            app.reconcile()
        self.assertFalse(app.g["_flags"]["busy"])

    def test_missing_dispatch_sensor_never_charges(self):
        app = ev_app({"dispatch_sensor": None})
        app.set_charger()
        app.reconcile()
        self.assertEqual(app.ops, [])


class DebounceTests(unittest.TestCase):
    def test_periodic_waits_for_debounce(self):
        app = ev_app(debounce=60)
        app.set_charger()
        app.at("14:12")
        app.reconcile()
        self.assertEqual(app.ops, [])  # first seen now
        app.at("14:12", second=30)
        app.reconcile()
        self.assertEqual(app.ops, [])
        app.at("14:13")
        app.reconcile()
        self.assertEqual(app.ops, [start("14:00-14:30")])

    def test_blip_resets_debounce(self):
        app = ev_app(debounce=60)
        app.set_charger()
        app.at("14:12")
        app.reconcile()
        app.set_charger(amps="0")
        app.at("14:12", second=30)
        app.reconcile()
        app.set_charger()
        app.at("14:13")
        app.reconcile()
        self.assertEqual(app.ops, [])


class StateChangeTests(unittest.TestCase):
    def setUp(self):
        self.app = ev_app(debounce=60)
        self.app.at("14:12")

    def fire(self):
        self.app.run(self.app.solis_octopus_ev_changed(var_name=CHARGER_CURRENT, value="16"))

    def test_sustained_charging_starts_battery_after_debounce(self):
        self.app.set_charger()
        self.fire()
        self.assertEqual(self.app.asyncio.sleeps, [60])
        self.assertIn(("solis_octopus_ev_debounce", True), self.app.unique_calls)
        self.assertEqual(self.app.ops, [start("14:00-14:30")])

    def test_brief_blip_is_ignored(self):
        self.app.set_charger()
        self.app.asyncio.on_sleep = lambda _: self.app.set_charger(amps="0")
        with self.assertLogs("pyscript.solis_octopus", "INFO") as logs:
            self.fire()
        self.assertEqual(self.app.ops, [])
        self.assertTrue(any("did not persist" in line for line in logs.output))

    def test_custom_debounce(self):
        app = ev_app(debounce=15)
        app.set_charger()
        app.run(app.solis_octopus_ev_changed())
        self.assertEqual(app.asyncio.sleeps, [15])

    def test_only_acts_when_charging_state_flips(self):
        self.app.set_charger()
        self.fire()
        self.fire()  # current fluctuates, still charging: nothing new
        self.assertEqual(self.app.asyncio.sleeps, [60])
        self.assertEqual(len(self.app.ops), 1)

    def test_stopping_keeps_the_half_hour(self):
        self.app.set_charger()
        self.fire()
        self.app.set_charger(amps="0")
        self.fire()
        self.assertEqual(self.app.ops, [start("14:00-14:30")])
        self.assertEqual(self.app.slot("ev"), "14:00-14:30")

    def test_not_charging_does_nothing(self):
        self.app.set_charger(dispatch="off")
        self.app.g["_ev"]["last_charging"] = False
        self.fire()
        self.assertEqual(self.app.asyncio.sleeps, [])
        self.assertEqual(self.app.ops, [])

    def test_trigger_registered_on_charger_entities(self):
        triggers = {name: (kind, args) for kind, args, name in self.app.triggers}
        self.assertEqual(triggers["solis_octopus_ev_changed"],
                         ("state_trigger", (CHARGER_SWITCH, CHARGER_CURRENT, DISPATCH)))


class CoreSlotTests(unittest.TestCase):
    def fresh(self, ev=None, extra=None):
        cfg = dict(EV_CONFIG["ev_follow"])
        cfg.update(ev or {})
        app = App({"power_down": None, "ev_follow": cfg}, extra)
        app.capture_ops()
        app.set_charger(dispatch="off")
        return app

    def test_first_pass_writes_core_and_clears_ev(self):
        app = self.fresh()
        app.reconcile()
        self.assertEqual(app.ops, [[
            ("switch", EV_SWITCH, False), ("value", EV_TIME, "00:00-00:00"),
            ("value", CORE_TIME, "23:30-05:30"), ("switch", CORE_SWITCH, True),
        ]])
        app.ops.clear()
        app.reconcile()
        self.assertEqual(app.ops, [])

    def test_spaces_stripped_from_core_window(self):
        app = self.fresh(extra={"core_window": "23:30 - 05:30"})
        app.reconcile()
        self.assertIn(("value", CORE_TIME, "23:30-05:30"), app.ops[0])

    def test_without_core_management(self):
        app = self.fresh({"manage_core_slot": False})
        app.reconcile()
        self.assertEqual(app.ops, [[("switch", EV_SWITCH, False), ("value", EV_TIME, "00:00-00:00")]])

    def test_other_charge_slots_left_alone_by_default(self):
        app = self.fresh()
        app.reconcile()
        cids = [op[1] for op in app.ops[0]]
        for i in (2, 3, 4, 5):
            self.assertNotIn(CHARGE_TIME_CIDS[i], cids)

    def test_clear_other_charge_slots_once(self):
        app = self.fresh(extra={"slots": {"clear_other_charge_slots": True}})
        app.reconcile()
        cids = [op[1] for op in app.ops[0]]
        for i in (2, 3, 4, 5):
            self.assertIn(CHARGE_TIME_CIDS[i], cids)
            self.assertIn(CHARGE_SWITCH_CIDS[i], cids)
        app.ops.clear()
        app.reconcile()
        self.assertEqual(app.ops, [])

    def test_ev_slot_clashing_with_core_is_refused(self):
        app = self.fresh(extra={"slots": {"ev": 1}})
        self.assertTrue(app.SLOT_PROBLEMS)
        with self.assertLogs("pyscript.solis_octopus", "ERROR"):
            app.run(app.solis_octopus_startup())
        self.assertEqual(app.ops, [])


class StatusSensorTests(unittest.TestCase):
    def test_status_while_charging(self):
        app = ev_app()
        app.set_charger(amps="16")
        app.at("14:12")
        app.reconcile("charging started")
        entity, value, attrs = app.ev_status()
        self.assertEqual(value, "charging")
        self.assertEqual(attrs["programmed_slot"], "14:00-14:30")
        self.assertTrue(attrs["ev_charging"])
        self.assertEqual(attrs["charger_amps"], 16.0)
        self.assertEqual(attrs["dispatch_state"], "on")
        self.assertEqual(attrs["last_reason"], "charging started")
        self.assertTrue(attrs["last_write_ok"])
        self.assertFalse(attrs["diagnostics_only"])
        self.assertEqual(attrs["ev_slot"], 2)

    def test_status_when_idle(self):
        app = ev_app()
        app.set_charger(dispatch="off")
        app.reconcile()
        _, value, attrs = app.ev_status()
        self.assertEqual(value, "idle")
        self.assertEqual(attrs["programmed_slot"], "00:00-00:00")
        self.assertFalse(attrs["ev_charging"])


class CarChargingTests(unittest.TestCase):
    def setUp(self):
        self.app = ev_app()

    def charging(self):
        return self.app.car_charging()[0]

    def test_charging_during_started_dispatch(self):
        self.app.set_charger(switch="on", amps="16", dispatch="on")
        charging, info = self.app.car_charging()
        self.assertTrue(charging)
        self.assertEqual(info, {"charger_switch": "on", "charger_amps": 16.0, "dispatch": "on"})

    def test_drawing_power_without_dispatch_is_ignored(self):
        self.app.set_charger(amps="16", dispatch="off")
        self.assertFalse(self.charging())

    def test_dispatch_without_current_is_ignored(self):
        self.app.set_charger(amps="0", dispatch="on")
        self.assertFalse(self.charging())

    def test_charger_switch_off(self):
        self.app.set_charger(switch="off", amps="16", dispatch="on")
        self.assertFalse(self.charging())

    def test_current_threshold_is_inclusive(self):
        self.app.set_charger(amps="2.9")
        self.assertFalse(self.charging())
        self.app.set_charger(amps="3")
        self.assertTrue(self.charging())

    def test_custom_threshold(self):
        app = ev_app({"min_current_amps": 10})
        app.set_charger(amps="9.5")
        self.assertFalse(app.car_charging()[0])
        app.set_charger(amps="10")
        self.assertTrue(app.car_charging()[0])

    def test_unavailable_or_missing_current_counts_as_zero(self):
        for value in ("unavailable", "unknown", "", None):
            with self.subTest(value=value):
                self.app.set_charger(amps=value)
                charging, info = self.app.car_charging()
                self.assertFalse(charging)
                self.assertEqual(info["charger_amps"], 0.0)

    def test_unavailable_dispatch_sensor(self):
        self.app.set_charger(dispatch="unavailable")
        self.assertFalse(self.charging())
        self.app.set_charger(dispatch=None)
        self.assertFalse(self.charging())

    def test_custom_entity_ids(self):
        app = ev_app({
            "charger_switch": "switch.my_charger",
            "charger_current": "sensor.my_amps",
            "dispatch_sensor": "binary_sensor.my_dispatch",
        })
        app.states.update({"switch.my_charger": ("on", {}), "sensor.my_amps": ("32", {}),
                           "binary_sensor.my_dispatch": ("on", {})})
        self.assertTrue(app.car_charging()[0])


if __name__ == "__main__":
    unittest.main()
