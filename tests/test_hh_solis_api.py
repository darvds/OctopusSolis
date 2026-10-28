import unittest

from tests.hh_harness import App, CURRENT_CID, SOC_CID, SWITCH_CID, TIME_CID


class SolisWriteTests(unittest.TestCase):
    def app(self):
        return App({"sync_inverter_time": False})

    def test_arm_writes_values_then_switch(self):
        app = self.app()
        ok = app.run(app.solis_apply([("value", TIME_CID, "14:00-15:00"), ("value", CURRENT_CID, "100"),
                                      ("value", SOC_CID, "100"), ("switch", SWITCH_CID, True)]))
        self.assertTrue(ok)
        self.assertEqual(app.cloud.control_writes(), [
            (TIME_CID, "14:00-15:00", None), (CURRENT_CID, "100", None), (SOC_CID, "100", None),
            (SWITCH_CID, "1", "0")])

    def test_yuanzhi_has_other_slot_states(self):
        app = self.app()
        app.cloud.values["5916"] = "1"   # charge slot 1 (core window) on
        app.cloud.values["5922"] = "1"   # discharge slot 1 on
        app.run(app.solis_apply([("switch", SWITCH_CID, True)]))
        self.assertEqual(app.cloud.control_writes(), [(SWITCH_CID, "1", str(1 | 1 << 6))])

    def test_switch_not_written_if_a_value_fails(self):
        app = self.app()
        app.cloud.fail_cids.add(CURRENT_CID)
        with self.assertLogs("pyscript.solis_octopus", level="ERROR"):
            ok = app.run(app.solis_apply([("value", TIME_CID, "14:00-15:00"), ("value", CURRENT_CID, "100"),
                                          ("switch", SWITCH_CID, True)]))
        self.assertFalse(ok)
        self.assertNotIn(SWITCH_CID, [w[0] for w in app.cloud.control_writes()])

    def test_clear_service(self):
        app = self.app()
        app.cloud.values[SWITCH_CID] = "1"
        app.run(app.solis_octopus_clear())
        self.assertEqual([w[0] for w in app.cloud.control_writes()], [SWITCH_CID, TIME_CID])
        self.assertFalse(app.g["_slots"]["happy_hour"]["unknown"])

    def test_time_sync_once_a_day(self):
        app = App()
        app.run(app.solis_apply([("value", TIME_CID, "00:00-00:00")]))
        app.run(app.solis_apply([("value", TIME_CID, "00:00-00:00")]))
        syncs = [c for c in app.cloud.calls if c["path"] == "/v2/api/control" and c["body"].get("cid") == "56"]
        self.assertEqual(len(syncs), 1)

    def test_request_is_signed(self):
        app = self.app()
        app.run(app.solis_apply([("value", TIME_CID, "00:00-00:00")]))
        headers = app.cloud.calls[0]["headers"]
        self.assertTrue(headers["Authorization"].startswith("API test-key:"))
        self.assertIn("Content-MD5", headers)


if __name__ == "__main__":
    unittest.main()
