import unittest

from tests.hh_harness import App, EVENTS, available, booked, local


class TriggerTests(unittest.TestCase):
    def test_registered_triggers_and_services(self):
        app = App()
        kinds = {(t[0], t[1]) for t in app.triggers}
        self.assertIn(("time_trigger", ("startup",)), kinds)
        self.assertIn(("time_trigger", ("cron(* * * * *)",)), kinds)
        self.assertIn(("time_trigger", ("cron(7 * * * *)",)), kinds)
        self.assertIn(("event_trigger", ("state_changed", f"entity_id == '{EVENTS}'")), kinds)
        self.assertEqual(set(app.service_funcs), {
            "solis_octopus_reconcile", "solis_octopus_clear", "solis_octopus_preview"})

    def test_no_state_trigger_on_event_domain(self):
        # pyscript reads `event` in a state_trigger expression as its own built-in.
        for kind, args, _ in App().triggers:
            if kind == "state_trigger":
                for arg in args:
                    self.assertFalse(str(arg).startswith("event."), arg)

    def test_nothing_happens_before_startup(self):
        app = App(started=False)
        app.capture_ops()
        app.set_events(booked=[booked("14:00", "15:00")])
        app.at("14:00")
        app.reconcile()
        self.assertEqual(app.ops, [])
        self.assertIsNone(app.status())

    def test_startup_waits_seeds_joins_then_reconciles(self):
        app = App({"auto_join": True}, started=False)
        app.capture_ops()
        app.g["log_storage_mode"] = _noop
        app.set_tokens(1)
        app.set_events(booked=[booked("14:00", "15:00")], available=[available("17:00", "18:00", code="HH9")])
        app.run(app.solis_octopus_startup())
        self.assertIn(60, app.asyncio.sleeps)
        self.assertEqual(app.services.hh_joins(), ["HH9"])
        # The 14:00 one was already known; the one we just booked is announced.
        self.assertEqual(app.services.titles(), ["Happy Hour booked"])
        self.assertIn("17:00-18:00", app.services.notifications()[0]["data"]["message"])
        self.assertEqual(app.status()[1], "scheduled")
        self.assertEqual(len(app.ops), 1)  # clears the leftover slot

    def test_events_change_replans(self):
        app = App()
        app.capture_ops()
        app.g["_slots"]["happy_hour"]["unknown"] = False
        app.set_events(booked=[booked("14:00", "15:00")])
        app.at("13:58")
        app.run(app.solis_octopus_happy_hour_changed(entity_id=EVENTS, old_state=None, new_state=None))
        self.assertEqual(len(app.ops), 1)

    def test_preview(self):
        app = App()
        app.set_events(booked=[booked("14:00", "15:00")], available=[available("17:00", "18:00")])
        with self.assertLogs("pyscript.solis_octopus", level="INFO") as logs:
            app.run(app.solis_octopus_preview())
        text = "\n".join(logs.output)
        self.assertIn("Sat 31 Oct 14:00-15:00", text)
        self.assertIn("HH301", text)
        self.assertIn("60 min at 50°C", text)


async def _noop():
    return None


if __name__ == "__main__":
    unittest.main()
