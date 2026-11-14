import unittest

from tests.harness import PREFIX, FakeModbus, charge_slot
from tests.hh_harness import App, CURRENT, CUTOFF_SOC, SWITCH, TIME


class SolisModbusWriteTests(unittest.TestCase):
    def test_arm_writes_values_then_switch(self):
        app = App()
        ok = app.run(app.solis_apply([("time", TIME, "14:00-15:00"), ("value", CURRENT, "100"),
                                      ("value", CUTOFF_SOC, "100"), ("switch", SWITCH, True)]))
        self.assertTrue(ok)
        self.assertEqual(app.modbus.writes(), [
            (TIME[0], "14:00"), (TIME[1], "15:00"), (CURRENT, "100"), (CUTOFF_SOC, "100"), (SWITCH, "on")])
        self.assertEqual(app.states[TIME[0]][0], "14:00:00")

    def test_switch_already_on_is_not_rewritten(self):
        app = App()
        app.modbus.set(SWITCH, "on")
        self.assertTrue(app.run(app.solis_apply([("switch", SWITCH, True)])))
        self.assertEqual(app.modbus.calls, [])

    def test_switch_not_written_if_a_value_fails(self):
        app = App()
        app.modbus.fail.add(CURRENT)
        with self.assertLogs("pyscript.solis_octopus", level="ERROR"):
            ok = app.run(app.solis_apply([("time", TIME, "14:00-15:00"), ("value", CURRENT, "100"),
                                          ("switch", SWITCH, True)]))
        self.assertFalse(ok)
        self.assertNotIn(SWITCH, app.modbus.written())

    def test_failed_write_is_retried(self):
        app = App({"control_retries": 3})
        calls = {"n": 0}
        real = app.modbus.call

        def flaky(domain, service, data):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("modbus timeout")
            return real(domain, service, data)

        app.modbus.call = flaky
        self.assertTrue(app.run(app.solis_apply([("value", CURRENT, "50")])))
        self.assertEqual(app.modbus.writes(), [(CURRENT, "50")])

    def test_clear_service(self):
        app = App()
        app.modbus.set(SWITCH, "on")
        app.run(app.solis_octopus_clear())
        self.assertEqual(app.modbus.written(), [SWITCH, TIME[0], TIME[1]])
        self.assertFalse(app.g["_slots"]["happy_hour"]["unknown"])

    def test_readback_warns_on_mismatch(self):
        app = App({"verify_readback": True})
        real = app.modbus.call

        def ignored(domain, service, data):
            real(domain, service, data)
            app.modbus.set(data["entity_id"], "0.0")   # the inverter didn't take it

        app.modbus.call = ignored
        with self.assertLogs("pyscript.solis_octopus", level="WARNING") as logs:
            self.assertTrue(app.run(app.solis_apply([("value", CURRENT, "100")])))
        self.assertTrue(any("Readback" in line for line in logs.output))


class PrefixTests(unittest.TestCase):
    def test_prefix_found_from_entities(self):
        app = App()
        self.assertEqual(app.inverter_prefix(), PREFIX)

    def test_other_device_name_found(self):
        app = App()
        for entity in list(app.states):
            if entity.split(".")[0] in ("time", "number", "switch") and "_grid_time_of_use_" in entity:
                del app.states[entity]
        times, _, _, _ = charge_slot(1, prefix="solis_rhi_5k_48es_5g")
        app.states[times[0]] = ("00:00:00", {})
        self.assertEqual(app.inverter_prefix(), "solis_rhi_5k_48es_5g")
        self.assertEqual(app.slot_entities("happy_hour"), charge_slot(3, prefix="solis_rhi_5k_48es_5g"))

    def test_prefix_from_config(self):
        app = App({"inverter_prefix": "garage_inverter"})
        FakeModbus(app.states, prefix="garage_inverter")   # a second inverter
        self.assertEqual(app.slot_entities("happy_hour"), charge_slot(3, prefix="garage_inverter"))

    def test_two_inverters_need_a_prefix(self):
        app = App()
        app.states["time.other_" + "grid_time_of_use_charge_start_slot_1"] = ("00:00:00", {})
        with self.assertLogs("pyscript.solis_octopus", level="ERROR"):
            app.inverter_prefix()


class OlderEntityNameTests(unittest.TestCase):
    """Solis Modbus before 2025-12 named the switches "Solis Grid Time of Use ...", and HA keeps old IDs."""

    def rename_switches(self, app, new_prefix):
        for n in range(1, 7):
            for kind in ("charging", "discharge"):
                old = f"switch.{PREFIX}_grid_time_of_use_{kind}_period_{n}"
                app.modbus.entities.discard(old)
                del app.states[old]
                new = f"switch.{new_prefix}_grid_time_of_use_{kind}_period_{n}"
                app.states[new] = ("off", {})
                app.modbus.entities.add(new)

    def test_switches_with_the_old_name_are_found(self):
        app = App()
        self.rename_switches(app, f"{PREFIX}_solis")
        _, _, _, switch = app.slot_entities("happy_hour")
        self.assertEqual(switch, f"switch.{PREFIX}_solis_grid_time_of_use_charging_period_3")
        ok = app.run(app.solis_apply([("time", TIME, "14:00-15:00"), ("switch", switch, True)]))
        self.assertTrue(ok)
        self.assertEqual(app.states[switch][0], "on")

    def test_switches_without_the_device_prefix_are_found(self):
        app = App()
        self.rename_switches(app, "solis")
        app.g["INVERTER_PREFIX"] = "solis_s6_eh1p"
        _, _, _, switch = app.slot_entities("export")
        self.assertEqual(switch, "switch.solis_grid_time_of_use_discharge_period_1")

    def test_another_inverters_switch_is_not_used(self):
        app = App({"inverter_prefix": "garage"})
        app.states["switch.house_grid_time_of_use_charging_period_3"] = ("off", {})
        app.states["switch.garage_x_grid_time_of_use_charging_period_3"] = ("off", {})
        _, _, _, switch = app.slot_entities("happy_hour")
        self.assertEqual(switch, "switch.garage_x_grid_time_of_use_charging_period_3")


if __name__ == "__main__":
    unittest.main()
