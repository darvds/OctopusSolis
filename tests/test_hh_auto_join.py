import unittest
from datetime import timedelta

from tests.hh_harness import App, EVENTS, available, booked, local


def join_app(config=None):
    cfg = {"auto_join": True}
    cfg.update(config or {})
    app = App(cfg)
    app.set_tokens(2)
    return app


class AutoJoinTests(unittest.TestCase):
    def test_off_by_default(self):
        app = App()
        app.set_events(available=[available("14:00", "15:00")])
        app.run(app.auto_join())
        self.assertEqual(app.services.calls, [])

    def test_books_earliest_once(self):
        app = join_app()
        app.set_events(available=[available("16:00", "17:00", eid="2", code="HH2"),
                                  available("14:00", "15:00", eid="1", code="HH1")])
        app.run(app.auto_join())
        self.assertEqual(app.services.hh_joins(), ["HH1"])
        self.assertEqual(app.services.calls[0]["target"], {"entity_id": EVENTS})
        # Counts as booked before the integration refreshes.
        self.assertEqual(app.happy_hours()[0]["start"], local("14:00"))
        app.run(app.auto_join())
        self.assertEqual(app.services.hh_joins(), ["HH1", "HH2"])

    def test_latest(self):
        app = join_app({"join_selection": "latest"})
        app.set_events(available=[available("14:00", "15:00", eid="1", code="HH1"),
                                  available("16:00", "17:00", eid="2", code="HH2")])
        app.run(app.auto_join())
        self.assertEqual(app.services.hh_joins(), ["HH2"])

    def test_no_tokens(self):
        app = join_app()
        app.set_tokens(0)
        app.set_events(available=[available("14:00", "15:00")])
        app.run(app.auto_join())
        self.assertEqual(app.services.hh_joins(), [])

    def test_skips_full_started_and_booked(self):
        app = join_app()
        app.at("14:30")
        app.set_events(booked=[booked("18:00", "19:00", eid="3")],
                       available=[available("14:00", "15:00", eid="1"),
                                  available("16:00", "17:00", eid="2", availability="FULL"),
                                  available("18:00", "19:00", eid="3")])
        app.run(app.auto_join())
        self.assertEqual(app.services.hh_joins(), [])

    def test_failure_backs_off_an_hour(self):
        app = join_app()
        app.set_events(available=[available("14:00", "15:00", code="HH1")])
        app.services.fail.add(("octopus_energy", "join_octoplus_weekend_happy_hour_event"))
        with self.assertLogs("pyscript.solis_octopus", level="ERROR"):
            app.run(app.auto_join())
        app.run(app.auto_join())
        self.assertEqual(app.services.hh_joins(), ["HH1"])
        self.assertEqual(app.happy_hours(), [])
        app.services.fail.clear()
        app.g["_hh_joined"]["failed_at"]["301"] -= timedelta(hours=2)
        app.run(app.auto_join())
        self.assertEqual(app.services.hh_joins(), ["HH1", "HH1"])
        self.assertEqual(len(app.happy_hours()), 1)


if __name__ == "__main__":
    unittest.main()
