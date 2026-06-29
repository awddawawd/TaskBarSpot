import sys
sys.coinit_flags = 0

import os
import logging
import time
import ctypes
import threading
import traceback
import warnings       # <--- NEW
from pathlib import Path
from ctypes import wintypes
from PyQt5.QtCore import QTimer, QByteArray, QObject, pyqtSignal, QThread
from PyQt5.QtWidgets import QApplication, QMessageBox

import numpy as np
import soundcard as sc

from spotify_client import SpotifyClient
from spotify_overlay import SpotifyOverlay
from spotify_track_monitor import SpotifyTrackMonitor
from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume

warnings.filterwarnings("ignore", message="data discontinuity in recording") # <--- NEW

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
# Create a dedicated directory in AppData for logs
app_data_dir = Path(os.getenv('APPDATA', os.path.expanduser('~'))) / 'TaskBarSpot'
log_file_path = app_data_dir / 'taskbarspot.log'

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(log_file_path, encoding='utf-8'),
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger("main")

# Global exception handler to catch silent background crashes
def handle_unhandled_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logger.critical("Unhandled exception crashed the application:", exc_info=(exc_type, exc_value, exc_traceback))

sys.excepthook = handle_unhandled_exception

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
# True Audio FFT Worker (Frequency Bands)
# -------------------------------------------------------------------
class AudioFFTWorker(QObject):
    fftReady = pyqtSignal(list)

    # --- FFT Configuration ---
    FFT_SAMPLES = 1024           
    AUTO_GAIN_DECAY = 0.98       
    MIN_GAIN = 0.05              
    
    # --- Linear Frequency Scale Settings ---
    NUM_BARS = 7
    FREQ_CENTER = 600.0          # The exact middle of the visualizer (Bar 4 out of 7)
    FREQ_MIN = 100.0              # The absolute left edge (Bar 1)
    
    # Music naturally has way more energy in the bass (left) than the mids (right).
    # This multiplier slowly scales up from left to right to keep the visualizer balanced.
    TREBLE_BOOST = 2           # Set to 1.0 for true raw data, or higher to boost the right side.

    def __init__(self):
        super().__init__()
        self._running = True
        self._rolling_max = self.MIN_GAIN

    def run(self):
        logger.info("AudioFFTWorker started")
        try:
            speaker = sc.default_speaker()
            mics = sc.all_microphones(include_loopback=True)
            loopback = next((m for m in mics if m.isloopback and m.name == speaker.name), None)

            if not loopback:
                logger.warning("Could not find loopback device. FFT visualizer disabled.")
                return

            # Math to figure out our frequency buckets
            freq_delta = self.FREQ_CENTER - self.FREQ_MIN
            freq_max = self.FREQ_CENTER + freq_delta 
            hz_per_bar = (freq_max - self.FREQ_MIN) / self.NUM_BARS
            
            # 44100Hz samplerate / 1024 samples = ~43 Hz per FFT data point
            hz_per_bin = 44100 / self.FFT_SAMPLES

            with loopback.recorder(samplerate=44100) as mic:
                while self._running:
                    data = mic.record(numframes=self.FFT_SAMPLES)
                    mono = data.mean(axis=1)
                    fft_data = np.abs(np.fft.rfft(mono * np.hanning(len(mono))))

                    bands = []
                    
                    # Dynamically slice the FFT data into 7 frequency buckets
                    for i in range(self.NUM_BARS):
                        start_hz = self.FREQ_MIN + (i * hz_per_bar)
                        end_hz = start_hz + hz_per_bar
                        
                        # Convert Hertz to Array Indices
                        start_bin = max(1, int(start_hz / hz_per_bin))
                        end_bin = max(start_bin + 1, int(end_hz / hz_per_bin))
                        
                        # Safeguard in case user sets extremely high frequencies
                        end_bin = min(end_bin, len(fft_data))
                        
                        # Extract the average volume for this specific frequency range
                        band_val = float(np.mean(fft_data[start_bin:end_bin]))
                        
                        # Apply the gradual treble boost (0% boost on the left, 100% of TREBLE_BOOST on the right)
                        boost_multiplier = 1.0 + (self.TREBLE_BOOST - 1.0) * (i / (self.NUM_BARS - 1))
                        bands.append(band_val * boost_multiplier)

                    # --- Auto-Gain Normalization ---
                    current_max = max(bands)
                    
                    if current_max > self._rolling_max:
                        self._rolling_max = current_max
                    else:
                        self._rolling_max = max(self.MIN_GAIN, self._rolling_max * self.AUTO_GAIN_DECAY)

                    # Normalize all bands between 0.0 and 1.0
                    normalized = [min(1.0, b / self._rolling_max) for b in bands]
                    
                    self.fftReady.emit(normalized)

        except Exception as e:
            logger.error(f"FFT Audio capture failed: {e}")

    def stop(self):
        self._running = False

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
# UI Helpers
# -------------------------------------------------------------------
def show_startup_error(message: str):
    """Shows a popup for fatal startup errors only, logs it, and prevents silent startup failure."""
    logger.critical(f"Startup Error: {message}")
    msg = QMessageBox()
    msg.setIcon(QMessageBox.Critical)
    msg.setWindowTitle("TaskBarSpot - Startup Error")
    msg.setText("TaskBarSpot failed to start.")
    msg.setInformativeText(message)
    msg.exec_()

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
                show_startup_error("Failed to launch Spotify within the timeout.")
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
        show_startup_error(f"Required file not found:\n{e}")
        sys.exit(1)
    except Exception as e:
        show_startup_error(f"An unexpected error occurred during startup:\n{e}")
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
            overlay.set_track_info(prefetched_next.get("title", ""), prefetched_next.get("artist", ""))
        threading.Thread(target=client.run_cli, args=("next",), daemon=True).start()

    def handle_previous():
        logger.info("Optimistic Previous Track (Swiped/Clicked)")
        if history_stack:
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

    # Replace the old artistClicked lambda with this handler:
    def handle_artist_click(uri_target):
        open_link(uri_target if uri_target else link_state.get('artist_uri'))

    overlay.albumDoubleClicked.connect(lambda: open_link(link_state.get('album_uri')))
    overlay.artistClicked.connect(handle_artist_click) # <--- UPDATED
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
        
        # --- ADD THIS ---
        if 'artists' in links and links['artists']:
            overlay.set_artists(links['artists'])
        
    def on_next_ready(meta: dict):
        nonlocal prefetched_next
        prefetched_next = meta

    bg_bridge.metaReady.connect(on_meta_ready)
    bg_bridge.nextReady.connect(on_next_ready)

    def fetch_heavy_metadata_bg():
        """Fetches active metadata and looks one step ahead into the queue natively."""
        uri = client.now_playing()
        saved = False
        links = {'artist_uri': '', 'album_uri': '', 'artists': []} # <--- UPDATE THIS LINE
        
        if uri:
            saved = client.is_track_saved(uri) or False
            meta = client.lookup(uri)
            if meta:
                links['artist_uri'] = meta.get("artist_uri", "")
                links['album_uri'] = meta.get("album_uri", "")
                links['artists'] = meta.get("artists", [])         # <--- ADD THIS LINE
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
        
        cache_key = f"{title}|{artist}"
        thumb = info.get("thumbnail", b"")
        
        if thumb:
            album_byte_cache[cache_key] = thumb
        elif cache_key in album_byte_cache:
            thumb = album_byte_cache[cache_key]
        
        if history_stack and history_stack[-1].get("title") == title:
            history_stack.pop()
        elif current_track_info and current_track_info.get("title") != title:
            if not history_stack or history_stack[-1].get("title") != current_track_info.get("title"):
                history_stack.append(current_track_info)
                if len(history_stack) > 20:
                    history_stack.pop(0)

        current_track_info = info
        current_track_start = time.time()
        prefetched_next = None
        
        overlay.set_track_info(title, artist, QByteArray(thumb) if thumb else QByteArray())
        overlay.set_playing(True)
        state['playing'] = True
        
        threading.Thread(target=fetch_heavy_metadata_bg, daemon=True).start()

    track_bridge.trackChanged.connect(on_track_changed)
    
    monitor = SpotifyTrackMonitor(track_bridge.on_track_change)
    monitor.start()

    # -------------------------------------------------------------------
    # True FFT Audio Polling (30 FPS)
    # -------------------------------------------------------------------
    peak_worker = AudioFFTWorker()
    peak_thread = QThread()
    peak_worker.moveToThread(peak_thread)
    peak_thread.started.connect(peak_worker.run)
    peak_thread.start()
    
    peak_worker.fftReady.connect(overlay.set_fft_bands)

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
        peak_worker.stop()
        peak_thread.quit()
        peak_thread.wait()
        worker.stop()
        worker_thread.quit()
        worker_thread.wait()
        monitor.stop()
        
    app.aboutToQuit.connect(cleanup)

    logger.info("Application started")
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()