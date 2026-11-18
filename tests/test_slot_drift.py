"""The app's slots are checked against the inverter, and rewritten when something else changed them."""
import unittest

from tests.harness import App, EXPORT_CURRENT, EXPORT_SOC, EXPORT_SWITCH


def exporting_app():
    """An Axle Self-Dispatch export 17:30-18:30, armed and running at 17:35."""
    app = App({"axle_events": True, "axle": {"control": True, "pre_charge": False}})
    app.capture_ops()
    app.known()
    app.g["_slots"]["core"].update({"window": "23:30-05:30", "enabled": True})
    app.set_events()
    app.set_axle("export", "17:30", "18:30")
    app.set_soc(60)
    app.at("17:28")
    app.reconcile()
    app.mirror_slots = False  # from here on the fake inverter only changes when written to
    app.ops.clear()
    return app


def written(app, entity_id):
    return [op[2] for batch in app.ops for op in batch if op[1] == entity_id]


class SlotDriftTests(unittest.TestCase):
    def test_export_is_armed(self):
        app = exporting_app()
        self.assertTrue(app.g["_slots"]["export"]["enabled"])
        self.assertEqual(app.states[EXPORT_SWITCH][0], "on")

    def test_cut_off_soc_changed_on_the_inverter_is_rewritten(self):
        app = exporting_app()
        ours = app.g["_slots"]["export"]["soc"]
        app.states[EXPORT_SOC] = ("44.0", {})
        app.at("17:35")
        app.reconcile()
        self.assertEqual(written(app, EXPORT_SOC), [str(ours)])
        self.assertEqual(app.states[EXPORT_SOC][0], str(float(ours)))
        self.assertIn("export slot changed on the inverter", app.status()[2]["decision"])

    def test_slot_switched_off_on_the_inverter_is_switched_back_on(self):
        app = exporting_app()
        app.states[EXPORT_SWITCH] = ("off", {})
        app.at("17:35")
        app.reconcile()
        self.assertEqual(written(app, EXPORT_SWITCH), [True])

    def test_current_changed_on_the_inverter_is_rewritten(self):
        app = exporting_app()
        app.states[EXPORT_CURRENT] = ("20.0", {})
        app.at("17:35")
        app.reconcile()
        self.assertTrue(written(app, EXPORT_CURRENT))

    def test_no_rewrite_while_the_inverter_matches(self):
        app = exporting_app()
        app.at("17:35")
        app.reconcile()
        self.assertEqual(written(app, EXPORT_SOC), [])
        self.assertEqual(written(app, EXPORT_SWITCH), [])

    def test_waits_for_solis_modbus_to_catch_up_after_a_write(self):
        app = exporting_app()
        app.states[EXPORT_SOC] = ("44.0", {})
        app.at("17:29")  # 60 s after the write: Solis Modbus may not show it yet
        app.reconcile()
        self.assertEqual(written(app, EXPORT_SOC), [])


class SlotDriftLimitTests(unittest.TestCase):
    def test_gives_up_on_a_value_the_inverter_wont_take(self):
        app = exporting_app()
        for minute in range(35, 50, 2):
            app.states[EXPORT_SOC] = ("44.0", {})  # the inverter keeps putting it back
            app.at(f"17:{minute}")
            app.reconcile()
        ours = str(app.g["_slots"]["export"]["soc"])
        self.assertEqual(written(app, EXPORT_SOC), [ours] * 3)
        self.assertIn("won't take our value", app.status()[2]["decision"])
