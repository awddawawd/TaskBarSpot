import os
import re
import subprocess
import sys
import time
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class SpotifyClient:
    """
    A client for interacting with Spotify via spotify_cli.exe.
    Locates the CLI by finding the Spotify shortcut and deriving the path.
    """

    def __init__(self):
        self._spotify_exe_path = None
        self._cli_path = None
        self._last_previous_time = 0.0   # cooldown to avoid accidental double‑skips
        self._locate_cli()

    # ---------------------------------------------------------------
    # Internal helpers – path finding
    # ---------------------------------------------------------------
    def _locate_cli(self):
        """Find Spotify.exe and derive spotify_cli.exe path."""
        spotify_exe = self._find_spotify_exe()
        if not spotify_exe:
            raise FileNotFoundError(
                "Could not find a Spotify shortcut on Desktop or in Start Menu."
            )
        self._spotify_exe_path = Path(spotify_exe)
        self._cli_path = self._spotify_exe_path.parent / "spotify_cli.exe"

    @staticmethod
    def _get_shortcut_target(lnk_path: str) -> str | None:
        """Extract the target path from a .lnk shortcut using WScript.Shell via PowerShell."""
        try:
            ps_command = (
                f'(New-Object -ComObject WScript.Shell)'
                f'.CreateShortcut("{lnk_path}").TargetPath'
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_command],
                capture_output=True,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            target = result.stdout.strip()
            return target if target else None
        except Exception:
            return None

    @staticmethod
    def _find_spotify_exe() -> str | None:
        """Search Desktop and Start Menu for a Spotify shortcut."""
        search_roots = [
            Path.home() / "Desktop",
            Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu",
            Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft" / "Windows" / "Start Menu",
        ]

        for root in search_roots:
            if not root.exists():
                continue
            for lnk in root.rglob("*.lnk"):
                if "spotify" not in lnk.stem.lower():
                    continue
                target = SpotifyClient._get_shortcut_target(str(lnk))
                if target and os.path.basename(target).lower() == "spotify.exe":
                    return target
        return None

    # ---------------------------------------------------------------
    # Properties
    # ---------------------------------------------------------------
    @property
    def spotify_exe_path(self) -> Path:
        return self._spotify_exe_path

    @property
    def cli_path(self) -> Path:
        return self._cli_path

    # ---------------------------------------------------------------
    # CLI execution
    # ---------------------------------------------------------------
    def run_cli(self, *args: str, capture: bool = True, encoding: str = "utf-8") -> subprocess.CompletedProcess:
        """
        Run spotify_cli.exe with the given arguments.
        By default, captures stdout/stderr as text with UTF-8 encoding.
        Set capture=False to let output flow to the console.
        """
        if not self._cli_path or not self._cli_path.exists():
            raise FileNotFoundError(f"CLI executable not found at {self._cli_path}")
        cmd = [str(self._cli_path)] + list(args)
        return subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            encoding=encoding,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    # ---------------------------------------------------------------
    # Public methods
    # ---------------------------------------------------------------
    def is_desktop_client_running(self) -> bool:
        """
        Check if the Spotify desktop client is currently running.
        Returns True if the client is running, False otherwise.
        """
        result = self.run_cli("now-playing")
        output = result.stdout.strip()
        if "Spotify desktop client is not running" in output:
            return False
        return bool(output)

    def playback_status(self) -> str | None:
        """
        Returns 'Playing', 'Paused', or None if the desktop client
        is not running or the status cannot be determined.
        """
        result = self.run_cli("now-playing")
        output = result.stdout.strip()
        if not output or "Spotify desktop client is not running" in output:
            return None

        for line in output.splitlines():
            if line.startswith("Status: "):
                return line.split(":", 1)[1].strip()
        return None

    def now_playing(self) -> str | None:
        """
        Return the Spotify track URI of the currently playing track.
        Returns None if nothing is playing or the URI cannot be parsed.
        """
        result = self.run_cli("now-playing")
        output = result.stdout.strip()
        if not output:
            return None

        first_line = output.splitlines()[0].strip()
        # Look for a token that starts with "spotify:track:"
        for token in reversed(first_line.split()):
            if token.startswith("spotify:track:"):
                return token

        # Fallback regex
        match = re.search(r'(spotify:track:[a-zA-Z0-9]+)', first_line)
        if match:
            return match.group(1)

        return None

    def previous_track(self, played_since: float | None = None):
        """
        Go to the previous track, mimicking Spotify's own logic.
        Prevents accidental double‑skips by using a 3‑second cooldown:
        - If a 'previous' command was sent in the last 3 seconds,
          only ONE command is sent now.
        - Otherwise, the normal rule applies:
            * If the current track started < 3 seconds ago (e.g. after 'next'),
              one command is enough.
            * Else, two commands are sent (restart then skip back).
        """
        now = time.time()

        if now - self._last_previous_time < 3.0:
            self.run_cli("previous")
            self._last_previous_time = now
            return

        THRESHOLD = 3.0
        if played_since is not None and (now - played_since) < THRESHOLD:
            self.run_cli("previous")
        else:
            self.run_cli("previous")
            time.sleep(0.1)
            self.run_cli("previous")

        self._last_previous_time = now

    def is_track_saved(self, uri: str) -> bool | None:
        """
        Check if a track (by URI) is saved in the user's library.
        Returns True/False, or None if the check fails.
        """
        try:
            result = self.run_cli("library", "contains", uri)
            output = result.stdout.strip()

            if "Spotify desktop client is not running" in output:
                logger.error("Spotify desktop client is not running.")
                return None

            if output.endswith(": saved"):
                return True
            elif output.endswith(": not saved"):
                return False
            else:
                logger.error("Unexpected output from 'library contains': %s", output)
                return None

        except Exception as e:
            logger.error("Failed to check saved status: %s", e)
            return None

    def lookup(self, uri: str) -> dict | None:
        """
        Look up metadata for a Spotify track URI.
        Returns a dict with:
            title, artist, album, artist_uri, album_uri, artists (list)
        or None on failure.
        """
        result = self.run_cli("lookup", uri)
        output = result.stdout.strip()
        if not output:
            logger.error("Empty output from lookup command.")
            return None

        lines = output.splitlines()
        info = {}

        # First line: "[Song] <title>"
        if lines and lines[0].startswith("[Song] "):
            info["title"] = lines[0][7:].strip()
        else:
            logger.error("Unexpected lookup format: %s", lines[0] if lines else "no output")
            return None

        # --- FIX: Accumulate artists across multiple lines ---
        all_artists = []

        # Parse "By:" and "From:" lines
        for line in lines[1:]:
            if line.startswith("  By: "):
                rest = line[6:].strip()
                if " (spotify:" in rest: # Catches both artist and user URIs
                    name_part, uri_part = rest.rsplit(" (", 1)
                    all_artists.append({
                        "name": name_part.strip(),
                        "uri": uri_part.rstrip(")").strip()
                    })
                else:
                    all_artists.append({"name": rest, "uri": ""})
                    
            elif line.startswith("  From: "):
                rest = line[8:].strip()
                if " (spotify:album:" in rest:
                    name_part, uri_part = rest.rsplit(" (", 1)
                    info["album"] = name_part.strip()
                    info["album_uri"] = uri_part.rstrip(")").strip()
                else:
                    info["album"] = rest

        # Apply the accumulated artists back to the dictionary
        if all_artists:
            info["artists"] = all_artists
            info["artist"] = ", ".join(a["name"] for a in all_artists)
            info["artist_uri"] = all_artists[0]["uri"] # Main artist fallback

        # Ensure all keys exist
        for key in ("title", "artist", "album", "artist_uri", "album_uri"):
            info.setdefault(key, "")

        return info

    def start_minimized(self) -> subprocess.Popen | None:
        """
        Launch Spotify desktop client minimized.
        Returns the Popen object, or None if the executable path is missing.
        """
        if not self._spotify_exe_path or not self._spotify_exe_path.exists():
            logger.error("Cannot start Spotify: executable not found at %s", self._spotify_exe_path)
            return None
        logger.info("Launching Spotify minimized: %s", self._spotify_exe_path)
        return subprocess.Popen(
            [str(self._spotify_exe_path), "--minimized"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW
        )

    def wait_for_client(self, timeout: float = 30.0, poll_interval: float = 1.0) -> bool:
        """
        Wait for the Spotify desktop client to start running.
        Returns True if the client is running within the timeout, else False.
        """
        start = time.time()
        while time.time() - start < timeout:
            if self.is_desktop_client_running():
                logger.info("Spotify desktop client is now running.")
                return True
            time.sleep(poll_interval)
        logger.error("Timed out waiting for Spotify desktop client.")
        return False
    
    def get_queue(self) -> list[str] | None:
        """
        Get the current play queue as a list of Spotify track URIs.
        Returns an ordered list of strings like 'spotify:track:...',
        or None if the desktop client is not running.
        """
        result = self.run_cli("queue")
        output = result.stdout.strip()

        if "Spotify desktop client is not running" in output:
            logger.warning("Spotify desktop client is not running.")
            return None

        uris = []
        for line in output.splitlines():
            # Only process lines that actually contain a track number
            # (they start with whitespace + digits + dot)
            if not re.match(r'\s*\d+\.', line):
                continue

            # Extract the first occurrence of a Spotify track URI
            match = re.search(r'spotify:track:[a-zA-Z0-9]+', line)
            if match:
                uris.append(match.group(0))

        return uris


# ---------------------------------------------------------------
# Example usage
# ---------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
    try:
        client = SpotifyClient()
        print(f"Spotify.exe at : {client.spotify_exe_path}")
        print(f"CLI at         : {client.cli_path}")

        if client.is_desktop_client_running():
            print("Client is running.")
            uri = client.now_playing()
            if uri:
                print(f"Now playing   : {uri}")
                meta = client.lookup(uri)
                if meta:
                    print(f"Title         : {meta['title']}")
                    print(f"Artist        : {meta['artist']} ({meta['artist_uri']})")
                    print(f"Album         : {meta['album']} ({meta['album_uri']})")
                saved = client.is_track_saved(uri)
                print(f"Saved         : {saved}")
            else:
                print("Nothing is playing.")
        else:
            print("Spotify desktop client is NOT running.")
    except FileNotFoundError as e:
        print(e, file=sys.stderr)