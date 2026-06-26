import asyncio
import logging
import threading
from typing import Callable, Optional, Dict

from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager,
)
from winrt.windows.storage.streams import DataReader

logger = logging.getLogger(__name__)


class SpotifyTrackMonitor:
    """
    Monitors the Spotify media session for track changes and calls a user-provided
    callback whenever a new track starts playing (including the currently playing
    track at launch).
    """

    def __init__(self, on_change: Callable[[Dict[str, str]], None]):
        self._callback = on_change
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = asyncio.Event()

    async def _extract_thumbnail_bytes(self, thumb_ref) -> bytes:
        if not thumb_ref:
            return b""
        try:
            stream = await thumb_ref.open_read_async()
            size = stream.size
            if size == 0:
                return b""
            input_stream = stream.get_input_stream_at(0)
            reader = DataReader(input_stream)
            await reader.load_async(size)
            return bytes(reader.read_buffer(size))
        except Exception:
            return b""

    async def _send_current_track(self, session):
        """Fetch the current track properties and invoke the callback."""
        try:
            props = await session.try_get_media_properties_async()
            title = props.title or ""
            artist = props.artist or ""
            thumb = await self._extract_thumbnail_bytes(props.thumbnail)
            info = {"title": title, "artist": artist, "thumbnail": thumb}
            self._callback(info)
        except Exception as e:
            logger.warning("Error fetching initial track: %s", e)

    async def _monitor_loop(self):
        try:
            manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
        except Exception as e:
            logger.error("Failed to get session manager: %s", e)
            return

        session = None
        current_key = None

        while not self._stop_event.is_set():
            while not self._stop_event.is_set():
                session = manager.get_current_session()
                if session and "spotify" in session.source_app_user_model_id.lower():
                    break
                await asyncio.sleep(1)

            if self._stop_event.is_set():
                break

            logger.info("Session found, fetching current track…")
            await self._send_current_track(session)

            async def handle_media_properties(sender):
                nonlocal current_key
                try:
                    props = await sender.try_get_media_properties_async()
                    title = props.title or ""
                    artist = props.artist or ""
                    thumb = await self._extract_thumbnail_bytes(props.thumbnail)

                    new_key = f"{title}|{artist}"
                    if new_key != current_key:
                        current_key = new_key
                        info = {"title": title, "artist": artist, "thumbnail": thumb}
                        self._callback(info)
                except Exception as e:
                    logger.warning("Error in media properties handler: %s", e)

            def on_media_properties_changed(sender, _):
                if self._loop is None:
                    return
                asyncio.run_coroutine_threadsafe(handle_media_properties(sender), self._loop)

            session.add_media_properties_changed(on_media_properties_changed)
            logger.info("Subscribed to media changes.")

            while not self._stop_event.is_set():
                await asyncio.sleep(1)
                if session != manager.get_current_session():
                    logger.info("Session lost, will re‑acquire…")
                    break

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._monitor_loop())
        finally:
            self._loop.close()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._stop_event.set)
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()