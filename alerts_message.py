"""
What a meeting alert says: the short spoken sentence and the fuller popup.
Built from templates (alerts_config.Config.spoken_template and
spoken_template_no_attendees), with times written as words so the voice
says "ten thirty a.m." rather than reading out "10:30".

Template placeholders: {user_name}, {title}, {participants}, {spoken_time}
(when it starts, in words) and {time_until} (e.g. "fifteen minutes"; less if
the alerter started late). After the template comes "It's a Teams meeting."
or "In <location>.", then the pronunciation fixes are applied.
"""

import html
import logging
import math
import re
from datetime import datetime

from alerts_config import DEFAULT_SPOKEN_TEMPLATE, DEFAULT_SPOKEN_TEMPLATE_NO_ATTENDEES, Config
from ics_feed import Occurrence, Person

log = logging.getLogger("bonsai-alerts")

_ONES = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen"
).split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
_URL = re.compile(r"^(?:https?://|www\.)", re.IGNORECASE)


def number_words(n: int) -> str:
    """0..99 in words, e.g. 45 -> "forty-five"; bigger numbers as digits."""
    if 0 <= n < 20:
        return _ONES[n]
    if 20 <= n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")
    return str(n)


def spoken_time(moment: datetime) -> str:
    """e.g. "ten thirty a.m.", "two p.m.", "nine oh five a.m.", "twelve noon"."""
    if moment.minute == 0 and moment.hour in (0, 12):
        return "twelve midnight" if moment.hour == 0 else "twelve noon"
    hour = number_words(moment.hour % 12 or 12)
    if moment.minute == 0:
        minutes = ""
    elif moment.minute < 10:
        minutes = f" oh {number_words(moment.minute)}"
    else:
        minutes = f" {number_words(moment.minute)}"
    return f"{hour}{minutes} {'a.m.' if moment.hour < 12 else 'p.m.'}"


def duration_words(seconds: float) -> str:
    """How long until it starts, rounded up to a whole minute: "one minute",
    "fifteen minutes", "one hour and five minutes".
    """
    minutes = max(1, math.ceil(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{number_words(hours)} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{number_words(minutes)} minute{'s' if minutes != 1 else ''}")
    return " and ".join(parts)


def people(event: Occurrence, user_email: str) -> list[Person]:
    """Who the meeting is with: the attendees (and organizer, if not listed
    with them), leaving out you, meeting rooms, and anyone who's declined.
    """
    me = user_email.strip().casefold()
    everyone = list(event.attendees)
    organizer = event.organizer
    if organizer and not any(organizer.email.casefold() == p.email.casefold() for p in everyone if p.email):
        everyone.insert(0, organizer)
    chosen, seen = [], set()
    for person in everyone:
        email = person.email.casefold()
        if (me and email == me) or person.is_room or person.response == "DECLINED":
            continue
        tag = email or person.display_name.casefold()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        chosen.append(person)
    return chosen


def participants_phrase(names: list[str]) -> str:
    """ "Sarah Chen", "Sarah Chen and Tom Walsh", or "Sarah Chen, Tom Walsh
    and four others".
    """
    if len(names) <= 2:
        return " and ".join(names)
    others = len(names) - 2
    return f"{names[0]}, {names[1]} and {number_words(others)} other{'s' if others != 1 else ''}"


def _where(event: Occurrence) -> str:
    if event.is_teams:
        return "It's a Teams meeting."
    # A web address (some invites put one here) is no use read out.
    if event.location and not _URL.match(event.location):
        return f"In {event.location.rstrip('.')}."
    return ""


def pronounce(text: str, fixes: dict[str, str]) -> str:
    """Apply the pronunciation fixes: whole words or phrases, as written.
    All in one pass, so one fix's result is never changed again by another.
    """
    words = sorted((word for word in fixes if word), key=len, reverse=True)  # longest first
    if not words:
        return text
    pattern = r"(?<!\w)(?:" + "|".join(map(re.escape, words)) + r")(?!\w)"
    return re.sub(pattern, lambda match: fixes[match.group()], text)


def spoken_message(event: Occurrence, config: Config, now: datetime) -> str:
    names = [person.display_name for person in people(event, config.user_email)]
    values = {
        "user_name": config.user_name,
        "title": event.title,
        "participants": participants_phrase(names),
        "spoken_time": spoken_time(event.start),
        "time_until": duration_words((event.start - now).total_seconds()),
    }
    template, default = (
        (config.spoken_template, DEFAULT_SPOKEN_TEMPLATE)
        if names
        else (config.spoken_template_no_attendees, DEFAULT_SPOKEN_TEMPLATE_NO_ATTENDEES)
    )
    try:
        text = template.format(**values)
    except (KeyError, IndexError, ValueError) as error:
        log.error("Spoken template %r doesn't work (%s); using the default", template, error)
        text = default.format(**values)
    where = _where(event)
    return pronounce(f"{text} {where}".strip(), config.pronunciations)


def _clock(moment: datetime) -> str:
    """e.g. "9:30 am" (see calendar_sync._time for why it's by hand)."""
    hour = moment.hour % 12 or 12
    return f"{hour}:{moment.minute:02d} {'am' if moment.hour < 12 else 'pm'}"


def popup_message(event: Occurrence, config: Config) -> tuple[str, str]:
    """(heading, body) for the desktop notification. The body uses the
    little markup notifications allow, so the text in it is escaped.
    """
    when = _clock(event.start)
    if isinstance(event.end, datetime) and event.end > event.start:
        when += f" – {_clock(event.end)}"
    lines = [when]
    if event.location:
        lines.append(f"Location: {event.location}")
    elif event.is_teams:
        lines.append("Location: Microsoft Teams")
    if event.organizer:
        lines.append(f"Organizer: {event.organizer.display_name}")
    me = config.user_email.strip().casefold()
    attendees = [p for p in event.attendees if not p.is_room and not (me and p.email.casefold() == me)]
    if attendees:
        names = [
            person.display_name + (f" ({person.response.lower()})" if person.response in ("DECLINED", "TENTATIVE") else "")
            for person in attendees
        ]
        lines.append(f"Attendees: {', '.join(names)}")
    return event.title, html.escape("\n".join(lines), quote=False)
