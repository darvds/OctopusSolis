import unittest

from tests.hh_harness import (App, CURRENT_CID, SOC_CID, SWITCH_CID, TIME_CID, WATER_HEATER, booked, local)

ARM = [("value", TIME_CID, "14:00-15:00"), ("value", CURRENT_CID, "100"),
       ("value", SOC_CID, "100"), ("switch", SWITCH_CID, True)]
CLEAR = [("switch", SWITCH_CID, False), ("value", TIME_CID, "00:00-00:00")]

ACTIONS = {
    "start_actions": [
        {"service": "switch.turn_on", "target": {"entity_id": "switch.hypervolt_charging"}},
        "script.dishwasher_eco",
    ],
    "end_actions": [{"action": "switch.turn_off", "target": {"entity_id": "switch.hypervolt_charging"}}],
}


def happy_app(config=None, start="14:00", end="15:00"):
    app = App(config)
    app.capture_ops()
    app.g["_slots"]["happy_hour"]["unknown"] = False
    app.set_events(booked=[booked(start, end)])
    app.seed(local("09:00"))
    return app


class TimelineTests(unittest.TestCase):
    def test_happy_hour(self):
        app = happy_app(ACTIONS)
        steps = [
            # time,  inverter ops, status
            ("12:00", [], "scheduled"),
            ("13:54", [], "scheduled"),
            ("13:55", [ARM], "armed"),
            ("13:59", [], "armed"),
            ("14:00", [], "active"),
            ("14:30", [], "active"),
            ("15:00", [CLEAR], "idle"),
            ("15:01", [], "idle"),
        ]
        for hhmm, expected, status in steps:
            with self.subTest(time=hhmm):
                app.ops.clear()
                app.at(hhmm)
                app.reconcile()
                self.assertEqual(app.ops, expected)
                self.assertEqual(app.status()[1], status)
                self.assertEqual(app.active(), "on" if status == "active" else "off")

        boosts = app.services.boosts()
        self.assertEqual(len(boosts), 1)
        self.assertEqual(boosts[0]["target"], {"entity_id": WATER_HEATER})
        self.assertEqual(boosts[0]["data"], {"hours": 1, "minutes": 0, "target_temperature": 50.0})
        self.assertEqual(app.services.others(), [("switch", "turn_on"), ("script", "dishwasher_eco"),
                                                 ("switch", "turn_off")])
        self.assertEqual(app.services.titles(), ["Happy Hour started", "Happy Hour over"])
        started = app.services.notifications()[0]
        self.assertEqual(started["service"], "mobile_app_phone")
        self.assertEqual(started["data"]["message"],
                         "Free electricity until 15:00. Battery charging to 100%, hot water boosting "
                         "to 50°C, 2 other actions.")

    def test_new_booking_is_announced(self):
        app = happy_app()
        app.reconcile()
        self.assertEqual(app.services.titles(), [])  # known at startup
        app.set_events(booked=[booked("14:00", "15:00"), booked("17:00", "18:00", eid="2")])
        app.reconcile()
        app.reconcile()
        self.assertEqual(app.services.titles(), ["Happy Hour booked"])
        self.assertTrue(app.services.notifications()[0]["data"]["message"].startswith("Sat 31 Oct 17:00-18:00."))

    def test_late_start_boosts_for_the_rest(self):
        app = happy_app()
        app.at("14:20")
        app.reconcile()
        self.assertEqual(app.services.boosts()[0]["data"]["minutes"], 30)
        self.assertEqual(app.services.boosts()[0]["data"]["hours"], 0)

    def test_boost_rounds_up_a_late_minute(self):
        self.assertEqual(App().boost_minutes({"start": local("14:00"), "end": local("15:00")}, local("14:01")), 60)

    def test_no_boost_in_the_last_minutes(self):
        app = happy_app()
        app.at("14:50")
        app.reconcile()
        self.assertEqual(app.services.boosts(), [])
        self.assertEqual(app.services.titles(), ["Happy Hour started"])

    def test_extended_window_tops_up_the_boost(self):
        app = happy_app()
        app.at("14:00")
        app.reconcile()
        app.set_events(booked=[booked("14:00", "15:00"), booked("15:00", "16:00", eid="2")])
        app.ops.clear()
        app.at("14:10")
        app.reconcile()
        self.assertEqual([b["data"]["hours"] * 60 + b["data"]["minutes"] for b in app.services.boosts()],
                         [60, 105])
        self.assertEqual(app.ops[0][0], ("value", TIME_CID, "14:00-16:00"))
        self.assertEqual(app.services.titles(), ["Happy Hour started"])

    def test_boost_failure_retries(self):
        app = happy_app()
        app.services.fail.add(("octopus_energy", "boost_water_heater"))
        app.at("14:00")
        with self.assertLogs("pyscript.solis_octopus", level="ERROR"):
            app.reconcile()
            app.reconcile()
            app.reconcile()
            app.reconcile()
        self.assertEqual(len(app.services.boosts()), 3)
        self.assertEqual(app.services.titles(), ["Happy Hour started"])

    def test_cancelled_happy_hour_ends(self):
        app = happy_app(ACTIONS)
        app.at("14:00")
        app.reconcile()
        app.set_events(booked=[])
        app.ops.clear()
        app.at("14:10")
        app.reconcile()
        self.assertEqual(app.ops, [CLEAR])
        self.assertEqual(app.services.titles(), ["Happy Hour started", "Happy Hour over"])

    def test_restart_mid_happy_hour_only_resumes_the_battery(self):
        app = App(ACTIONS)
        app.capture_ops()
        app.set_events(booked=[booked("14:00", "15:00")])
        app.at("14:30")
        app.seed(local("14:30"))
        app.reconcile("startup")
        self.assertEqual(app.ops, [ARM])
        self.assertEqual(app.services.calls, [])
        app.at("15:00")
        app.reconcile()
        self.assertEqual(app.services.others(), [("switch", "turn_off")])
        self.assertEqual(app.services.titles(), ["Happy Hour over"])

    def test_startup_clears_leftover_slot(self):
        app = App()
        app.capture_ops()
        app.reconcile("startup")
        self.assertEqual(app.ops, [CLEAR])

    def test_failed_write_is_retried_in_full(self):
        app = happy_app()
        app.capture_ops(ok=False)
        app.at("13:55")
        with self.assertLogs("pyscript.solis_octopus", level="ERROR"):
            app.reconcile()
        self.assertTrue(app.status()[2]["slot_state_unknown"])
        app.capture_ops()
        app.reconcile()
        self.assertEqual(app.ops, [ARM])

    def test_custom_charge_settings(self):
        app = happy_app({"charge_slot": 5, "charge_current": 70, "charge_soc": 90, "prepare_minutes": 15})
        app.at("13:45")
        app.reconcile()
        self.assertEqual(app.ops, [[("value", "5958", "14:00-15:00"), ("value", "5960", "70"),
                                    ("value", "5932", "90"), ("switch", "5920", True)]])


class FeatureToggleTests(unittest.TestCase):
    def test_no_solis_credentials_means_no_battery(self):
        with self.assertLogs("pyscript.solis_octopus", level="ERROR"):
            app = App({"key_id": None, "battery_charge": True})
        self.assertFalse(app.BATTERY_CHARGE)

    def test_battery_off_by_default_without_credentials(self):
        app = App({"key_id": None, "secret": None})
        self.assertFalse(app.BATTERY_CHARGE)
        app.capture_ops()
        app.set_events(booked=[booked("14:00", "15:00")])
        app.seed(local("09:00"))
        app.at("14:00")
        app.reconcile()
        self.assertEqual(app.ops, [])
        self.assertEqual(app.status()[1], "active")
        self.assertEqual(app.services.notifications()[0]["data"]["message"],
                         "Free electricity until 15:00. Hot water boosting to 50°C.")

    def test_hot_water_off(self):
        app = happy_app({"hot_water_boost": False})
        app.at("14:00")
        app.reconcile()
        self.assertEqual(app.services.boosts(), [])

    def test_hot_water_off_without_heat_pump(self):
        app = App({"heat_pump_id": None})
        self.assertFalse(app.HOT_WATER_BOOST)
        self.assertEqual(App({"heat_pump_id": None, "water_heater_entity": "water_heater.tank"}).WATER_HEATER,
                         "water_heater.tank")

    def test_invalid_action_is_dropped(self):
        with self.assertLogs("pyscript.solis_octopus", level="ERROR"):
            app = App({"start_actions": [{"service": "nodomain"}, {"data": {}}, "light.turn_on"]})
        self.assertEqual([(a["domain"], a["service"]) for a in app.START_ACTIONS], [("light", "turn_on")])

    def test_several_notify_services_and_data(self):
        app = happy_app({"notify_service": ["notify.mobile_app_a", "mobile_app_b"], "notify_end": False,
                         "notify_data": {"push": {"interruption-level": "time-sensitive"}}})
        app.at("14:00")
        app.reconcile()
        app.at("15:00")
        app.reconcile()
        calls = app.services.notifications()
        self.assertEqual([c["service"] for c in calls], ["mobile_app_a", "mobile_app_b"])
        self.assertEqual(calls[0]["data"]["data"], {"push": {"interruption-level": "time-sensitive"}})


class DiagnosticsTests(unittest.TestCase):
    def test_nothing_is_changed(self):
        app = App(dict(ACTIONS, diagnostics_only=True))
        app.set_events(booked=[booked("14:00", "15:00")])
        app.seed(local("09:00"))
        app.at("14:00")
        with self.assertLogs("pyscript.solis_octopus", level="WARNING") as logs:
            app.reconcile()
        self.assertTrue(any("would write" in line for line in logs.output))
        self.assertTrue(any("would call octopus_energy.boost_water_heater" in line for line in logs.output))
        self.assertEqual(app.cloud.calls, [])
        self.assertEqual(app.services.boosts(), [])
        self.assertEqual(app.services.others(), [])
        self.assertIn("diagnostics only", app.services.notifications()[0]["data"]["message"])
        self.assertEqual(app.status()[1], "active")


if __name__ == "__main__":
    unittest.main()
