"""configuration.example.yaml must load cleanly with every feature on."""
import unittest
from pathlib import Path

from tests.harness import App

try:
    import yaml
except ImportError:  # pragma: no cover - CI installs PyYAML
    yaml = None

EXAMPLE = Path(__file__).resolve().parents[1] / "configuration.example.yaml"


def load_example():
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_constructor("!secret", lambda loader, node: f"secret-{loader.construct_scalar(node)}")
    return yaml.load(EXAMPLE.read_text(), Loader=Loader)["pyscript"]["apps"]["solis_octopus"]


@unittest.skipIf(yaml is None, "PyYAML not installed")
class ExampleConfigTests(unittest.TestCase):
    def test_every_feature_on_and_no_slot_problems(self):
        app = App(raw=load_example())
        self.assertEqual(app.SLOT_PROBLEMS, [])
        self.assertTrue(app.INVERTER_CONTROL)
        for flag in ("EV_ENABLED", "HH_ENABLED", "PD_ENABLED", "PEAK_EXPORT", "AXLE_EVENTS",
                     "HH_BATTERY_CHARGE", "HOT_WATER_BOOST", "MANAGE_CORE_SLOT", "DIAGNOSTICS_ONLY"):
            self.assertTrue(app.g[flag], flag)
        self.assertEqual(app.active_slot_keys(), ["core", "ev", "happy_hour", "export"])
        self.assertEqual(app.BATTERY_CAPACITY_KWH, 16.1)
        self.assertEqual(app.FLOOR_SOC, 10)
        self.assertEqual(app.PEAK_WINDOW, "16:00-19:00")

    def test_reconciles_in_diagnostics_mode(self):
        app = App(raw=load_example())
        app.reconcile("startup")
        self.assertEqual(app.cloud.calls, [])  # diagnostics: nothing sent
        self.assertIsNotNone(app.overview())


if __name__ == "__main__":
    unittest.main()
