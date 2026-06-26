import sys
import os
import logging
import time
import ctypes
import threading
from ctypes import wintypes
from PyQt5.QtCore import QTimer, QByteArray, QObject, pyqtSignal, QThread
from PyQt5.QtWidgets import QApplication

from spotify_client import SpotifyClient
from spotify_overlay import SpotifyOverlay
from spotify_track_monitor import SpotifyTrackMonitor
from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume

# -------------------------------------------------------------------
# Helper – close a window by process id (send WM_CLOSE)
# -------------------------------------------------------------------
def close_spotify_window_to_tray(pid: int, timeout: float = 10.0):
    """Find the main Spotify window for the given pid and send WM_CLOSE."""
    user32 = ctypes.windll.user32
    WM_CLOSE = 0x0010
    found = []

    def enum_callback(hwnd, lParam):
        if user32.IsWindowVisible(hwnd):
            process_id = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            if process_id.value == pid:
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    found.append(hwnd)
                    return False
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    enum_proc = WNDENUMPROC(enum_callback)

    start = time.time()
    while time.time() - start < timeout:
        found.clear()
        user32.EnumWindows(enum_proc, 0)
        if found:
            hwnd = found[0]
            logger.info("Sending WM_CLOSE to Spotify window (hwnd=0x%X)", hwnd)
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            return True
        time.sleep(0.5)

    logger.warning("Could not find Spotify window for pid %d within timeout", pid)
    return False

# -------------------------------------------------------------------
# Logging configuration
# -------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
logger = logging.getLogger("main")

# -------------------------------------------------------------------
# Volume helpers
# -------------------------------------------------------------------
def get_spotify_volume():
    try:
        for session in AudioUtilities.GetAllSessions():
            if session.Process and session.Process.name().lower() == "spotify.exe":
                vol = session._ctl.QueryInterface(ISimpleAudioVolume)
                return vol.GetMasterVolume()
    except Exception as e:
        logger.warning("Volume fetch failed: %s", e)
    return None

def set_spotify_volume(volume: float):
    volume = max(0.0, min(1.0, volume))
    try:
        for session in AudioUtilities.GetAllSessions():
            if session.Process and session.Process.name().lower() == "spotify.exe":
                vol = session._ctl.QueryInterface(ISimpleAudioVolume)
                vol.SetMasterVolume(volume, None)
    except Exception as e:
        logger.warning("Volume set failed: %s", e)

# -------------------------------------------------------------------
# Background worker – fetches playback state, volume, and monitors exit
# -------------------------------------------------------------------
class StatusWorker(QObject):
    playbackStateChanged = pyqtSignal(bool)
    volumeChanged = pyqtSignal(float)
    clientExited = pyqtSignal()

    def __init__(self, client: SpotifyClient):
        super().__init__()
        self._client = client
        self._running = True
        self._ready = False

    def set_ready(self):
        self._ready = True

    def run(self):
        logger.info("StatusWorker started")
        while self._running:
            if self._ready:
                try:
                    if not self._client.is_desktop_client_running():
                        logger.warning("Spotify desktop client exited.")
                        self.clientExited.emit()
                        return
                except Exception as e:
                    logger.warning("Client check failed: %s", e)

            try:
                status = self._client.playback_status()
                playing = (status == "Playing")
                self.playbackStateChanged.emit(playing)
            except Exception as e:
                logger.warning("Playback state check failed: %s", e)

            try:
                vol = get_spotify_volume()
                if vol is not None:
                    self.volumeChanged.emit(vol)
            except Exception as e:
                logger.warning("Volume check failed: %s", e)

            QThread.msleep(500)

    def stop(self):
        self._running = False

# -------------------------------------------------------------------
# Thread‑safe bridges for cross-thread UI updates
# -------------------------------------------------------------------
class TrackUpdateBridge(QObject):
    trackChanged = pyqtSignal(dict)

    def on_track_change(self, info: dict):
        self.trackChanged.emit(info)
    
class BackgroundLookupBridge(QObject):
    metaReady = pyqtSignal(str, dict, bool) # uri, link_dict, saved
    nextReady = pyqtSignal(dict) # metadata for next in queue
    actionVerified = pyqtSignal(str, bool) # context, status

# -------------------------------------------------------------------
# Main application
# -------------------------------------------------------------------
def main():
    args = sys.argv[1:]
    is_startup = '--startup' in args
    if is_startup:
        sys.argv.remove('--startup')

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    try:
        client = SpotifyClient()
        logger.info("SpotifyClient ready")
        proc = None

        if not client.is_desktop_client_running():
            logger.info("Spotify desktop client not running, launching minimized…")
            proc = client.start_minimized()
            if proc is None or not client.wait_for_client(timeout=30.0):
                logger.error("Failed to launch Spotify. Exiting.")
                sys.exit(1)
        else:
            logger.info("Spotify desktop client is already running.")

        if proc and proc.pid:
            time.sleep(2.0)
            close_spotify_window_to_tray(proc.pid)

        if not is_startup:
            logger.info("Manual launch – resuming playback")
            client.run_cli("resume")

    except FileNotFoundError as e:
        logger.error("Fatal error: %s", e)
        sys.exit(1)

    overlay = SpotifyOverlay()
    track_bridge = TrackUpdateBridge()
    bg_bridge = BackgroundLookupBridge()

    # ---- Fast Cache / State Tracking ----
    current_track_start = time.time()
    current_uri = None
    
    state = {'playing': False, 'saved': False}
    link_state = {'artist_uri': '', 'album_uri': ''}
    pending_verification = {'play_pause': False, 'like': False}

    # Predictive UI caches
    history_stack = []
    prefetched_next = None
    current_track_info = None

    # ---- Album byte cache for instant backward swipes ----
    album_byte_cache = {}

    # -------------------------------------------------------------------
    # Optimistic Actions (Non-blocking threads)
    # -------------------------------------------------------------------
    def bg_verify_action(context: str, func, expected):
        time.sleep(0.5) # Wait for CLI to apply change
        result = func()
        bg_bridge.actionVerified.emit(context, result == expected)
        
    def on_action_verified(context, matched):
        pending_verification[context] = False
        if not matched:
            logger.warning(f"Optimistic mismatch for {context}. State will self-correct.")

    bg_bridge.actionVerified.connect(on_action_verified)

    def handle_play_pause():
        new_playing = not state['playing']
        state['playing'] = new_playing
        overlay.set_playing(new_playing)
        logger.info(f"Play/Pause -> {'resume' if new_playing else 'pause'}")
        
        pending_verification['play_pause'] = True
        def task():
            client.run_cli("resume" if new_playing else "pause")
            bg_verify_action('play_pause', lambda: client.playback_status() == "Playing", new_playing)
            
        threading.Thread(target=task, daemon=True).start()

    def handle_like():
        if not current_uri:
            return
        new_saved = not state['saved']
        state['saved'] = new_saved
        overlay.set_liked(new_saved)
        logger.info(f"Like -> {'save' if new_saved else 'unsave'}")
        
        pending_verification['like'] = True
        def task(uri):
            client.run_cli("library", "add" if new_saved else "remove", uri)
            bg_verify_action('like', lambda: client.is_track_saved(uri), new_saved)

        threading.Thread(target=task, args=(current_uri,), daemon=True).start()

    def handle_next():
        logger.info("Optimistic Next Track (Swiped/Clicked)")
        if prefetched_next:
            # Visually snap to next track immediately
            overlay.set_track_info(prefetched_next.get("title", ""), prefetched_next.get("artist", ""))
        threading.Thread(target=client.run_cli, args=("next",), daemon=True).start()

    def handle_previous():
        logger.info("Optimistic Previous Track (Swiped/Clicked)")
        if history_stack:
            # Visually snap to historical track immediately
            prev_info = history_stack[-1]
            thumb = prev_info.get("thumbnail", b"")
            overlay.set_track_info(
                prev_info.get("title", ""), 
                prev_info.get("artist", ""), 
                QByteArray(thumb) if thumb else QByteArray()
            )
        threading.Thread(target=client.previous_track, args=(current_track_start,), daemon=True).start()

    overlay.playPauseClicked.connect(handle_play_pause)
    overlay.likeClicked.connect(handle_like)
    overlay.nextClicked.connect(handle_next)
    overlay.previousClicked.connect(handle_previous)

    # -------------------------------------------------------------------
    # Double-click interactions (Non-blocking)
    # -------------------------------------------------------------------
    def open_link(uri_target, fallback="spotify:"):
        def task():
            if uri_target:
                client.run_cli("navigate", uri_target)
            else:
                os.startfile(fallback)
        threading.Thread(target=task, daemon=True).start()

    overlay.albumDoubleClicked.connect(lambda: open_link(link_state.get('album_uri')))
    overlay.artistClicked.connect(lambda: open_link(link_state.get('artist_uri')))
    overlay.titleClicked.connect(lambda: open_link("spotify:collection:tracks"))
    overlay.settingsClicked.connect(lambda: logger.info("Settings clicked"))
    overlay.volumeChanged.connect(set_spotify_volume)

    # -------------------------------------------------------------------
    # Heavy Background Lookup (Prevents Track Change Stuttering)
    # -------------------------------------------------------------------
    def on_meta_ready(uri: str, links: dict, saved: bool):
        nonlocal current_uri
        current_uri = uri
        state['saved'] = saved
        overlay.set_liked(saved)
        link_state.update(links)
        
    def on_next_ready(meta: dict):
        nonlocal prefetched_next
        prefetched_next = meta

    bg_bridge.metaReady.connect(on_meta_ready)
    bg_bridge.nextReady.connect(on_next_ready)

    def fetch_heavy_metadata_bg():
        """Fetches active metadata and looks one step ahead into the queue natively."""
        uri = client.now_playing()
        saved = False
        links = {'artist_uri': '', 'album_uri': ''}
        
        if uri:
            saved = client.is_track_saved(uri) or False
            meta = client.lookup(uri)
            if meta:
                links['artist_uri'] = meta.get("artist_uri", "")
                links['album_uri'] = meta.get("album_uri", "")
        bg_bridge.metaReady.emit(uri or "", links, saved)
        
        # Pre-fetch the next track in the queue silently
        try:
            uris = client.get_queue()
            if uris:
                for u in uris:
                    if u != uri:
                        next_meta = client.lookup(u)
                        if next_meta:
                            bg_bridge.nextReady.emit(next_meta)
                        break
        except Exception as e:
            logger.debug(f"Queue prefetch failed: {e}")

    # -------------------------------------------------------------------
    # Track monitor event handler
    # -------------------------------------------------------------------
    def on_track_changed(info: dict):
        nonlocal current_track_start, current_track_info, prefetched_next
        
        title = info.get("title", "Unknown Title")
        artist = info.get("artist", "")
        
        # Memory Caching logic:
        cache_key = f"{title}|{artist}"
        thumb = info.get("thumbnail", b"")
        
        if thumb:
            # If we got a real image, cache it
            album_byte_cache[cache_key] = thumb
        elif cache_key in album_byte_cache:
            # If Windows failed/delayed the image, but we've seen it before, grab it from memory instantly!
            thumb = album_byte_cache[cache_key]
        
        # Intelligent History tracking (handles skips vs backward skips)
        if history_stack and history_stack[-1].get("title") == title:
            # We stepped backward - remove the current track so history stays aligned
            history_stack.pop()
        elif current_track_info and current_track_info.get("title") != title:
            # We stepped forward - save the exiting track
            if not history_stack or history_stack[-1].get("title") != current_track_info.get("title"):
                history_stack.append(current_track_info)
                if len(history_stack) > 20:
                    history_stack.pop(0)

        current_track_info = info
        current_track_start = time.time()
        prefetched_next = None # clear old cache immediately
        
        # 1. Update UI visually and instantly
        overlay.set_track_info(title, artist, QByteArray(thumb) if thumb else QByteArray())
        overlay.set_playing(True)
        state['playing'] = True
        
        # 2. Kick off the heavy CLI fetching in a background thread
        threading.Thread(target=fetch_heavy_metadata_bg, daemon=True).start()

    track_bridge.trackChanged.connect(on_track_changed)
    
    monitor = SpotifyTrackMonitor(track_bridge.on_track_change)
    monitor.start()

    # -------------------------------------------------------------------
    # Background Status Polling (State Recovery)
    # -------------------------------------------------------------------
    worker = StatusWorker(client)
    worker_thread = QThread()
    worker.moveToThread(worker_thread)
    worker_thread.started.connect(worker.run)
    worker_thread.start()
    worker.clientExited.connect(app.quit)

    def update_playing_from_worker(playing: bool):
        if not pending_verification['play_pause']:
            state['playing'] = playing
            overlay.set_playing(playing)

    worker.playbackStateChanged.connect(update_playing_from_worker)
    worker.volumeChanged.connect(overlay.set_volume)
    worker.set_ready()

    # -------------------------------------------------------------------
    # Clean shutdown
    # -------------------------------------------------------------------
    def cleanup():
        logger.info("Shutting down…")
        worker.stop()
        worker_thread.quit()
        worker_thread.wait()
        monitor.stop()
        
    app.aboutToQuit.connect(cleanup)

    logger.info("Application started")
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()