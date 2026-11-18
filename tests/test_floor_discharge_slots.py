"""The discharge slots the app doesn't use keep their cut-off SOC at floor_soc."""
import unittest

from tests.harness import App, discharge_slot

SLOT4_SOC = discharge_slot(4)[2]
OTHER_SOCS = [discharge_slot(n)[2] for n in range(2, 7)]
EXPORT_SOC = discharge_slot(1)[2]


def floor_app(enabled=True):
    app = App({"slots": {"floor_other_discharge_slots": enabled}})
    app.capture_ops()
    app.known()
    app.set_events()
    app.at("12:00")
    return app


def soc_writes(app):
    return [op for batch in app.ops for op in batch if op[0] == "value" and op[1] in OTHER_SOCS + [EXPORT_SOC]]


class FloorOtherDischargeSlotsTests(unittest.TestCase):
    def test_lowers_a_stale_cut_off_to_the_floor(self):
        app = floor_app()
        for soc in OTHER_SOCS:
            app.states[soc] = ("10.0", {})
        app.states[SLOT4_SOC] = ("44.0", {})
        app.reconcile()
        self.assertEqual(soc_writes(app), [("value", SLOT4_SOC, "10")])

    def test_leaves_slots_already_at_the_floor(self):
        app = floor_app()
        for soc in OTHER_SOCS:
            app.states[soc] = ("10.0", {})
        app.reconcile()
        self.assertEqual(soc_writes(app), [])

    def test_never_touches_the_export_slot(self):
        app = floor_app()
        app.reconcile()
        self.assertNotIn(EXPORT_SOC, [op[1] for op in soc_writes(app)])
        self.assertEqual(len(soc_writes(app)), 5)

    def test_can_be_turned_off(self):
        app = floor_app(False)
        app.states[SLOT4_SOC] = ("44.0", {})
        app.reconcile()
        self.assertEqual(soc_writes(app), [])
