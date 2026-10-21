"""
Calendar (.ics link) downloading and reading, shared by the board's calendar
sync (calendar_sync.py) and the meeting alerter (alerter.py). No Qt here, so
the alerter can run without the app.

- fetch() downloads a published calendar; redact() is how a link appears in
  logs and messages (its path holds a private token, so only the host shows).
- one_off_events() reads the events that happen once; occurrences() also
  expands recurring ones (RRULE / RDATE, minus EXDATE, with moved or edited
  occurrences -- RECURRENCE-ID -- in place of the ones they replace), using
  recurring_ical_events.
- Both give Occurrence objects with times in local time. Outlook names its
  time zones the Windows way ("E. Australia Standard Time") and describes
  them itself in a VTIMEZONE; those are swapped for the real IANA zone
  (Australia/Brisbane) so daylight saving follows the system's tz database.
- Attendees and the organizer are only in the feed when the calendar is
  published with them (Outlook's published calendars usually leave them
  out), so code using them must cope with none.
"""

import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import icalendar
import recurring_ical_events
from icalendar.timezone.windows_to_olson import WINDOWS_TO_OLSON

USER_AGENT = "bonsai-kanban"

_CANCELLED_PREFIX = re.compile(r"^\s*(?:canceled|cancelled)\s*:\s*", re.IGNORECASE)
TEAMS_JOIN_URL = re.compile(r"https://teams\.microsoft\.com/(?:meet|l/meetup-join)/[^\s<>]+")


@dataclass(frozen=True)
class Person:
    name: str  # CN, may be empty
    email: str  # may be empty
    response: str = ""  # PARTSTAT, upper case: ACCEPTED, DECLINED, TENTATIVE, NEEDS-ACTION; "" if not given
    kind: str = ""  # CUTYPE, upper case: INDIVIDUAL, ROOM, RESOURCE, ...; "" if not given

    @property
    def display_name(self) -> str:
        """The name, else the part of the email address before the @."""
        return self.name or self.email.split("@")[0]

    @property
    def is_room(self) -> bool:
        return self.kind in ("ROOM", "RESOURCE")


@dataclass
class Occurrence:
    """One time an event happens: a one-off event, or one occurrence of a
    recurring one (which all share the series' uid).
    """

    uid: str
    title: str  # "(no title)" if it has none; any "Canceled: " prefix removed
    start: datetime | date  # timed: an aware datetime in local time; all-day: a date
    end: datetime | date  # all-day: the day after it finishes, as in the .ics
    location: str
    description: str  # as in the .ics (Google sends some as HTML)
    status: str  # STATUS, upper case; "" if not given
    cancelled: bool
    recurring: bool  # part of a series, or a moved/edited occurrence of one
    organizer: Person | None = None
    attendees: list[Person] = field(default_factory=list)
    conference: str = ""  # Google Meet's join link (X-GOOGLE-CONFERENCE)

    @property
    def all_day(self) -> bool:
        return not isinstance(self.start, datetime)

    @property
    def key(self) -> tuple[str, str]:
        """Tells occurrences apart: uid plus start time (in UTC, so changing
        the computer's time zone doesn't make it look like a different one).
        A meeting that moves gets a new key.
        """
        start = self.start.astimezone(timezone.utc) if isinstance(self.start, datetime) else self.start
        return self.uid, start.isoformat()

    @property
    def is_teams(self) -> bool:
        return "microsoft teams" in self.location.lower() or bool(TEAMS_JOIN_URL.search(self.description))

    def response_of(self, email: str) -> str:
        """That person's PARTSTAT (e.g. "DECLINED"), or "" if they aren't
        listed or the feed doesn't say.
        """
        email = email.strip().casefold()
        for person in self.attendees:
            if email and person.email.casefold() == email:
                return person.response
        return ""


def normalise_url(url: str) -> str:
    """webcal:// links (what "subscribe" buttons often give) are https."""
    url = url.strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    return url


def redact(url: str) -> str:
    """The link as it's safe to show in logs: just the host, as the rest
    (e.g. Outlook's .../calendar/<token>/calendar.ics) gives access to the
    calendar.
    """
    host = urllib.parse.urlsplit(normalise_url(url)).hostname
    return f"{host}/…" if host else "(calendar link)"


def fetch(url: str, timeout: float = 30) -> bytes:
    """Download a calendar. Errors are raised as OSError with a message
    that doesn't include the link.
    """
    request = urllib.request.Request(normalise_url(url), headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except Exception as error:
        raise OSError(str(getattr(error, "reason", error))) from None


def read_calendar(data: bytes) -> icalendar.Calendar:
    """Raises ValueError if it isn't a calendar (e.g. a sign-in page)."""
    return icalendar.Calendar.from_ical(data)


def _iana_zone(moment: datetime) -> tzinfo | None:
    """The IANA zone for a datetime's time zone, if it isn't one already
    but its name is a Windows zone name or an IANA name.
    """
    if isinstance(moment.tzinfo, ZoneInfo) or moment.tzinfo is timezone.utc:
        return None
    try:
        name = icalendar.timezone.tzid_from_dt(moment)
    except Exception:
        return None
    if not name:
        return None
    try:
        return ZoneInfo(WINDOWS_TO_OLSON.get(name, name))
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _local(value: datetime | date, local_zone: tzinfo | None) -> datetime | date:
    """A timed value in local time (local_zone, or the system's if None);
    all-day dates are left as they are. Floating times (no time zone) are
    taken as local already.
    """
    if not isinstance(value, datetime):
        return value
    if value.tzinfo is None:
        return value.replace(tzinfo=local_zone) if local_zone else value.astimezone()
    zone = _iana_zone(value)
    if zone is not None:
        value = value.replace(tzinfo=zone)  # same clock time, in the real zone
    return value.astimezone(local_zone)


def _person(prop) -> Person:
    email = str(prop).strip()
    if email.lower().startswith("mailto:"):
        email = email[len("mailto:"):]
    params = getattr(prop, "params", {})
    return Person(
        name=str(params.get("CN", "")).strip().strip('"'),
        email=email,
        response=str(params.get("PARTSTAT", "")).upper(),
        kind=str(params.get("CUTYPE", "")).upper(),
    )


def _to_occurrence(component, local_zone: tzinfo | None) -> Occurrence:
    title = str(component.get("SUMMARY", "")).strip() or "(no title)"
    status = str(component.get("STATUS", "")).upper()
    cancelled = status == "CANCELLED"
    if _CANCELLED_PREFIX.match(title):  # e.g. Outlook's "Canceled: <subject>"
        cancelled = True
        title = _CANCELLED_PREFIX.sub("", title) or "(no title)"

    start = component.decoded("DTSTART")
    if "DTEND" in component:
        end = component.decoded("DTEND")
    elif "DURATION" in component:
        end = start + component.decoded("DURATION")
    else:
        end = start

    attendees = component.get("ATTENDEE", [])
    if not isinstance(attendees, list):
        attendees = [attendees]
    organizer = component.get("ORGANIZER")

    return Occurrence(
        uid=str(component.get("UID")),
        title=title,
        start=_local(start, local_zone),
        end=_local(end, local_zone),
        location=str(component.get("LOCATION", "")).strip(),
        description=str(component.get("DESCRIPTION", "")),
        status=status,
        cancelled=cancelled,
        recurring=any(key in component for key in ("RRULE", "RDATE", "RECURRENCE-ID")),
        organizer=_person(organizer) if organizer is not None else None,
        attendees=[_person(attendee) for attendee in attendees],
        conference=str(component.get("X-GOOGLE-CONFERENCE", "")),
    )


def one_off_events(calendar: icalendar.Calendar, local_zone: tzinfo | None = None) -> list[Occurrence]:
    """The events that happen once (not part of a series), in the order
    they're in the file.
    """
    return [
        _to_occurrence(component, local_zone)
        for component in calendar.walk("VEVENT")
        if not any(key in component for key in ("RRULE", "RDATE", "RECURRENCE-ID")) and component.get("UID")
    ]


def occurrences(
    calendar: icalendar.Calendar, first_day: date, last_day: date, local_zone: tzinfo | None = None
) -> list[Occurrence]:
    """Every occurrence, recurring or not, that overlaps first_day ..
    last_day (inclusive), cancelled ones included (check .cancelled).
    """
    expanded = recurring_ical_events.of(calendar).between(first_day, last_day + timedelta(days=1))
    return [_to_occurrence(component, local_zone) for component in expanded if component.get("UID")]
