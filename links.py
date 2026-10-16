"""
Opening links clicked on cards.

Everything opens in the default browser, except Microsoft Teams links
(meeting join links and so on): those open in a Teams app if one is
installed, so they open signed in, in their own window. The app is found
by looking through the desktop's application launchers (.desktop files,
in the standard XDG folders, Flatpak's included) for one that's Teams:

- a web app made with Linux Mint's Web Apps tool  (chromium --app=URL ...)
- a site installed as an app from Chrome, Chromium, Edge, Brave, ...
  (... --profile-directory=X --app-id=ID): the link opens as an app window
  in that same browser profile, which is where the Teams sign-in lives
- a GNOME Web (Epiphany) web app  (epiphany --application-mode ... URL)
- a Firefox PWA  (firefoxpwa site launch ID)
- a native app that accepts links, e.g. teams-for-linux  (... %U)

If none of those suit, a command can be set by hand ("Teams links..." in
the Calendar menu); it's saved in the settings table.
"""

import configparser
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QProcess, QUrl
from PySide6.QtGui import QDesktopServices

import database as db

TEAMS_HOSTS = {"teams.microsoft.com", "teams.cloud.microsoft", "teams.live.com"}
COMMAND_SETTING = "teams_command"  # "" or unset = find a Teams app automatically

# Desktop-file placeholders (see the Desktop Entry spec). The URL ones are
# replaced by the link; the rest aren't real arguments and are dropped,
# along with Flatpak's "@@u ... @@" file-forwarding markers.
_URL_FIELD_CODES = {"%u", "%U", "%f", "%F"}
_DROPPED_ARGS = {"%i", "%c", "%k", "%d", "%D", "%n", "%N", "%v", "%m", "@@", "@@u"}


@dataclass
class Launcher:
    name: str
    path: Path
    exec_args: list[str]
    web_app_url: str  # X-WebApp-URL, set by Mint's Web Apps tool; else ""


def is_teams_link(url: str) -> bool:
    return QUrl(url).host().lower() in TEAMS_HOSTS


def _application_dirs() -> list[Path]:
    """Where launchers live, most important first: the user's own folder
    (plus the usual ~/.local/share, in case XDG_DATA_HOME points elsewhere,
    e.g. inside a sandbox), then the system ones, then Flatpak's (usually
    listed already).
    """
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    dirs = [data_home, str(Path.home() / ".local/share"), *data_dirs.split(":"),
            str(Path.home() / ".local/share/flatpak/exports/share"), "/var/lib/flatpak/exports/share"]
    result = []
    for d in dirs:
        path = Path(d) / "applications"
        if d and Path(d).is_absolute() and path not in result and path.is_dir():
            result.append(path)
    return result


def _read_launcher(path: Path) -> Launcher | None:
    entry = configparser.ConfigParser(interpolation=None, strict=False)
    entry.optionxform = str  # keep key case (Name vs name)
    try:
        entry.read(path, encoding="utf-8")
        section = entry["Desktop Entry"]
        args = shlex.split(section.get("Exec", ""))
    except (configparser.Error, KeyError, UnicodeDecodeError, ValueError):
        return None
    if section.get("Type") != "Application" or section.get("Hidden", "").lower() == "true" or not args:
        return None
    return Launcher(section.get("Name", path.stem), path, args, section.get("X-WebApp-URL", ""))


def _looks_like_teams(launcher: Launcher) -> bool:
    if launcher.web_app_url:
        return QUrl(launcher.web_app_url).host().lower() in TEAMS_HOSTS
    # "Teams", "Microsoft Teams", "teams-for-linux" -- but not "TeamSpeak".
    return any(re.search(r"\bteams\b", text, re.IGNORECASE) for text in (launcher.name, launcher.path.stem))


def find_teams_launcher() -> Launcher | None:
    """The first Teams launcher that can open a link, or None. Looked up each
    time, so it keeps working if the app is reinstalled under a new name.
    """
    seen = set()
    for directory in _application_dirs():
        for path in sorted(directory.rglob("*.desktop")):
            desktop_id = str(path.relative_to(directory))
            if desktop_id in seen:
                continue  # a user's launcher overrides a system one of the same name
            seen.add(desktop_id)
            launcher = _read_launcher(path)
            if launcher and _looks_like_teams(launcher) and launcher_command(launcher, "x") is not None:
                return launcher
    return None


def launcher_command(launcher: Launcher, url: str) -> list[str] | None:
    """The launcher's command, changed to open `url`, or None if this kind of
    launcher has no way to be given a link.
    """
    args = list(launcher.exec_args)
    program = os.path.basename(args[0])

    if any(arg.startswith("--app=") for arg in args):  # Mint web app / Chromium app window
        args = [f"--app={url}" if arg.startswith("--app=") else arg for arg in args]
    elif any(arg.startswith("--app-id=") for arg in args):  # site installed from a Chromium browser
        args = [f"--app={url}" if arg.startswith("--app-id=") else arg for arg in args]
        # Edge adds --app-url (the site's home page); it would compete with the link.
        args = [arg for arg in args if not arg.startswith(("--app-url=", "--app-launch-url-for-shortcuts-menu-item"))]
    elif program == "firefoxpwa" or "firefoxpwa" in args:
        args = [arg for arg in args if arg != "--protocol" and arg not in _URL_FIELD_CODES] + ["--url", url]
    elif "--application-mode" in args:  # GNOME Web
        urls = [i for i, arg in enumerate(args) if arg.startswith(("http://", "https://"))]
        if urls:
            args[urls[-1]] = url
        else:
            args.append(url)
    elif any(arg in _URL_FIELD_CODES for arg in args):  # an app that takes links the standard way
        first = next(i for i, arg in enumerate(args) if arg in _URL_FIELD_CODES)
        args = [url if i == first else arg for i, arg in enumerate(args) if i == first or arg not in _URL_FIELD_CODES]
    else:
        return None

    args = [arg for arg in args if arg not in _DROPPED_ARGS and arg not in _URL_FIELD_CODES]
    return [arg.replace("%%", "%") for arg in args]


def custom_command(url: str) -> list[str] | None:
    """The hand-set command (see COMMAND_SETTING) for `url`, or None if there
    isn't one. "{url}" in it is replaced by the link; without it, the link
    goes on the end.
    """
    template = (db.get_setting(COMMAND_SETTING) or "").strip()
    if not template:
        return None
    try:
        args = shlex.split(template)
    except ValueError:
        return None
    if any("{url}" in arg for arg in args):
        return [arg.replace("{url}", url) for arg in args]
    return args + [url]


def teams_command(url: str) -> list[str] | None:
    """How a Teams link would be opened: the hand-set command, else a found
    Teams app, else None (meaning: the default browser).
    """
    command = custom_command(url)
    if command:
        return command
    launcher = find_teams_launcher()
    return launcher_command(launcher, url) if launcher else None


def open_link(url: str) -> None:
    """Open a link from a card (see the module docstring)."""
    if is_teams_link(url):
        command = teams_command(url)
        if command and QProcess.startDetached(command[0], command[1:])[0]:
            return
    QDesktopServices.openUrl(QUrl(url))
