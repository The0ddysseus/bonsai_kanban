"""
Bonsai's meeting alerter: a background program (run at login by a systemd
user service; see the README) that, 15 minutes before each meeting in your
calendar, plays a chime, says what's coming up and shows a popup -- whether
or not the Bonsai window is open.

    bonsai-alerts            run (what the service does)
    bonsai-alerts --list     show the coming day's meetings and what would be said
    bonsai-alerts --test     sound a sample alert now
    (or python alerter.py ..., from the project folder)

How it goes:
- The calendar (the links set on Bonsai's boards, or ics_url in the config)
  is downloaded every poll_minutes. A failed download is logged and the last
  good copy kept; it's tried again next time.
- Every TICK_SECONDS it looks for meetings starting within lead_minutes
  (plus PREPARE_SECONDS, to get the speech ready in time). Skipped: all-day
  events, cancelled ones (or ones gone from the calendar), ones you've
  declined (if the calendar says), ones already started, and ones already
  announced (alerts_store, so restarts don't repeat them).
- Just before it announces one, the calendar is downloaded again (if the copy
  is more than FRESH_SECONDS old) and the meeting checked once more: still
  there, not cancelled, not moved.
- Started late (e.g. just after login) with a meeting less than
  lead_minutes away? It's announced straight away, with the real time left.
"""

import sys
from pathlib import Path

# Same as in main.py: lets these modules import each other by plain name
# both from the project folder and when installed as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import argparse
import logging
import signal
import sqlite3
import threading
import time
from datetime import datetime, timedelta

import alerts_config
import alerts_message
import database as db
import ics_feed
from alerts_audio import Player, Speaker
from alerts_notify import notify
from alerts_store import AlertStore

log = logging.getLogger("bonsai-alerts")

TICK_SECONDS = 15
PREPARE_SECONDS = 120  # make the speech this long before it's due
FRESH_SECONDS = 60  # before announcing, re-download the calendar if older than this
HOUSEKEEPING_SECONDS = 60 * 60


def spoken_alerts_on() -> bool:
    """The "Spoken meeting alerts" switch in the app's Calendar menu (on
    unless switched off). Off leaves popups but mutes chime and speech.
    """
    if not db.DB_PATH.exists():
        return True
    try:
        return db.get_setting(alerts_config.SPOKEN_SETTING) != "off"
    except sqlite3.Error:
        return True


class Alerter:
    def __init__(self, config_path: Path | None = None, store: AlertStore | None = None):
        self.config_path = config_path or alerts_config.config_path()
        self.config = alerts_config.load(self.config_path)
        self._config_mtime = self._mtime()
        self.store = store or AlertStore()
        self.speaker = Speaker()
        self.player = Player()
        self.calendars: dict[str, ics_feed.icalendar.Calendar] = {}  # link -> last good copy
        self._last_fetch: float | None = None  # time.monotonic() of the last download attempt
        self._last_housekeeping = 0.0
        self._warned_no_links = False
        self.stopping = threading.Event()

    # --- settings and calendar ---

    def _mtime(self) -> float | None:
        try:
            return self.config_path.stat().st_mtime
        except OSError:
            return None

    def _reload_config_if_changed(self):
        mtime = self._mtime()
        if mtime != self._config_mtime:
            self._config_mtime = mtime
            self.config = alerts_config.load(self.config_path)
            log.info("Settings reloaded from %s", self.config_path)

    def links(self) -> list[str]:
        if self.config.ics_url:
            return [self.config.ics_url]
        try:
            return db.get_all_calendar_urls()
        except sqlite3.Error as error:
            log.warning("Couldn't read the calendar links from Bonsai's database (%s)", error)
            return list(self.calendars)  # carry on with the ones we had

    def refresh(self):
        """Download every calendar; any that fail keep their last good copy."""
        self._last_fetch = time.monotonic()
        links = self.links()
        if not links:
            if not self._warned_no_links:
                log.warning(
                    "No calendar link: set one on a board in Bonsai (Calendar → Set calendar link) "
                    "or ics_url in %s",
                    self.config_path,
                )
                self._warned_no_links = True
            self.calendars.clear()
            return
        self._warned_no_links = False
        for link in links:
            try:
                self.calendars[link] = ics_feed.read_calendar(ics_feed.fetch(link))
                log.debug("Downloaded calendar %s", ics_feed.redact(link))
            except Exception as error:
                kept = "keeping the last good copy" if link in self.calendars else "nothing to fall back on yet"
                log.warning("Couldn't download calendar %s: %s (%s)", ics_feed.redact(link), error, kept)
        for gone in set(self.calendars) - set(links):
            del self.calendars[gone]

    def events(self, now: datetime) -> dict[tuple[str, str], ics_feed.Occurrence]:
        """Every occurrence from yesterday to the day after tomorrow, from all
        the calendars, by key (so a calendar on two boards counts once).
        """
        found = {}
        for link, calendar in self.calendars.items():
            try:
                for event in ics_feed.occurrences(
                    calendar, now.date() - timedelta(days=1), now.date() + timedelta(days=2), self.config.local_zone
                ):
                    found[event.key] = event
            except Exception as error:
                log.error("Couldn't read the events in calendar %s (%s)", ics_feed.redact(link), error)
        return found

    def now(self) -> datetime:
        zone = self.config.local_zone
        return datetime.now(zone) if zone else datetime.now().astimezone()

    # --- deciding ---

    def should_announce(self, event: ics_feed.Occurrence, now: datetime) -> bool:
        """Whether this meeting gets an alert at all. (The one place to add
        rules such as quiet hours.)
        """
        if event.all_day or event.cancelled:
            return False
        if self.config.user_email and event.response_of(self.config.user_email) == "DECLINED":
            return False
        return event.start > now

    def coming_up(self, now: datetime) -> list[ics_feed.Occurrence]:
        """Meetings due an alert now, or soon enough to get the speech ready,
        that haven't had one yet; soonest first.
        """
        window = timedelta(minutes=self.config.lead_minutes, seconds=PREPARE_SECONDS)
        return sorted(
            (
                event
                for event in self.events(now).values()
                if self.should_announce(event, now)
                and event.start - now <= window
                and not self.store.was_announced(event.key)
            ),
            key=lambda event: event.start,
        )

    # --- doing ---

    def tick(self):
        self._reload_config_if_changed()
        if self._last_fetch is None or time.monotonic() - self._last_fetch >= self.config.poll_minutes * 60:
            self.refresh()
        now = self.now()
        soon = self.coming_up(now)
        if soon and time.monotonic() - self._last_fetch > FRESH_SECONDS:
            # About to get one ready or announce it: check against the latest calendar.
            self.refresh()
            now = self.now()
            soon = self.coming_up(now)
        lead = timedelta(minutes=self.config.lead_minutes)
        for event in soon:
            if event.start - now <= lead:
                self.announce(event, now)
            else:
                self.prepare(event)
        if time.monotonic() - self._last_housekeeping >= HOUSEKEEPING_SECONDS:
            self.housekeeping()

    def prepare(self, event: ics_feed.Occurrence):
        """Make the speech ahead of time, so it starts on time."""
        if self.config.play_speech and spoken_alerts_on():
            # As it will be worded when it goes off, lead_minutes before the start.
            due = event.start - timedelta(minutes=self.config.lead_minutes)
            self.speaker.prepare(alerts_message.spoken_message(event, self.config, due), self.config.voice_model)

    def announce(self, event: ics_feed.Occurrence, now: datetime):
        # Marked first, so even a crash part way through can't repeat it.
        self.store.mark_announced(event.key, event.title)
        log.info("Alert: %s, starting %s", event.title, event.start.strftime("%a %d %b %H:%M"))
        self.sound_alert(event, now)

    def sound_alert(self, event: ics_feed.Occurrence, now: datetime):
        config = self.config
        if config.show_popup:
            notify(*alerts_message.popup_message(event, config), urgency=config.urgency)
        if not (config.play_chime or config.play_speech):
            return
        if not spoken_alerts_on():
            log.info("Spoken alerts are switched off in Bonsai's Calendar menu: popup only")
            return
        sounds = []
        if config.play_chime:
            if Path(config.chime_sound).is_file():
                sounds.append(Path(config.chime_sound))
            else:
                log.error("Chime sound %s not found (chime_sound in %s)", config.chime_sound, self.config_path)
        if config.play_speech:
            text = alerts_message.spoken_message(event, config, now)
            log.info("Saying: %s", text)
            speech = self.speaker.prepare(text, config.voice_model)
            if speech:
                sounds.append(speech)
        self.player.play(sounds)

    def housekeeping(self):
        self._last_housekeeping = time.monotonic()
        self.store.prune()
        self.speaker.prune()

    def run(self):
        log.info(
            "Started: alerts %d minutes before meetings; calendar checked every %d minutes",
            self.config.lead_minutes,
            self.config.poll_minutes,
        )
        while not self.stopping.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("Something went wrong (carrying on)")
            self.stopping.wait(TICK_SECONDS)
        self.store.close()
        log.info("Stopped")


def _list(alerter: Alerter):
    """Print the next day's meetings and what each alert would say."""
    alerter.refresh()
    now = alerter.now()
    events = sorted(alerter.events(now).values(), key=lambda event: (str(event.start), event.title))
    shown = 0
    for event in events:
        if event.all_day or event.start < now or event.start > now + timedelta(days=1):
            continue
        shown += 1
        if alerter.store.was_announced(event.key):
            state = "already announced"
        elif alerter.should_announce(event, now):
            state = "will be announced"
        else:
            state = "skipped (cancelled or declined)"
        due = event.start - timedelta(minutes=alerter.config.lead_minutes)
        print(f"{event.start:%a %H:%M}  {event.title}  [{state}]")
        if state == "will be announced":
            print(f"    says: {alerts_message.spoken_message(event, alerter.config, max(due, now))}")
    if not shown:
        print("No meetings in the next 24 hours.")


def _test(alerter: Alerter, title: str):
    """Sound a made-up alert now, as it would be for a meeting in lead_minutes."""
    now = alerter.now()
    start = (now + timedelta(minutes=alerter.config.lead_minutes)).replace(second=0, microsecond=0)
    event = ics_feed.Occurrence(
        uid="test",
        title=title,
        start=start,
        end=start + timedelta(minutes=30),
        location="Microsoft Teams Meeting",
        description="",
        status="CONFIRMED",
        cancelled=False,
        recurring=False,
        organizer=ics_feed.Person("Sarah Chen", "sarah.chen@example.com"),
        attendees=[
            ics_feed.Person("Sarah Chen", "sarah.chen@example.com", "ACCEPTED"),
            ics_feed.Person("Tom Walsh", "tom.walsh@example.com", "TENTATIVE"),
        ],
    )
    alerter.sound_alert(event, start - timedelta(minutes=alerter.config.lead_minutes))
    alerter.player.wait()


def main():
    parser = argparse.ArgumentParser(prog="bonsai-alerts", description="Spoken alerts before your meetings.")
    parser.add_argument("--list", action="store_true", help="show the next day's meetings and what would be said")
    parser.add_argument("--test", nargs="?", const="Test meeting", metavar="TITLE", help="sound a sample alert now")
    parser.add_argument("--config", type=Path, help=f"settings file (default {alerts_config.config_path()})")
    parser.add_argument("-v", "--verbose", action="store_true", help="log more detail")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        # Under systemd the journal adds the time itself.
        format="%(levelname)s %(message)s" if not sys.stderr.isatty() else "%(asctime)s %(levelname)s %(message)s",
    )
    alerter = Alerter(args.config)
    if args.list:
        _list(alerter)
    elif args.test is not None:
        _test(alerter, args.test)
    else:
        signal.signal(signal.SIGTERM, lambda *_: alerter.stopping.set())  # systemctl stop
        try:
            alerter.run()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
