"""Happy hours read from the Octoplus power up calendar."""
import unittest
from datetime import timezone

from tests.hh_harness import App, CURRENT_CID, SOC_CID, SWITCH_CID, TIME_CID, booked, local

CALENDAR = "calendar.octopus_energy_a_c6fd0e83_octoplus_power_up"
EVENTS = "event.octopus_energy_a_c6fd0e83_octoplus_power_up_events"
TOKENS = "sensor.octopus_energy_a_c6fd0e83_octoplus_weekend_happy_hours"

ARM = [("value", TIME_CID, "14:00-15:00"), ("value", CURRENT_CID, "100"),
       ("value", SOC_CID, "100"), ("switch", SWITCH_CID, True)]
CLEAR = [("switch", SWITCH_CID, False), ("value", TIME_CID, "00:00-00:00")]


def attr_time(hhmm, day=31):
    """How HA shows a calendar event's time in its attributes: local, no offset."""
    return local(hhmm, day).strftime("%Y-%m-%d %H:%M:%S")


def calendar_app(extra=None):
    cfg = {"power_up_calendar": CALENDAR, "power_up_events_entity": None}
    cfg.update(extra or {})
    app = App(cfg)
    app.capture_ops()
    app.g["_slots"]["happy_hour"]["unknown"] = False
    return app


def set_calendar(app, start=None, end=None, on=False, day=31):
    attrs = {"friendly_name": "Octoplus Power Up (A-C6FD0E83)"}
    if start:
        attrs.update({"message": "Octopus Energy Power Up", "all_day": False,
                      "start_time": attr_time(start, day), "end_time": attr_time(end, day)})
    app.states[CALENDAR] = ("on" if on else "off", attrs)


def get_events_response(*spans, day=31):
    events = [{"start": local(s, day).isoformat(), "end": local(e, day).isoformat(),
               "summary": "Octopus Energy Power Up"} for s, e in spans]
    return {CALENDAR: {"events": events}}


class CalendarConfigTests(unittest.TestCase):
    def test_entities_derived_from_the_calendar(self):
        app = App({"power_up_calendar": CALENDAR, "power_up_events_entity": None})
        self.assertEqual(app.HH_CALENDAR, CALENDAR)
        self.assertEqual(app.HH_EVENTS_ENTITY, EVENTS)
        self.assertEqual(app.HH_TOKENS_ENTITY, TOKENS)

    def test_calendar_derived_from_the_events_entity(self):
        app = App({"power_up_events_entity": EVENTS})
        self.assertEqual(app.HH_CALENDAR, CALENDAR)

    def test_calendar_alone_is_ok(self):
        app = calendar_app()
        set_calendar(app)
        self.assertEqual(app.hh_events_status(), "ok")

    def test_neither_exists_is_missing(self):
        app = calendar_app()
        self.assertEqual(app.hh_events_status(), "missing")
        self.assertIn(CALENDAR, app.hh_events_warning("missing"))


class CalendarSessionTests(unittest.TestCase):
    def test_attributes_alone_drive_the_happy_hour(self):
        app = calendar_app()
        set_calendar(app, "14:00", "15:00")
        app.services.fail.add(("calendar", "get_events"))
        steps = [("12:00", [], "scheduled"), ("13:55", [ARM], "armed"),
                 ("14:00", [], "active"), ("15:00", [CLEAR], "idle")]
        with self.assertLogs("pyscript.solis_octopus", level="WARNING"):
            for hhmm, expected, status in steps:
                with self.subTest(time=hhmm):
                    app.ops.clear()
                    app.at(hhmm)
                    if hhmm == "14:00":
                        set_calendar(app, "14:00", "15:00", on=True)
                    if hhmm == "15:00":
                        set_calendar(app)
                    app.reconcile()
                    self.assertEqual(app.ops, expected)
                    self.assertEqual(app.status()[1], status)
        self.assertEqual(app.services.boosts()[0]["data"]["hours"], 1)
        self.assertEqual(app.services.titles(), ["Happy Hour booked", "Happy Hour started", "Happy Hour over"])

    def test_attribute_times_are_local(self):
        app = calendar_app()
        set_calendar(app, "14:00", "15:00", day=6)  # BST
        (s,) = app.hh_calendar_sessions()
        self.assertEqual(s["start"].astimezone(timezone.utc).hour, 13)

    def test_get_events_lists_later_happy_hours(self):
        app = calendar_app()
        set_calendar(app, "14:00", "15:00")
        app.services.responses[("calendar", "get_events")] = get_events_response(
            ("14:00", "15:00"), ("17:00", "18:00"))
        app.reconcile()
        windows = [app.fmt_window(w["start"], w["end"]) for w in app.happy_hours()]
        self.assertEqual(windows, ["14:00-15:00", "17:00-18:00"])
        self.assertEqual(app.happy_hours()[0]["sessions"], 1)  # same session from both: not doubled
        (call,) = [c for c in app.services.calls if c["domain"] == "calendar"]
        self.assertEqual(call["target"], {"entity_id": CALENDAR})

    def test_get_events_is_throttled_until_the_calendar_changes(self):
        app = calendar_app()
        set_calendar(app)
        app.services.responses[("calendar", "get_events")] = get_events_response()
        for hhmm in ("12:00", "12:01", "12:04", "12:05"):
            app.at(hhmm)
            app.reconcile()
        self.assertEqual(len([c for c in app.services.calls if c["domain"] == "calendar"]), 2)
        app._hh_cal["dirty"] = True  # what the calendar's state_changed trigger does
        app.reconcile()
        self.assertEqual(len([c for c in app.services.calls if c["domain"] == "calendar"]), 3)

    def test_cancelled_happy_hour_drops_out(self):
        app = calendar_app()
        set_calendar(app, "14:00", "15:00")
        app.services.responses[("calendar", "get_events")] = get_events_response(("14:00", "15:00"))
        app.at("13:55")
        app.reconcile()
        set_calendar(app)
        app.services.responses[("calendar", "get_events")] = get_events_response()
        app._hh_cal["dirty"] = True
        app.ops.clear()
        app.at("13:57")
        app.reconcile()
        self.assertEqual(app.ops, [CLEAR])

    def test_events_entity_and_calendar_together(self):
        app = App({"power_up_calendar": CALENDAR})
        app.capture_ops()
        app.set_events(booked=[booked("14:00", "15:00")])
        set_calendar(app, "14:00", "15:00")
        self.assertEqual(len(app.hh_sessions()[0]), 1)

    def test_auto_join_needs_the_events_entity(self):
        app = calendar_app({"auto_join": True})
        set_calendar(app)
        app.run(app.hh_auto_join())
        self.assertEqual(app.services.hh_joins(), [])

    def test_trigger_registered(self):
        app = calendar_app()
        names = [t[2] for t in app.triggers]
        self.assertIn("solis_octopus_happy_hour_calendar_changed", names)


if __name__ == "__main__":
    unittest.main()
