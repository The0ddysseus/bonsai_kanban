"""
Desktop notifications for meeting alerts, through the desktop's own
notification system (notify-send, from libnotify).
"""

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("bonsai-alerts")

APP_NAME = "Bonsai"
ICON = Path(__file__).resolve().parent / "assets" / "bonsai.svg"


def notify(heading: str, body: str, urgency: str = "normal") -> bool:
    """Show a notification. "critical" ones stay on screen until dismissed.
    False (and the reason logged) if it couldn't be shown.
    """
    command = shutil.which("notify-send")
    if command is None:
        log.error("Can't show the popup: notify-send isn't installed (it's in the libnotify-bin package)")
        return False
    args = [command, f"--app-name={APP_NAME}", f"--urgency={urgency}"]
    if ICON.exists():
        args.append(f"--icon={ICON}")
    try:
        result = subprocess.run([*args, "--", heading, body], capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        log.error("Couldn't show the popup (%s)", error)
        return False
    if result.returncode != 0:
        log.error("Couldn't show the popup: %s", result.stderr.strip())
        return False
    return True
