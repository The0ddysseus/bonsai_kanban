"""
Tests for the calendar reading (ics_feed) and the meeting alerter.
Run from the project folder:  python -m unittest discover tests
"""

import sys
import tempfile
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import alerter
import alerts_config
import alerts_message
import ics_feed
from alerts_store import AlertStore

BRISBANE = ZoneInfo("Australia/Brisbane")


def calendar(*events: str, timezones: str = "") -> bytes:
    body = "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:test\n" + timezones + "".join(events) + "END:VCALENDAR\n"
    return body.replace("\n", "\r\n").encode()


def vevent(uid: str, start: str, end: str = "", *, lines: tuple[str, ...] = (), **extra: str) -> str:
    """extra: NAME=";params:value" or ":value" (underscores in NAME become
    hyphens); lines: whole lines, for properties given more than once.
    """
    out = ["BEGIN:VEVENT", f"UID:{uid}", start if ":" in start else f"DTSTART:{start}"]
    if end:
        out.append(end if ":" in end else f"DTEND:{end}")
    out += [f"{key.replace('_', '-')}{value}" for key, value in extra.items()]
    return "\n".join(out + list(lines) + ["END:VEVENT", ""])


# Outlook describes Windows-named zones itself. Sydney's, as an old Outlook
# might have it: DST ending on the last Sunday of March (it's been the first
# Sunday in April since 2008). The real zone must win.
SYDNEY_VTIMEZONE = """BEGIN:VTIMEZONE
TZID:AUS Eastern Standard Time
BEGIN:STANDARD
DTSTART:16010101T030000
TZOFFSETFROM:+1100
TZOFFSETTO:+1000
RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=3
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:16010101T020000
TZOFFSETFROM:+1000
TZOFFSETTO:+1100
RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=10
END:DAYLIGHT
END:VTIMEZONE
"""
BRISBANE_VTIMEZONE = """BEGIN:VTIMEZONE
TZID:E. Australia Standard Time
BEGIN:STANDARD
DTSTART:16010101T000000
TZOFFSETFROM:+1000
TZOFFSETTO:+1000
END:STANDARD
END:VTIMEZONE
"""


def parse_all(data: bytes, first: date, last: date) -> list[ics_feed.Occurrence]:
    return ics_feed.occurrences(ics_feed.read_calendar(data), first, last, BRISBANE)


class TimeZoneTests(unittest.TestCase):
    def test_windows_zone_with_outdated_vtimezone_uses_real_rules(self):
        # 2 April 2026, Sydney is still on daylight time (+11) until the 5th;
        # the outdated VTIMEZONE would say +10.
        data = calendar(
            vevent("a", "DTSTART;TZID=AUS Eastern Standard Time:20260402T100000", SUMMARY=":Sydney"),
            timezones=SYDNEY_VTIMEZONE,
        )
        (event,) = parse_all(data, date(2026, 4, 1), date(2026, 4, 3))
        self.assertEqual(event.start.astimezone(timezone.utc), datetime(2026, 4, 1, 23, 0, tzinfo=timezone.utc))
        self.assertEqual(event.start.tzinfo, BRISBANE)
        self.assertEqual(event.start.hour, 9)  # 10 am Sydney is 9 am Brisbane then

    def test_windows_zone_without_vtimezone(self):
        data = calendar(vevent("a", "DTSTART;TZID=E. Australia Standard Time:20261021T103000"))
        (event,) = parse_all(data, date(2026, 10, 21), date(2026, 10, 21))
        self.assertEqual(event.start, datetime(2026, 10, 21, 10, 30, tzinfo=BRISBANE))

    def test_utc_and_floating_times(self):
        data = calendar(vevent("utc", "20261021T003000Z"), vevent("floating", "20261021T140000"))
        events = {e.uid: e for e in parse_all(data, date(2026, 10, 21), date(2026, 10, 21))}
        self.assertEqual(events["utc"].start, datetime(2026, 10, 21, 10, 30, tzinfo=BRISBANE))
        self.assertEqual(events["floating"].start, datetime(2026, 10, 21, 14, 0, tzinfo=BRISBANE))

    def test_all_day(self):
        data = calendar(vevent("a", "DTSTART;VALUE=DATE:20261021", "DTEND;VALUE=DATE:20261022"))
        (event,) = parse_all(data, date(2026, 10, 21), date(2026, 10, 21))
        self.assertTrue(event.all_day)


class RecurrenceTests(unittest.TestCase):
    def setUp(self):
        tz = "TZID=E. Australia Standard Time"
        self.data = calendar(
            vevent(
                "weekly",
                f"DTSTART;{tz}:20261005T093000",
                f"DTEND;{tz}:20261005T100000",
                SUMMARY=":Stand-up",
                RRULE=":FREQ=WEEKLY;COUNT=5",
                EXDATE=f";{tz}:20261012T093000",
            ),
            # The 19th moved to 2 pm and renamed; the 26th cancelled.
            vevent(
                "weekly",
                f"DTSTART;{tz}:20261019T140000",
                f"DTEND;{tz}:20261019T143000",
                SUMMARY=":Stand-up (moved)",
                RECURRENCE_ID=f";{tz}:20261019T093000",
            ),
            vevent(
                "weekly",
                f"DTSTART;{tz}:20261026T093000",
                f"DTEND;{tz}:20261026T100000",
                SUMMARY=":Stand-up",
                STATUS=":CANCELLED",
                RECURRENCE_ID=f";{tz}:20261026T093000",
            ),
            timezones=BRISBANE_VTIMEZONE,
        )

    def test_expansion(self):
        events = sorted(parse_all(self.data, date(2026, 10, 1), date(2026, 11, 30)), key=lambda e: e.start)
        got = [(e.start.strftime("%m-%d %H:%M"), e.title, e.cancelled) for e in events]
        self.assertEqual(
            got,
            [
                ("10-05 09:30", "Stand-up", False),
                # 10-12 excluded by EXDATE
                ("10-19 14:00", "Stand-up (moved)", False),
                ("10-26 09:30", "Stand-up", True),
                ("11-02 09:30", "Stand-up", False),
            ],
        )
        self.assertEqual(len({e.key for e in events}), 4)  # one uid, four keys
        self.assertTrue(all(e.recurring for e in events))

    def test_kanban_still_skips_recurring(self):
        self.assertEqual(ics_feed.one_off_events(ics_feed.read_calendar(self.data)), [])


class AttendeeTests(unittest.TestCase):
    def test_attendees_organizer_and_my_response(self):
        data = calendar(
            vevent(
                "a",
                "20261021T003000Z",
                ORGANIZER=';CN="Sarah Chen":mailto:sarah@example.com',
                lines=(
                    "ATTENDEE;CN=Me;PARTSTAT=DECLINED:mailto:Me@Example.com",
                    "ATTENDEE;PARTSTAT=ACCEPTED:mailto:tom.walsh@example.com",
                    "ATTENDEE;CN=Room 3;CUTYPE=ROOM:mailto:room3@example.com",
                ),
            )
        )
        (event,) = parse_all(data, date(2026, 10, 21), date(2026, 10, 21))
        self.assertEqual(event.organizer.display_name, "Sarah Chen")
        self.assertEqual([p.display_name for p in event.attendees], ["Me", "tom.walsh", "Room 3"])
        self.assertEqual(event.response_of("me@example.com"), "DECLINED")
        self.assertEqual(event.response_of("nobody@example.com"), "")
        self.assertTrue(event.attendees[2].is_room)

    def test_no_attendees(self):
        data = calendar(vevent("a", "20261021T003000Z", SUMMARY=":Solo"))
        (event,) = parse_all(data, date(2026, 10, 21), date(2026, 10, 21))
        self.assertEqual((event.attendees, event.organizer), ([], None))

    def test_cancelled_prefix(self):
        data = calendar(vevent("a", "20261021T003000Z", SUMMARY=":Canceled: Budget review"))
        (event,) = parse_all(data, date(2026, 10, 21), date(2026, 10, 21))
        self.assertEqual((event.title, event.cancelled), ("Budget review", True))


def occurrence(title="Budget review", start=None, attendees=(), organizer=None, location="", **extra):
    start = start or datetime(2026, 10, 21, 10, 30, tzinfo=BRISBANE)
    return ics_feed.Occurrence(
        uid=extra.pop("uid", "uid-1"),
        title=title,
        start=start,
        end=start + timedelta(minutes=30),
        location=location,
        description=extra.pop("description", ""),
        status="",
        cancelled=extra.pop("cancelled", False),
        recurring=False,
        organizer=organizer,
        attendees=list(attendees),
    )


class MessageTests(unittest.TestCase):
    def setUp(self):
        self.config = alerts_config.Config(user_name="Derek", user_email="derek@example.com")

    def test_spoken_time(self):
        cases = {
            (10, 30): "ten thirty a.m.",
            (14, 0): "two p.m.",
            (9, 5): "nine oh five a.m.",
            (12, 0): "twelve noon",
            (0, 0): "twelve midnight",
            (12, 45): "twelve forty-five p.m.",
            (0, 15): "twelve fifteen a.m.",
            (23, 59): "eleven fifty-nine p.m.",
        }
        for (hour, minute), words in cases.items():
            self.assertEqual(alerts_message.spoken_time(datetime(2026, 1, 1, hour, minute)), words)

    def test_duration_words(self):
        self.assertEqual(alerts_message.duration_words(15 * 60), "fifteen minutes")
        self.assertEqual(alerts_message.duration_words(14 * 60 + 50), "fifteen minutes")
        self.assertEqual(alerts_message.duration_words(20), "one minute")
        self.assertEqual(alerts_message.duration_words(65 * 60), "one hour and five minutes")

    def test_participants(self):
        phrase = alerts_message.participants_phrase
        self.assertEqual(phrase(["Sarah Chen"]), "Sarah Chen")
        self.assertEqual(phrase(["Sarah Chen", "Tom Walsh"]), "Sarah Chen and Tom Walsh")
        self.assertEqual(phrase(["Sarah Chen", "Tom Walsh", "A", "B", "C", "D"]), "Sarah Chen, Tom Walsh and four others")
        self.assertEqual(phrase(["Sarah Chen", "Tom Walsh", "A"]), "Sarah Chen, Tom Walsh and one other")

    def test_full_message_with_teams(self):
        P = ics_feed.Person
        event = occurrence(
            attendees=[P("Derek", "Derek@example.com"), P("Sarah Chen", "s@example.com"), P("", "tom.walsh@example.com")],
            organizer=P("Sarah Chen", "s@example.com"),
            location="Microsoft Teams Meeting",
        )
        now = event.start - timedelta(minutes=15)
        self.assertEqual(
            alerts_message.spoken_message(event, self.config, now),
            "Hey Derek. Budget review with Sarah Chen and tom.walsh starts at ten thirty a.m., "
            "in fifteen minutes. It's a Teams meeting.",
        )

    def test_no_attendees_with_location(self):
        event = occurrence(location="Board Room")
        now = event.start - timedelta(minutes=15)
        self.assertEqual(
            alerts_message.spoken_message(event, self.config, now),
            "Budget review starts at ten thirty a.m., in fifteen minutes. In Board Room.",
        )

    def test_url_location_not_spoken_and_late_start(self):
        event = occurrence(location="https://example.com.au")
        now = event.start - timedelta(minutes=6, seconds=30)
        self.assertEqual(
            alerts_message.spoken_message(event, self.config, now),
            "Budget review starts at ten thirty a.m., in seven minutes.",
        )

    def test_only_me_uses_no_attendee_template(self):
        event = occurrence(attendees=[ics_feed.Person("Derek", "derek@example.com")])
        self.assertTrue(
            alerts_message.spoken_message(event, self.config, event.start).startswith("Budget review starts")
        )

    def test_pronunciations(self):
        self.config.pronunciations = {"QBR": "Q B R", "Q": "queue"}
        event = occurrence(title="QBR prep with QBRs team")
        text = alerts_message.spoken_message(event, self.config, event.start - timedelta(minutes=15))
        self.assertTrue(text.startswith("Q B R prep with QBRs team"), text)

    def test_bad_template_falls_back(self):
        self.config.spoken_template_no_attendees = "{nonsense} at {spoken_time}"
        event = occurrence()
        with self.assertLogs("bonsai-alerts", "ERROR"):
            text = alerts_message.spoken_message(event, self.config, event.start - timedelta(minutes=15))
        self.assertTrue(text.startswith("Budget review starts at"))

    def test_popup(self):
        P = ics_feed.Person
        event = occurrence(
            title="R&D sync",
            attendees=[P("Sarah <SC>", "s@example.com", "TENTATIVE"), P("Derek", "derek@example.com")],
            organizer=P("Sarah <SC>", "s@example.com"),
            location="Room 3",
        )
        heading, body = alerts_message.popup_message(event, self.config)
        self.assertEqual(heading, "R&D sync")
        self.assertEqual(
            body,
            "10:30 am – 11:00 am\nLocation: Room 3\nOrganizer: Sarah &lt;SC&gt;\nAttendees: Sarah &lt;SC&gt; (tentative)",
        )


class ConfigTests(unittest.TestCase):
    def test_load(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "alerts.toml"
            path.write_text(
                'user_email = "me@example.com"\nlead_minutes = "ten"\nurgency = "critical"\n'
                'play_chime = false\nmystery = 1\n[pronunciations]\nQBR = "Q B R"\n'
            )
            with self.assertLogs("bonsai-alerts", "WARNING"):
                config = alerts_config.load(path)
        self.assertEqual(config.user_email, "me@example.com")
        self.assertEqual(config.lead_minutes, 15)  # wrong type: default kept
        self.assertEqual(config.urgency, "critical")
        self.assertFalse(config.play_chime)
        self.assertEqual(config.pronunciations, {"QBR": "Q B R"})

    def test_example_file_loads_cleanly(self):
        example = Path(__file__).resolve().parent.parent / "alerts.example.toml"
        with self.assertNoLogs("bonsai-alerts", "WARNING"):
            alerts_config.load(example)

    def test_missing_file(self):
        self.assertEqual(alerts_config.load(Path("/nonexistent/alerts.toml")).lead_minutes, 15)


class StoreTests(unittest.TestCase):
    def test_announced_survives_reopen_and_prunes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "alerts.db"
            store = AlertStore(path)
            key = occurrence().key
            store.mark_announced(key, "Budget review")
            store.close()
            store = AlertStore(path)
            self.assertTrue(store.was_announced(key))
            moved = occurrence(start=datetime(2026, 10, 21, 11, 0, tzinfo=BRISBANE)).key
            self.assertFalse(store.was_announced(moved))
            store.prune(now=datetime(2026, 10, 23, 10, 0, tzinfo=BRISBANE))
            self.assertTrue(store.was_announced(key))  # it started 47.5 hours before
            store.prune(now=datetime(2026, 10, 24, 12, 0, tzinfo=BRISBANE))
            self.assertFalse(store.was_announced(key))
            store.close()


class AlerterTests(unittest.TestCase):
    """The alerter's decisions, with the calendar download, clock and
    sounds replaced.
    """

    NOW = datetime(2026, 10, 21, 10, 0, tzinfo=BRISBANE)

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store_path = Path(self.folder.name) / "alerts.db"
        self.config_path = Path(self.folder.name) / "alerts.toml"
        self.config_path.write_text(
            'ics_url = "https://example.com/secret-token/calendar.ics"\n'
            'timezone = "Australia/Brisbane"\nuser_email = "me@example.com"\nplay_speech = false\n'
        )
        self.feed = b""
        self.now = self.NOW
        self.fetches = 0
        self.alerter = self.make_alerter()

    def make_alerter(self):
        def fetch(url):
            self.fetches += 1
            return self.feed

        patches = [
            mock.patch.object(ics_feed, "fetch", side_effect=fetch),
            mock.patch.object(alerter.Alerter, "now", lambda _self: self.now),
            mock.patch.object(alerter, "time", types.SimpleNamespace(monotonic=lambda: self.now.timestamp())),
            mock.patch.object(alerter.Alerter, "sound_alert"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return self.alerter_with_new_store()

    def alerter_with_new_store(self):
        store = AlertStore(self.store_path)
        self.addCleanup(store.close)
        return alerter.Alerter(self.config_path, store)

    def set_feed(self, *events):
        self.feed = calendar(*events)

    def announced_titles(self, a=None):
        a = a or self.alerter
        return [c.args[0].title for c in a.sound_alert.call_args_list]

    def tick(self, a=None, advance=timedelta(0)):
        self.now += advance
        (a or self.alerter)._last_fetch = None  # download every tick, to keep tests simple
        (a or self.alerter).tick()

    def test_announces_at_lead_time_once(self):
        self.set_feed(vevent("a", "20261021T013000Z", SUMMARY=":Budget review"))  # 11:30 Brisbane
        self.tick()  # 10:00, 90 min away
        self.tick(advance=timedelta(minutes=74))  # 11:14, 16 min away: not yet
        self.assertEqual(self.announced_titles(), [])
        self.tick(advance=timedelta(minutes=1))  # 11:15
        self.tick(advance=timedelta(seconds=15))
        self.assertEqual(self.announced_titles(), ["Budget review"])

    def test_late_start_announces_immediately_but_not_started_ones(self):
        self.set_feed(
            vevent("soon", "20261021T000700Z", SUMMARY=":Soon"),  # 10:07
            vevent("started", "20261021T000000Z", SUMMARY=":Started"),  # 10:00, now
            vevent("earlier", "20261020T235000Z", SUMMARY=":Earlier"),  # 9:50
        )
        self.tick()
        self.assertEqual(self.announced_titles(), ["Soon"])

    def test_restart_does_not_repeat(self):
        self.set_feed(vevent("a", "20261021T001000Z", SUMMARY=":Budget review"))
        self.tick()
        self.alerter.sound_alert.reset_mock()
        again = self.alerter_with_new_store()
        self.tick(again)
        self.assertEqual(self.announced_titles(again), [])

    def test_skips_declined_cancelled_all_day(self):
        self.set_feed(
            vevent("d", "20261021T001000Z", SUMMARY=":Declined", ATTENDEE=";PARTSTAT=DECLINED:mailto:ME@example.com"),
            vevent("c", "20261021T001000Z", SUMMARY=":Cancelled", STATUS=":CANCELLED"),
            vevent("x", "DTSTART;VALUE=DATE:20261021", SUMMARY=":All day"),
            vevent("ok", "20261021T001000Z", SUMMARY=":Kept", ATTENDEE=";PARTSTAT=ACCEPTED:mailto:me@example.com"),
        )
        self.tick()
        self.assertEqual(self.announced_titles(), ["Kept"])

    def test_moved_and_vanished_events(self):
        self.set_feed(
            vevent("move", "20261021T002000Z", SUMMARY=":Moved"),  # 10:20
            vevent("gone", "20261021T002000Z", SUMMARY=":Gone"),
        )
        self.tick()  # 20 min away: nothing yet
        # Moved to 10:40 and the other deleted, before their alerts were due.
        self.set_feed(vevent("move", "20261021T004000Z", SUMMARY=":Moved"))
        self.tick(advance=timedelta(minutes=5))  # 10:05, when they would have gone off
        self.assertEqual(self.announced_titles(), [])
        self.tick(advance=timedelta(minutes=20))  # 10:25: the moved one's new time
        self.assertEqual(self.announced_titles(), ["Moved"])

    def test_failed_download_keeps_last_good_copy(self):
        self.set_feed(vevent("a", "20261021T013000Z", SUMMARY=":Budget review"))
        self.tick()
        with mock.patch.object(ics_feed, "fetch", side_effect=OSError("timed out")):
            with self.assertLogs("bonsai-alerts", "WARNING") as logs:
                self.tick(advance=timedelta(minutes=75))
        self.assertEqual(self.announced_titles(), ["Budget review"])
        self.assertNotIn("secret-token", "\n".join(logs.output))
        self.assertIn("example.com/…", "\n".join(logs.output))

    def test_refreshes_calendar_just_before_announcing(self):
        self.set_feed(vevent("a", "20261021T001500Z", SUMMARY=":Budget review"))  # 10:15
        self.alerter._last_fetch = None
        self.now -= timedelta(minutes=2)
        self.alerter.tick()  # 9:58: downloaded
        self.feed = calendar()  # cancelled (gone) since
        self.now += timedelta(minutes=2)
        self.alerter.tick()  # 10:00: poll isn't due, but the copy is 2 min old so it's re-checked
        self.assertEqual(self.announced_titles(), [])
        self.assertEqual(self.fetches, 2)


if __name__ == "__main__":
    unittest.main()
