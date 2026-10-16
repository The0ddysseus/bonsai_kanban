"""
Calendar -> cards.

Works with any calendar published as an .ics link, e.g. Outlook on the web
(Settings -> Calendar -> Shared calendars -> Publish a calendar) or Google
Calendar (the calendar's settings -> "Secret address in iCal format").
Each board can have its own calendar link (e.g. a Work board synced from
Outlook, a Personal board from Google). CalendarSync downloads every board's
link when the app starts and every few minutes after, and apply_events()
keeps one card per event in that board's "Calendar" column:

- A new event becomes a new card (title, action date(s), and a description
  with the times, the Teams or Google Meet join link, and the invite's text).
- When an event changes, its card's title, description and action dates
  are rewritten to match. Changes you make to the card stick until then.
- A cancelled event gets a red "Cancelled" label. Calendars (Outlook in
  particular) often just drop a cancelled meeting from the feed rather
  than marking it, so an
  event that vanishes before it starts counts as cancelled too. (Events
  that have already started also leave the feed, as it only covers a
  short window -- those are left alone.)

Skipped: recurring events (the series and any changed occurrences),
events that are over before they're first seen, and events whose card
you've deleted.
"""

import hashlib
import html
import re
import threading
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import icalendar
from PySide6.QtCore import QObject, QTimer, Signal

import database as db

CALENDAR_COLUMN_NAME = "Calendar"
CANCELLED_LABEL_NAME = "Cancelled"
CANCELLED_LABEL_COLOR = "#d92d20"
SYNC_INTERVAL_MS = 5 * 60 * 1000

_CANCELLED_PREFIX = re.compile(r"^\s*(?:canceled|cancelled)\s*:\s*", re.IGNORECASE)
# Where the meeting-service boilerplate starts in an invite's text: Outlook's
# "_____" line, or Google's "-::~:~::~:...::-" line.
_SEPARATOR_LINE = re.compile(r"^(?:_{10,}|-::~[:~]+-?)\s*$")
_HTML_TAG = re.compile(r"<\s*/?\s*(?:br|p|div|a|b|i|u|span|ul|ol|li|html|body)\b[^>]*>", re.IGNORECASE)
_TEAMS_JOIN_URL = re.compile(r"https://teams\.microsoft\.com/(?:meet|l/meetup-join)/[^\s<>]+")


@dataclass
class CalendarEvent:
    uid: str
    title: str
    action_start: str  # "yyyy-mm-dd", local time
    action_end: str  # last day the event runs on; same as action_start for one day
    description: str
    cancelled: bool

    def fingerprint(self) -> str:
        """Changes whenever anything shown on the card changes."""
        text = "\0".join((self.title, self.action_start, self.action_end, self.description))
        return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    cancelled: int = 0
    column_created: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.created or self.updated or self.cancelled or self.column_created)


def _time(moment: datetime) -> str:
    """e.g. "9:30 am". Built by hand: strftime's "%-I" (no leading zero)
    isn't supported by every C library, and "%p" varies by language.
    """
    hour = moment.hour % 12 or 12
    return f"{hour}:{moment.minute:02d} {'am' if moment.hour < 12 else 'pm'}"


def _plain_text(text: str) -> str:
    """Google Calendar sends some descriptions as HTML; turn those into plain
    text (line breaks kept, links as "text (address)"). Plain text, which is
    what Outlook sends, is returned unchanged.
    """
    if not _HTML_TAG.search(text):
        return text

    def link(match):
        address, label = match.group(1), re.sub(r"<[^>]+>", "", match.group(2)).strip()
        return address if not label or label == address else f"{label} ({address})"

    text = re.sub(r'<a\s[^>]*href="([^"]*)"[^>]*>(.*?)</a>', link, text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<\s*br\s*/?>|</\s*(?:p|div|li)\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*li[^>]*>", "- ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def _clean_body(text: str) -> str:
    """The invite's own text: everything above the meeting-service
    boilerplate (see _SEPARATOR_LINE; the join details in it are picked out
    separately), with long runs of blank lines squeezed down to one.
    """
    lines = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if _SEPARATOR_LINE.match(line):
            break
        lines.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _teams_details(text: str) -> list[str]:
    """ "Join: <link>" plus the meeting ID and passcode, when it's a Teams meeting."""
    join = _TEAMS_JOIN_URL.search(text)
    if not join:
        return []
    details = [f"Join: {join.group()}"]
    for field in ("Meeting ID", "Passcode"):
        match = re.search(rf"^{field}:\s*(.+)$", text, re.MULTILINE)
        if match:
            details.append(f"{field}: {match.group(1).strip()}")
    return details


def _to_event(component) -> CalendarEvent:
    title = str(component.get("SUMMARY", "")).strip() or "(no title)"
    cancelled = str(component.get("STATUS", "")).upper() == "CANCELLED"
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

    if isinstance(start, datetime):
        start = start.astimezone() if start.tzinfo else start
        end = end.astimezone() if end.tzinfo else end
        start_day = start.date()
        # A meeting ending at exactly midnight finishes on the day before.
        end_day = (end - timedelta(microseconds=1)).date() if end > start else start_day
        when = f"{_time(start)} – {_time(end)}" if end > start else _time(start)
    else:  # all-day event; DTEND is the day after it finishes
        start_day = start
        end_day = end - timedelta(days=1) if end > start else start
        when = "All day"

    raw_description = _plain_text(str(component.get("DESCRIPTION", "")))
    location = str(component.get("LOCATION", "")).strip()
    join = _teams_details(raw_description)
    if join and location.lower() == "microsoft teams meeting":
        location = ""  # the Join line says so already
    if not join and component.get("X-GOOGLE-CONFERENCE"):
        join = [f"Join: {component.get('X-GOOGLE-CONFERENCE')}"]  # Google Meet
        if location == str(component.get("X-GOOGLE-CONFERENCE")):
            location = ""
    header = [f"{when} · {location}" if location else when, *join]
    body = _clean_body(raw_description)
    description = "\n".join(header) + (f"\n\n{body}" if body else "")

    return CalendarEvent(
        uid=str(component.get("UID")),
        title=title,
        action_start=start_day.isoformat(),
        action_end=max(end_day, start_day).isoformat(),
        description=description,
        cancelled=cancelled,
    )


def parse_events(ics_data: bytes) -> list[CalendarEvent]:
    """The one-off (non-recurring) events in an .ics file."""
    calendar = icalendar.Calendar.from_ical(ics_data)
    return [
        _to_event(component)
        for component in calendar.walk("VEVENT")
        if not any(key in component for key in ("RRULE", "RDATE", "RECURRENCE-ID")) and component.get("UID")
    ]


def _calendar_column_id(board_id: int) -> tuple[int, bool]:
    """(id of the board's Calendar column, whether it had to be created)."""
    for column in db.get_columns(board_id):
        if column["name"].strip().lower() == CALENDAR_COLUMN_NAME.lower():
            return column["id"], False
    return db.add_column(board_id, CALENDAR_COLUMN_NAME), True


def apply_events(board_id: int, events: list[CalendarEvent], today: date | None = None) -> SyncResult:
    """Bring the board's calendar cards in line with the feed's events."""
    today = (today or date.today()).isoformat()
    result = SyncResult()
    known = db.get_calendar_events(board_id)
    column_id = None
    cancelled_label_id = None

    def set_cancelled(card_id: int, cancelled: bool):
        nonlocal cancelled_label_id
        if cancelled_label_id is None:
            cancelled_label_id = db.find_or_add_label(board_id, CANCELLED_LABEL_NAME, CANCELLED_LABEL_COLOR)
        db.set_card_label(card_id, cancelled_label_id, cancelled)

    for event in events:
        record = known.pop(event.uid, None)
        if record is None:
            if event.cancelled or event.action_end < today:
                continue
            if column_id is None:
                column_id, result.column_created = _calendar_column_id(board_id)
            card_id = db.add_card(
                column_id, event.title, event.description, None, event.action_start, event.action_end
            )
            db.save_calendar_event(board_id, event.uid, card_id, event.fingerprint(), event.action_start, False)
            result.created += 1
            continue

        card_id = record["card_id"]
        card = db.get_card(card_id) if card_id is not None else None
        if card is None:
            continue  # card was deleted: leave it that way
        fingerprint = event.fingerprint()
        updated = False
        if fingerprint != record["fingerprint"]:
            db.update_card(
                card_id, event.title, event.description, card["due_date"], event.action_start, event.action_end
            )
            updated = True
        if event.cancelled != bool(record["cancelled"]):
            set_cancelled(card_id, event.cancelled)
            if event.cancelled:
                result.cancelled += 1
            else:
                updated = True  # back on again
        result.updated += updated
        db.save_calendar_event(board_id, event.uid, card_id, fingerprint, event.action_start, event.cancelled)

    # Imported events no longer in the feed: cancelled if they hadn't started
    # yet, otherwise they've just aged out of the published window.
    for uid, record in known.items():
        if record["card_id"] is None or record["cancelled"] or record["start_date"] < today:
            continue
        if db.get_card(record["card_id"]) is None:
            continue
        set_cancelled(record["card_id"], True)
        db.save_calendar_event(board_id, uid, record["card_id"], record["fingerprint"], record["start_date"], True)
        result.cancelled += 1

    return result


class CalendarSync(QObject):
    """Downloads each board's calendar now and then, on background threads
    so the window never freezes, and applies it to that board (back on the
    main thread). Boards without a link are left alone.
    """

    synced = Signal(int, object)  # board_id, SyncResult
    failed = Signal(int, str)  # board_id, error message
    _downloaded = Signal(int, object, str)  # board_id, ics bytes or None, error message

    def __init__(self, parent=None):
        super().__init__(parent)
        self._busy: set[int] = set()  # boards with a download in progress
        # Emitted from the download threads; Qt delivers it on the main thread.
        self._downloaded.connect(self._on_downloaded)
        self._timer = QTimer(self)
        self._timer.setInterval(SYNC_INTERVAL_MS)
        self._timer.timeout.connect(self.sync_all)
        self._timer.start()

    @staticmethod
    def url(board_id: int) -> str | None:
        return db.get_calendar_url(board_id)

    @staticmethod
    def set_url(board_id: int, url: str | None) -> None:
        if url and url.lower().startswith("webcal://"):
            url = "https://" + url[len("webcal://"):]
        db.set_calendar_url(board_id, url or None)

    def sync_all(self):
        for board in db.get_boards():
            self.sync_board(board["id"])

    def sync_board(self, board_id: int):
        url = self.url(board_id)
        if not url or board_id in self._busy:
            return
        self._busy.add(board_id)
        threading.Thread(target=self._download, args=(board_id, url), daemon=True).start()

    def _download(self, board_id: int, url: str):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "bonsai-kanban"})
            with urllib.request.urlopen(request, timeout=30) as response:
                self._downloaded.emit(board_id, response.read(), "")
        except Exception as error:
            self._downloaded.emit(board_id, None, str(getattr(error, "reason", error)))

    def _on_downloaded(self, board_id: int, data: bytes | None, error: str):
        self._busy.discard(board_id)
        if not any(board["id"] == board_id for board in db.get_boards()):
            return  # the board was deleted while its calendar downloaded
        if data is None:
            self.failed.emit(board_id, error)
            return
        try:
            events = parse_events(data)
        except Exception as error:  # not an .ics file, e.g. a sign-in page
            self.failed.emit(board_id, f"Couldn't read the calendar ({error})")
            return
        self.synced.emit(board_id, apply_events(board_id, events))
