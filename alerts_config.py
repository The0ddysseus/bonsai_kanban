"""
Settings for the meeting alerter, from ~/.config/bonsai-kanban/alerts.toml
($XDG_CONFIG_HOME/bonsai-kanban/alerts.toml). Every setting is optional;
alerts.example.toml in the project folder lists them all. A missing file
means all the defaults; a setting that's the wrong type is logged and its
default used, so a typo never stops the alerts.

Not in this file: the "Spoken alerts" switch in the app's Calendar menu,
which is kept in the board database (SPOKEN_SETTING) so the app can change it.
"""

import logging
import os
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

import database as db

log = logging.getLogger("bonsai-alerts")

# In the board database's settings table: "off" mutes the chime and speech
# (popups still show). Set from the app's Calendar menu.
SPOKEN_SETTING = "spoken_alerts"

DEFAULT_VOICE = "en_US-lessac-medium"
DEFAULT_SPOKEN_TEMPLATE = "Hey {user_name}. {title} with {participants} starts at {spoken_time}, in {time_until}."
DEFAULT_SPOKEN_TEMPLATE_NO_ATTENDEES = "{title} starts at {spoken_time}, in {time_until}."
# The first of these that exists is the default chime.
DEFAULT_CHIMES = [
    "/usr/share/sounds/freedesktop/stereo/message-new-instant.oga",
    "/usr/share/sounds/freedesktop/stereo/bell.oga",
    "/usr/share/sounds/freedesktop/stereo/complete.oga",
]


def config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".config"
    return base / db.DATA_FOLDER / "alerts.toml"


def _default_chime() -> str:
    return next((path for path in DEFAULT_CHIMES if Path(path).exists()), DEFAULT_CHIMES[0])


@dataclass
class Config:
    # Leave empty to use the calendar links set on Bonsai's boards.
    ics_url: str = ""
    user_name: str = "there"  # "Hey there." until it's set
    user_email: str = ""  # left out of the participants, and to see if you've declined
    lead_minutes: int = 15
    poll_minutes: int = 5
    timezone: str = ""  # IANA name, e.g. "Australia/Brisbane"; empty = the system's
    voice_model: str = str(db.DATA_DIR / "voices" / f"{DEFAULT_VOICE}.onnx")
    chime_sound: str = field(default_factory=_default_chime)
    urgency: str = "normal"  # "low", "normal" or "critical" (stays until dismissed)
    play_chime: bool = True
    play_speech: bool = True
    show_popup: bool = True
    spoken_template: str = DEFAULT_SPOKEN_TEMPLATE
    spoken_template_no_attendees: str = DEFAULT_SPOKEN_TEMPLATE_NO_ATTENDEES
    # Applied to the spoken text only, whole words, e.g. {"QBR" = "Q B R"}.
    pronunciations: dict[str, str] = field(default_factory=dict)

    @property
    def local_zone(self) -> ZoneInfo | None:
        """The configured time zone, or None for the system's."""
        if not self.timezone:
            return None
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            log.error("Unknown timezone %r in the config; using the system's", self.timezone)
            return None


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    config = Config()
    try:
        with open(path, "rb") as file:
            values = tomllib.load(file)
    except FileNotFoundError:
        return config
    except (OSError, tomllib.TOMLDecodeError) as error:
        log.error("Couldn't read %s (%s); using the default settings", path, error)
        return config

    known = {f.name: f for f in fields(Config)}
    for key, value in values.items():
        if key not in known:
            log.warning("Unknown setting %r in %s (ignored)", key, path)
            continue
        default = getattr(config, key)
        # bool is a kind of int, so check it first.
        if isinstance(default, bool) != isinstance(value, bool) or not isinstance(value, type(default)):
            log.error("Setting %r in %s should be a %s; using %r", key, path, type(default).__name__, default)
            continue
        setattr(config, key, value)

    if config.urgency not in ("low", "normal", "critical"):
        log.error("urgency should be low, normal or critical, not %r; using normal", config.urgency)
        config.urgency = "normal"
    if config.lead_minutes < 1:
        log.error("lead_minutes must be at least 1; using 15")
        config.lead_minutes = 15
    if config.poll_minutes < 1:
        log.error("poll_minutes must be at least 1; using 5")
        config.poll_minutes = 5
    config.pronunciations = {str(word): str(said) for word, said in config.pronunciations.items()}
    config.voice_model = os.path.expanduser(config.voice_model)
    config.chime_sound = os.path.expanduser(config.chime_sound)
    return config
