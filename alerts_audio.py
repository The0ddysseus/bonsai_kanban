"""
Sound for meeting alerts: the speech, made with Piper (the piper-tts
package) a little before it's due and kept as a .wav in the cache folder,
and a player that plays each alert's chime and speech in turn, one alert
at a time, through the default PipeWire output (pw-play, else paplay).

Piper is optional: without it (or its voice model), alerts just have no
speech, and the log says why.
"""

import hashlib
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path

import database as db

log = logging.getLogger("bonsai-alerts")

PLAYERS = ("pw-play", "paplay")
CACHE_KEEP_SECONDS = 24 * 60 * 60


def cache_dir() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME", "")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".cache"
    return base / db.DATA_FOLDER / "speech"


class Speaker:
    """Turns text into a .wav with Piper. The voice is loaded the first time
    it's needed (and again if the configured model changes).
    """

    def __init__(self, folder: Path | None = None):
        self.folder = folder or cache_dir()
        self._voice = None
        self._voice_path = ""
        self._failed_path = ""  # don't keep logging the same broken voice

    def _load(self, model_path: str):
        if self._voice is not None and self._voice_path == model_path:
            return self._voice
        if self._failed_path == model_path:
            return None
        self._voice = None
        try:
            from piper import PiperVoice
        except ImportError:
            log.error("Can't speak: Piper isn't installed (pip install piper-tts; see the README)")
            self._failed_path = model_path
            return None
        if not Path(model_path).is_file() or not Path(model_path + ".json").is_file():
            log.error("Can't speak: voice model %s (and its .json) not found; see the README", model_path)
            self._failed_path = model_path
            return None
        try:
            self._voice = PiperVoice.load(model_path)
        except Exception as error:
            log.error("Can't speak: couldn't load voice model %s (%s)", model_path, error)
            self._failed_path = model_path
            return None
        self._voice_path, self._failed_path = model_path, ""
        log.info("Loaded voice %s", Path(model_path).name)
        return self._voice

    def prepare(self, text: str, model_path: str) -> Path | None:
        """The speech as a .wav file, made now unless it's in the cache already.
        None if it can't be made (the reason is logged).
        """
        name = hashlib.sha256(f"{model_path}\0{text}".encode()).hexdigest()[:32]
        path = self.folder / f"{name}.wav"
        if path.exists():
            return path
        voice = self._load(model_path)
        if voice is None:
            return None
        self.folder.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(".part")
        try:
            with wave.open(str(partial), "wb") as wav_file:
                voice.synthesize_wav(text, wav_file)
            partial.replace(path)
        except Exception as error:
            log.error("Couldn't make the speech (%s)", error)
            partial.unlink(missing_ok=True)
            return None
        return path

    def prune(self) -> None:
        """Delete cached speech older than a day."""
        if not self.folder.is_dir():
            return
        cutoff = time.time() - CACHE_KEEP_SECONDS
        for path in self.folder.iterdir():
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass


def find_player() -> str | None:
    return next((path for path in map(shutil.which, PLAYERS) if path), None)


class Player:
    """Plays queued alerts one after another on a background thread, so
    alerts that fall together never overlap.
    """

    def __init__(self):
        self._queue: queue.Queue[list[Path]] = queue.Queue()
        threading.Thread(target=self._run, daemon=True, name="alert-player").start()

    def play(self, sounds: list[Path]) -> None:
        """Queue one alert's sounds, to be played in order."""
        if sounds:
            self._queue.put(sounds)

    def wait(self) -> None:
        """Wait until everything queued has been played."""
        self._queue.join()

    def _run(self):
        while True:
            sounds = self._queue.get()
            try:
                self._play(sounds)
            finally:
                self._queue.task_done()

    def _play(self, sounds: list[Path]):
        player = find_player()  # looked up each time, in case one gets installed
        if player is None:
            log.error("Can't play the alert: neither pw-play nor paplay is installed")
            return
        for sound in sounds:
            try:
                result = subprocess.run([player, str(sound)], capture_output=True, text=True, timeout=120, check=False)
                if result.returncode != 0:
                    log.error("%s couldn't play %s: %s", Path(player).name, sound, result.stderr.strip())
            except (OSError, subprocess.TimeoutExpired) as error:
                log.error("Couldn't play %s (%s)", sound, error)
