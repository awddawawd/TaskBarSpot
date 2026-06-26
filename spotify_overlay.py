"""
SpotifyOverlay – a frameless, always‑on‑top taskbar widget.
Interactive title and artist text with hover carousel scrolling and fade.
On hover, the full text is shown immediately with a fade, then after a delay it scrolls.
Supports single‑click to play/pause anywhere, swipe left/right to skip tracks,
and double‑click on album / title / artist for dedicated actions.
Includes smooth crossfade when album art changes.
"""

import sys
import os
import time
from PyQt5.QtCore import (
    Qt, QRectF, QPropertyAnimation, QEasingCurve, pyqtProperty,
    QTimer, QPauseAnimation, QSequentialAnimationGroup, QParallelAnimationGroup,
    QByteArray, pyqtSignal, QPointF
)
from PyQt5.QtGui import (
    QPainter, QPainterPath, QColor, QBrush, QRegion, QCursor, QFont,
    QPixmap, QTransform, QFontDatabase, QImage, QFontMetrics, QIcon, QPen,
    QLinearGradient
)
from PyQt5.QtSvg import QSvgRenderer
from PyQt5.QtWidgets import (
    QApplication, QWidget, QSystemTrayIcon, QMenu, QAction
)


# ============== Helper for PyInstaller asset paths ==============
def resource_path(relative_path):
    """Get absolute path to a resource – works both in dev and inside PyInstaller."""
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


class SpotifyOverlay(QWidget):
    """
    Taskbar overlay widget for displaying Spotify track info and controls.
    Connect to its signals and update its state via the provided setter methods.
    """

    # ---------- Signals ----------
    playPauseClicked = pyqtSignal()
    nextClicked = pyqtSignal()
    previousClicked = pyqtSignal()
    likeClicked = pyqtSignal()          # the user toggled the like button
    albumDoubleClicked = pyqtSignal()   # double‑click on album art
    titleClicked = pyqtSignal()         # double‑click on song title
    artistClicked = pyqtSignal()        # double‑click on artist name
    volumeChanged = pyqtSignal(float)   # right‑click drag volume change
    settingsClicked = pyqtSignal()      # tray menu "Settings..."

    # ---------- Window constants ----------
    WIDTH = 400
    MAIN_ALPHA = 1

    PARENT_R, PARENT_G, PARENT_B, PARENT_ALPHA = 255, 255, 255, 0
    MARGIN = 8

    CHILD_WIDTH = 200
    CHILD_HEIGHT = 3
    CHILD_R, CHILD_G, CHILD_B, CHILD_ALPHA = 255, 255, 255, 128
    CHILD_RADIUS = 2

    REST_CHILD_WIDTH = 60
    REST_CHILD_R, REST_CHILD_G, REST_CHILD_B, REST_CHILD_ALPHA = 255, 255, 255, 50

    CIRCLE_DIAMETER = 6
    CIRCLE_R, CIRCLE_G, CIRCLE_B, CIRCLE_ALPHA = 255, 255, 255, 128
    CIRCLE_RADIUS = 3

    ACTIVATION_ZONE_HEIGHT = 5
    HOVER_DURATION = 150
    MORPH_DURATION = 150
    RISE_DURATION = 200

    SNAP_THRESHOLD = 1
    RISE_OVERLAP_THRESHOLD = 0.6

    ICON_COLOR = QColor(255, 255, 255)

    BUTTON_SPACING = 14
    BUTTON_BASE_SIZE = 28
    BUTTON_PADDING = 6
    BUTTON_RIGHT_MARGIN = 14
    BUTTON_ALPHA_INACTIVE = 0.9
    BUTTON_ALPHA_HOVER = 1.0

    BUTTONS = [
        ("previous", 0.4),
        ("play",    0.7),
        ("next",    0.4),
        ("like",    0.5),
    ]

    BTN_ANIM_SPEED = 0.25
    CACHE_SCALE = 2

    ALBUM_DOUBLE_CLICK_THRESHOLD = 0.2   # seconds for manual double‑click detection

    VOLUME_BAR_WIDTH = 100
    VOLUME_BAR_HEIGHT = 2
    VOLUME_BAR_RADIUS = 1
    VOLUME_BAR_COLOR = QColor(255, 255, 255, 180)
    VOLUME_BAR_BG_COLOR = QColor(255, 255, 255, 60)

    # ---------- Text layout constants ----------
    ALBUM_SIZE_RANGE = (10, 40)          # min / max album art size
    ALBUM_LEFT_MARGIN = 8
    TEXT_LEFT_MARGIN = 12               # distance from album to text
    TITLE_FONT_SIZE = 11
    ARTIST_FONT_SIZE = 9
    ARTIST_COLOR_NORMAL = QColor(160, 160, 160)
    ARTIST_COLOR_HOVER = QColor(255, 255, 255)

    # ---------- Carousel constants ----------
    CAROUSEL_DELAY = 0.2               # seconds before scroll starts
    CAROUSEL_END_WAIT = 2.0            # seconds wait at the end
    CAROUSEL_SPEED = 50                # pixels per second

    # ---------- Fade effect ----------
    FADE_WIDTH = 30                    # width of the gradient fade on the right edge

    # ---------- Swipe constants ----------
    SWIPE_THRESHOLD_PX = 80            # minimum drag distance to trigger skip (live threshold)
    SWIPE_VELOCITY_THRESHOLD = 200     # pixels/second (fallback flick)
    SWIPE_DEAD_ZONE = 10               # movement before a press becomes a swipe
    SWIPE_SNAP_DURATION = 250          # ms for snap-back animation
    POST_SWIPE_DELAY_MS = 1000

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)

        # ---------- Internal state ----------
        self._hover_progress = 0.0
        self._morph_progress = 0.0
        self._rise_progress = 0.0
        self._expanded = False

        self._is_playing = False
        self._is_liked = False
        self._current_volume = 0.5
        self._anim_volume = 0.5

        self._song_title = "Song Title"
        self._artist = "Artist Name"
        self._album_pixmap = None
        self._old_album_pixmap = None          # previous cover for crossfade
        self._album_crossfade = 1.0            # 1.0 = fully transitioned to new cover

        # Text hover states
        self._hovering_title = False
        self._hovering_artist = False
        self._title_rect = QRectF()      # set during paint
        self._artist_rect = QRectF()

        # Button interaction
        self._hovered_btn_index = -1
        self._pressed_btn_index = -1
        self._btn_states = []
        for _ in self.BUTTONS:
            self._btn_states.append({
                'current_alpha': self.BUTTON_ALPHA_INACTIVE,
                'target_alpha': self.BUTTON_ALPHA_INACTIVE,
                'last_change': time.time(),
                'press_time': 0.0
            })

        # Double‑click timers (manual time tracking)
        self._last_album_click_time = 0.0
        self._last_title_click_time = 0.0
        self._last_artist_click_time = 0.0

        # Right‑click volume drag
        self._right_dragging = False
        self._right_drag_start_x = 0
        self._drag_start_volume = 0.5

        # ---------- Swipe / single‑click state ----------
        self._swipe_offset = 0.0
        self._swipe_active = False
        self._swipe_start_pos = QPointF()
        self._swipe_last_pos = QPointF()
        self._swipe_last_time = 0.0
        self._swipe_velocity = 0.0
        self._swipe_anim = None          # keep reference to avoid garbage collection
        self._swipe_triggered = False    # True if the action already fired mid‑swipe
        self._post_swipe_delay = False   # 1‑second grace period after swipe

        self._press_region = None          # 'album', 'title', 'artist', 'background'
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.timeout.connect(self._on_single_click_timer)

        # Screen geometry
        screen = QApplication.primaryScreen()
        self.screen_geom = screen.geometry()
        avail = screen.availableGeometry()
        self.taskbar_height = self.screen_geom.bottom() - avail.bottom()
        self.window_height = self.taskbar_height + 1
        self.PANEL_HEIGHT = max(4, self.taskbar_height - 2)

        # Position at bottom center
        x = int(self.screen_geom.center().x() - self.WIDTH // 2)
        y = int(self.screen_geom.bottom() - self.window_height + 1)
        self.setGeometry(x, y, self.WIDTH, self.window_height)
        self.setMask(QRegion(self.rect()))

        # ---------- Fonts ----------
        font_id_black = QFontDatabase.addApplicationFont(
            resource_path("assets/fonts/circular-black.otf"))
        if font_id_black != -1:
            families = QFontDatabase.applicationFontFamilies(font_id_black)
            self._circular_black_family = families[0] if families else "Circular Black"
        else:
            self._circular_black_family = "Circular"

        font_id_book = QFontDatabase.addApplicationFont(
            resource_path("assets/fonts/circular-medium.otf"))
        if font_id_book != -1:
            families = QFontDatabase.applicationFontFamilies(font_id_book)
            self._circular_book_family = families[0] if families else "Circular Book"
        else:
            self._circular_book_family = "Circular"

        # ---------- Pre‑compute constant text layout ----------
        self._panel_album_size = int(max(10, min(self.PANEL_HEIGHT - 6, 40)))
        self._text_x_offset = self.ALBUM_LEFT_MARGIN + self._panel_album_size + self.TEXT_LEFT_MARGIN
        
        # Dynamically calculate the total pixel width of the playback buttons
        buttons_w = sum(int(self.BUTTON_BASE_SIZE * scale) for _, scale in self.BUTTONS)
        spacing_w = self.BUTTON_SPACING * (len(self.BUTTONS) - 1)
        self._controls_width = buttons_w + spacing_w
        
        # 16 is an arbitrary padding value so the text doesn't touch the first button's bounding box
        text_to_button_padding = 16 
        
        self._max_text_width = (
            self.WIDTH 
            - self._text_x_offset 
            - self._controls_width 
            - self.BUTTON_RIGHT_MARGIN 
            - text_to_button_padding
        )
        # Full widths of strings (updated on track change)
        self._title_full_width = 0.0
        self._artist_full_width = 0.0

        # ---------- Carousel state ----------
        def new_carousel_state():
            return {
                'state': 'idle',           # idle, waiting, scrolling, wait_end
                'hover_start_time': 0.0,
                'scroll_start_time': 0.0,  # when the scrolling actually starts
                'wait_end_start_time': 0.0,
                'scroll_offset': 0.0,
            }
        self._title_car = new_carousel_state()
        self._artist_car = new_carousel_state()

        # ---------- SVG icons ----------
        self._icons = {}
        svg_files = {
            "play": "play.svg",
            "pause": "pause.svg",
            "like": "like.svg",
            "liked": "liked.svg",
            "next": "next.svg",
        }
        button_scales = {
            "play": 0.7,
            "pause": 0.7,
            "like": 0.5,
            "liked": 0.5,
            "next": 0.4,
        }

        for key, filename in svg_files.items():
            path = resource_path(f"assets/svgs/{filename}")
            renderer = QSvgRenderer(path)
            scale = button_scales.get(key, 1.0)
            nominal_size = int(self.BUTTON_BASE_SIZE * scale)
            physical_size = nominal_size * self.CACHE_SCALE

            pix = QPixmap(physical_size, physical_size)
            pix.fill(Qt.transparent)
            painter = QPainter(pix)
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            renderer.render(painter, QRectF(0, 0, physical_size, physical_size))
            painter.end()

            recolored = self._recolor_pixmap(pix, self.ICON_COLOR)
            recolored.setDevicePixelRatio(self.CACHE_SCALE)
            self._icons[key] = recolored

        # Previous = flipped next
        next_pix = self._icons["next"]
        flip = QTransform().scale(-1, 1)
        prev_pix = next_pix.transformed(flip, Qt.SmoothTransformation)
        prev_pix.setDevicePixelRatio(self.CACHE_SCALE)
        self._icons["previous"] = prev_pix

        # Initialize text cache correctly at startup
        self._compute_full_widths()

        # ---------- Pre‑rendered static cache (background + album art only) ----------
        self._create_static_cache()

        # ---------- Timers ----------
        self._btn_anim_timer = QTimer(self)
        self._btn_anim_timer.timeout.connect(self._update_button_animations)
        self._btn_anim_timer.start(33)

        self._vol_anim_timer = QTimer(self)
        self._vol_anim_timer.timeout.connect(self._smooth_volume_step)
        self._vol_anim_timer.start(30)

        # Increased frequency for buttery smooth carousel
        self._carousel_timer = QTimer(self)
        self._carousel_timer.timeout.connect(self._update_carousels)
        self._carousel_timer.start(16)   # ~60 fps

        # Keep window above taskbar
        self._raise_timer = QTimer(self)
        self._raise_timer.timeout.connect(self.raise_)
        self._raise_timer.start(50)

        self.show()

        # ---------- System tray icon ----------
        self.tray_icon = QSystemTrayIcon(self)
        icon_path = resource_path("assets/icon.ico")
        if QPixmap(icon_path).isNull():
            pix = QPixmap(16, 16)
            pix.fill(QColor(255, 255, 255))
            self.tray_icon.setIcon(QIcon(pix))
        else:
            self.tray_icon.setIcon(QIcon(icon_path))
        self.tray_icon.setToolTip("TaskBarSpot")

        tray_menu = QMenu()
        settings_action = QAction("Settings...", self)
        settings_action.triggered.connect(self.settingsClicked.emit)
        tray_menu.addAction(settings_action)
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self._quit_app)
        tray_menu.addAction(exit_action)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.show()

    # ---------- Public update slots ----------
    def set_playing(self, playing: bool):
        self._is_playing = playing
        self.update()

    def set_liked(self, liked: bool):
        self._is_liked = liked
        self.update()

    def set_track_info(self, title: str, artist: str, image_bytes: QByteArray = QByteArray()):
        if title:
            self._song_title = title
        else:
            self._song_title = "Unknown Title"
        self._artist = artist if artist else ""

        # Save current pixmap as old for crossfade
        self._old_album_pixmap = self._album_pixmap

        if not image_bytes.isEmpty():
            img = QImage.fromData(image_bytes)
            if not img.isNull():
                self._album_pixmap = QPixmap.fromImage(img)
            else:
                self._album_pixmap = None
        else:
            self._album_pixmap = None

        self._compute_full_widths()
        self._title_car['state'] = 'idle'
        self._artist_car['state'] = 'idle'

        # Trigger the crossfade animation (250ms duration)
        self._album_crossfade = 0.0
        
        # Stop any currently running fade if the user is swiping super fast
        if hasattr(self, '_fade_anim') and self._fade_anim.state() == QPropertyAnimation.Running:
            self._fade_anim.stop()

        self._fade_anim = QPropertyAnimation(self, b"album_crossfade")
        self._fade_anim.setDuration(250)
        self._fade_anim.setStartValue(0.0)  # <-- Added this!
        self._fade_anim.setEndValue(1.0)    # <-- Added this!
        self._fade_anim.setEasingCurve(QEasingCurve.InOutQuad)
        self._fade_anim.start()

        self._create_static_cache()
        self.update()

    def set_volume(self, volume: float):
        self._current_volume = max(0.0, min(1.0, volume))

    # ---------- Private helpers ----------
    def _recolor_pixmap(self, pixmap, color):
        result = QPixmap(pixmap.size())
        result.fill(Qt.transparent)
        painter = QPainter(result)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        painter.fillRect(result.rect(), color)
        painter.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        painter.drawPixmap(0, 0, pixmap)
        painter.end()
        return result

    def _truncate_text(self, painter, text, max_width):
        if not text or max_width <= 0:
            return ""
        fm = QFontMetrics(painter.font())
        if fm.width(text) <= max_width:
            return text
        low, high = 0, len(text)
        best = 0
        while low <= high:
            mid = (low + high) // 2
            candidate = text[:mid] + "..."
            if fm.width(candidate) <= max_width:
                best = mid
                low = mid + 1
            else:
                high = mid - 1
        return text[:best] + "..." if best > 0 else "..."

    def _compute_full_widths(self):
        title_font = QFont(self._circular_black_family, self.TITLE_FONT_SIZE)
        title_font.setStyleStrategy(QFont.PreferAntialias | QFont.PreferQuality)
        title_font.setHintingPreference(QFont.PreferNoHinting)
        title_fm = QFontMetrics(title_font)
        self._title_full_width = title_fm.width(self._song_title)

        artist_font = QFont(self._circular_book_family, self.ARTIST_FONT_SIZE)
        artist_font.setStyleStrategy(QFont.PreferAntialias | QFont.PreferQuality)
        artist_font.setHintingPreference(QFont.PreferNoHinting)
        artist_fm = QFontMetrics(artist_font)
        self._artist_full_width = artist_fm.width(self._artist) if self._artist else 0

    def _create_static_cache(self):
        cache_w = self.WIDTH * self.CACHE_SCALE
        cache_h = self.PANEL_HEIGHT * self.CACHE_SCALE
        self._static_cache = QPixmap(cache_w, cache_h)
        self._static_cache.setDevicePixelRatio(self.CACHE_SCALE)
        self._static_cache.fill(Qt.transparent)

        painter = QPainter(self._static_cache)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        self._draw_static_content(painter, QRectF(0, 0, self.WIDTH, self.PANEL_HEIGHT), 255)
        painter.end()

    def _draw_static_content(self, painter, rect, alpha):
        x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
        if h <= 0:
            return
        album_size = self._panel_album_size
        album_x = x + self.ALBUM_LEFT_MARGIN
        album_y = y + (h - album_size) // 2
        album_rect = QRectF(album_x, album_y, album_size, album_size)

        path = QPainterPath()
        path.addRoundedRect(album_rect, 4, 4)
        painter.save()
        painter.setClipPath(path)

        # Helper to scale correctly
        def get_scaled(pix):
            size = int(album_size * self.CACHE_SCALE)
            s = pix.scaled(size, size, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            s.setDevicePixelRatio(self.CACHE_SCALE)
            return s

        # Crossfade: if animation not finished and old image exists
        if self._album_crossfade < 1.0 and self._old_album_pixmap and not self._old_album_pixmap.isNull():
            # Draw old image fading out
            painter.setOpacity((1.0 - self._album_crossfade) * (alpha / 255.0))
            painter.drawPixmap(album_rect.topLeft(), get_scaled(self._old_album_pixmap))

            # Draw new image fading in
            if self._album_pixmap and not self._album_pixmap.isNull():
                painter.setOpacity(self._album_crossfade * (alpha / 255.0))
                painter.drawPixmap(album_rect.topLeft(), get_scaled(self._album_pixmap))
        else:
            # Normal drawing (no old image or animation complete)
            painter.setOpacity(alpha / 255.0)
            if self._album_pixmap and not self._album_pixmap.isNull():
                painter.drawPixmap(album_rect.topLeft(), get_scaled(self._album_pixmap))

        painter.restore()

    # ---------- Text drawing (with immediate full text on hover) ----------
    # (Unchanged from previous version)
    def _draw_text_overlay(self, painter: QPainter, panel_rect: QRectF, alpha: int):
        x, y, w, h = panel_rect.x(), panel_rect.y(), panel_rect.width(), panel_rect.height()
        if h <= 0:
            return

        text_x = x + self._text_x_offset
        max_text_width = self._max_text_width

        title_font = QFont(self._circular_black_family, self.TITLE_FONT_SIZE)
        title_font.setStyleStrategy(QFont.PreferAntialias | QFont.PreferQuality)
        title_font.setHintingPreference(QFont.PreferNoHinting)

        artist_font = QFont(self._circular_book_family, self.ARTIST_FONT_SIZE)
        artist_font.setStyleStrategy(QFont.PreferAntialias | QFont.PreferQuality)
        artist_font.setHintingPreference(QFont.PreferNoHinting)

        painter.save()

        painter.setFont(title_font)
        title_fm = QFontMetrics(title_font)
        title_height = title_fm.height()

        painter.setFont(artist_font)
        artist_fm = QFontMetrics(artist_font)
        artist_height = artist_fm.height()

        spacing = 2
        total_text_height = title_height + spacing + artist_height
        start_y = y + (h - total_text_height) // 2

        # ---- Title ----
        painter.setFont(title_font)
        truncated_title = self._truncate_text(painter, self._song_title, max_text_width)

        if self._title_full_width > max_text_width:
            actual_title_width = max_text_width
        else:
            actual_title_width = title_fm.width(self._song_title)

        title_visible_rect = QRectF(text_x, start_y, actual_title_width, title_height)
        self._title_rect = title_visible_rect

        title_color = QColor(255, 255, 255, alpha)

        car = self._title_car
        show_full = (self._title_full_width > max_text_width and car['state'] != 'idle')
        if show_full:
            if car['state'] == 'scrolling':
                elapsed = time.time() - car['scroll_start_time']
                max_offset = self._title_full_width - max_text_width + self.FADE_WIDTH
                offset = min(max_offset, elapsed * self.CAROUSEL_SPEED)
            elif car['state'] == 'wait_end':
                offset = self._title_full_width - max_text_width + self.FADE_WIDTH
            else:
                offset = 0.0
        else:
            offset = 0.0

        if show_full:
            painter.save()
            painter.setClipRect(title_visible_rect)

            gradient = QLinearGradient(
                title_visible_rect.right() - self.FADE_WIDTH, 0,
                title_visible_rect.right(), 0
            )
            transparent_color = QColor(title_color)
            transparent_color.setAlpha(0)
            gradient.setColorAt(0, title_color)
            gradient.setColorAt(1, transparent_color)

            pen = QPen()
            pen.setBrush(QBrush(gradient))
            painter.setPen(pen)

            text_rect = QRectF(text_x - offset, start_y, self._title_full_width, title_height)
            painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter, self._song_title)
            painter.restore()
        else:
            painter.setPen(title_color)
            painter.drawText(title_visible_rect, Qt.AlignLeft | Qt.AlignVCenter, truncated_title)

        # Underline (scrolls with text, fades too)
        if self._hovering_title:
            underline_y = int(title_visible_rect.bottom()) - 2
            painter.save()
            painter.setRenderHint(QPainter.Antialiasing, False)
            if show_full:
                painter.setClipRect(title_visible_rect)
                gradient = QLinearGradient(
                    title_visible_rect.right() - self.FADE_WIDTH, 0,
                    title_visible_rect.right(), 0
                )
                transparent_color = QColor(title_color)
                transparent_color.setAlpha(0)
                gradient.setColorAt(0, title_color)
                gradient.setColorAt(1, transparent_color)
                pen = QPen()
                pen.setBrush(QBrush(gradient))
                pen.setWidth(1)
                painter.setPen(pen)
                line_start_x = text_x - offset
                line_end_x = text_x - offset + self._title_full_width
                painter.drawLine(
                    QPointF(line_start_x, underline_y),
                    QPointF(line_end_x, underline_y)
                )
            else:
                painter.setPen(QPen(title_color, 1))
                painter.drawLine(
                    QPointF(title_visible_rect.left(), underline_y),
                    QPointF(title_visible_rect.right(), underline_y)
                )
            painter.restore()

        # ---- Artist ----
        if self._artist:
            painter.setFont(artist_font)
            truncated_artist = self._truncate_text(painter, self._artist, max_text_width)

            if self._artist_full_width > max_text_width:
                actual_artist_width = max_text_width
            else:
                actual_artist_width = artist_fm.width(self._artist)

            artist_visible_rect = QRectF(text_x, start_y + title_height + spacing,
                                         actual_artist_width, artist_height)
            self._artist_rect = artist_visible_rect

            artist_color = self.ARTIST_COLOR_HOVER if self._hovering_artist else QColor(
                self.ARTIST_COLOR_NORMAL.red(),
                self.ARTIST_COLOR_NORMAL.green(),
                self.ARTIST_COLOR_NORMAL.blue(),
                alpha
            )

            car = self._artist_car
            show_full_artist = (self._artist_full_width > max_text_width and car['state'] != 'idle')
            if show_full_artist:
                if car['state'] == 'scrolling':
                    elapsed = time.time() - car['scroll_start_time']
                    max_offset = self._artist_full_width - max_text_width + self.FADE_WIDTH
                    offset = min(max_offset, elapsed * self.CAROUSEL_SPEED)
                elif car['state'] == 'wait_end':
                    offset = self._artist_full_width - max_text_width + self.FADE_WIDTH
                else:
                    offset = 0.0
            else:
                offset = 0.0

            if show_full_artist:
                painter.save()
                painter.setClipRect(artist_visible_rect)

                gradient = QLinearGradient(
                    artist_visible_rect.right() - self.FADE_WIDTH, 0,
                    artist_visible_rect.right(), 0
                )
                transparent_artist = QColor(artist_color)
                transparent_artist.setAlpha(0)
                gradient.setColorAt(0, artist_color)
                gradient.setColorAt(1, transparent_artist)

                pen = QPen()
                pen.setBrush(QBrush(gradient))
                painter.setPen(pen)

                text_rect = QRectF(text_x - offset, start_y + title_height + spacing,
                                   self._artist_full_width, artist_height)
                painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter, self._artist)
                painter.restore()
            else:
                painter.setPen(artist_color)
                painter.drawText(artist_visible_rect, Qt.AlignLeft | Qt.AlignVCenter, truncated_artist)
        else:
            self._artist_rect = QRectF()

        painter.restore()

    def _get_album_rect(self) -> QRectF:
        w, h = self.width(), self.height()
        pr = self._rise_progress
        panel_height = self.PANEL_HEIGHT
        offset = panel_height * pr
        panel_y = h - offset
        album_size = self._panel_album_size
        album_x = self.ALBUM_LEFT_MARGIN
        album_y = panel_y + (panel_height - album_size) // 2
        return QRectF(album_x, album_y, album_size, album_size)

    # ---------- Animated properties ----------
    def _get_hover_progress(self): return self._hover_progress
    def _set_hover_progress(self, v): self._hover_progress = v; self.update()
    hover_progress = pyqtProperty(float, _get_hover_progress, _set_hover_progress)

    def _animate_hover(self, target):
        if hasattr(self, '_anim_hover') and self._anim_hover and self._anim_hover.state() == QPropertyAnimation.Running:
            self._anim_hover.stop()
        self._anim_hover = QPropertyAnimation(self, b"hover_progress")
        self._anim_hover.setDuration(self.HOVER_DURATION)
        self._anim_hover.setEasingCurve(QEasingCurve.OutCubic)
        self._anim_hover.setStartValue(self._hover_progress)
        self._anim_hover.setEndValue(target)
        self._anim_hover.start()

    def _get_morph_progress(self): return self._morph_progress
    def _set_morph_progress(self, v): self._morph_progress = v; self.update()
    morph_progress = pyqtProperty(float, _get_morph_progress, _set_morph_progress)

    def _get_rise_progress(self): return self._rise_progress
    def _set_rise_progress(self, v): self._rise_progress = v; self.update()
    rise_progress = pyqtProperty(float, _get_rise_progress, _set_rise_progress)

    def _get_swipe_offset(self): return self._swipe_offset
    def _set_swipe_offset(self, v): self._swipe_offset = v; self.update()
    swipe_offset = pyqtProperty(float, _get_swipe_offset, _set_swipe_offset)

    def _get_album_crossfade(self):
        return self._album_crossfade

    def _set_album_crossfade(self, v):
        self._album_crossfade = v
        self._create_static_cache()
        self.update()

    album_crossfade = pyqtProperty(float, _get_album_crossfade, _set_album_crossfade)

    def _animate_morph(self, target):
        if hasattr(self, '_anim_group') and self._anim_group and self._anim_group.state() == QPropertyAnimation.Running:
            if getattr(self, '_morph_target', None) == target:
                return
            self._anim_group.stop()
        self._morph_target = target

        morph = QPropertyAnimation(self, b"morph_progress")
        morph.setDuration(self.MORPH_DURATION)
        morph.setEasingCurve(QEasingCurve.OutCubic)
        morph.setStartValue(self._morph_progress)
        morph.setEndValue(target)

        rise = QPropertyAnimation(self, b"rise_progress")
        rise.setDuration(self.RISE_DURATION)
        rise.setEasingCurve(QEasingCurve.OutCubic)
        rise.setStartValue(self._rise_progress)
        rise.setEndValue(1.0 if target > self._morph_progress else 0.0)

        delay_ms = int(self.MORPH_DURATION * self.RISE_OVERLAP_THRESHOLD)
        delay = QPauseAnimation(delay_ms)
        rise_seq = QSequentialAnimationGroup()
        rise_seq.addAnimation(delay)
        rise_seq.addAnimation(rise)

        self._anim_group = QParallelAnimationGroup(self)
        self._anim_group.addAnimation(morph)
        self._anim_group.addAnimation(rise_seq)
        self._anim_group.start()

    # ---------- Volume smoothing ----------
    def _smooth_volume_step(self):
        if self._right_dragging:
            return
        speed = 0.4
        if abs(self._anim_volume - self._current_volume) < 0.002:
            self._anim_volume = self._current_volume
            return
        self._anim_volume += (self._current_volume - self._anim_volume) * speed
        self.update()

    # ---------- Carousel state machine ----------
    def _update_carousels(self):
        now = time.time()
        changed = False

        # Title carousel
        car = self._title_car
        max_vis = self._max_text_width
        full_w = self._title_full_width
        if full_w > max_vis:
            state = car['state']
            if state == 'waiting':
                if now - car['hover_start_time'] >= self.CAROUSEL_DELAY:
                    car['state'] = 'scrolling'
                    car['scroll_start_time'] = now
                    car['scroll_offset'] = 0.0
                    changed = True
            elif state == 'scrolling':
                elapsed = now - car['scroll_start_time']
                max_offset = full_w - max_vis + self.FADE_WIDTH
                new_offset = min(max_offset, elapsed * self.CAROUSEL_SPEED)
                if abs(new_offset - car['scroll_offset']) > 0.1:
                    car['scroll_offset'] = new_offset
                    changed = True
                if new_offset >= max_offset - 0.1:
                    car['state'] = 'wait_end'
                    car['wait_end_start_time'] = now
                    car['scroll_offset'] = max_offset
                    changed = True
            elif state == 'wait_end':
                if now - car['wait_end_start_time'] >= self.CAROUSEL_END_WAIT:
                    car['state'] = 'idle'
                    car['scroll_offset'] = 0.0
                    changed = True
        else:
            if car['state'] != 'idle':
                car['state'] = 'idle'
                car['scroll_offset'] = 0.0
                changed = True

        # Artist carousel (identical logic)
        car = self._artist_car
        full_w = self._artist_full_width
        if full_w > max_vis:
            state = car['state']
            if state == 'waiting':
                if now - car['hover_start_time'] >= self.CAROUSEL_DELAY:
                    car['state'] = 'scrolling'
                    car['scroll_start_time'] = now
                    car['scroll_offset'] = 0.0
                    changed = True
            elif state == 'scrolling':
                elapsed = now - car['scroll_start_time']
                max_offset = full_w - max_vis
                new_offset = min(max_offset, elapsed * self.CAROUSEL_SPEED)
                if abs(new_offset - car['scroll_offset']) > 0.1:
                    car['scroll_offset'] = new_offset
                    changed = True
                if new_offset >= max_offset - 0.1:
                    car['state'] = 'wait_end'
                    car['wait_end_start_time'] = now
                    car['scroll_offset'] = max_offset
                    changed = True
            elif state == 'wait_end':
                if now - car['wait_end_start_time'] >= self.CAROUSEL_END_WAIT:
                    car['state'] = 'idle'
                    car['scroll_offset'] = 0.0
                    changed = True
        else:
            if car['state'] != 'idle':
                car['state'] = 'idle'
                car['scroll_offset'] = 0.0
                changed = True

        if changed:
            self.update()

    # ---------- Single‑click / swipe helpers ----------
    def _on_single_click_timer(self):
        """Called when no second click arrives – treat as a single click → toggle play/pause."""
        print("[SpotifyOverlay] Single-click detected: Toggling Play/Pause.")
        self.playPauseClicked.emit()
        self._press_region = None

    def _start_single_click_detection(self, region: str):
        """Start the timer that will emit playPauseClicked after a double‑click timeout."""
        timeout_ms = int(self.ALBUM_DOUBLE_CLICK_THRESHOLD * 1000)
        self._click_timer.start(timeout_ms)

    def _cancel_single_click_detection(self):
        self._click_timer.stop()
        # Do NOT clear self._press_region here – that would kill the swipe tracking.

    def _region_from_pos(self, pos):
        """Return which clickable region the point falls into."""
        if self._get_album_rect().contains(pos):
            return 'album'
        if self._title_rect.isValid() and self._title_rect.contains(pos):
            return 'title'
        if self._artist_rect.isValid() and self._artist_rect.contains(pos):
            return 'artist'
        return 'background'

    def _collapse_panel(self):
        """Helper to hide the panel and reset hover/carousel states."""
        self._animate_hover(0.0)
        if self._expanded:
            self._expanded = False
            self._animate_morph(0.0)
            self._hovered_btn_index = -1
            self._update_button_targets()
            self.setCursor(Qt.ArrowCursor)

            self._hovering_title = False
            self._hovering_artist = False
            self._title_car['state'] = 'idle'
            self._title_car['scroll_offset'] = 0.0
            self._artist_car['state'] = 'idle'
            self._artist_car['scroll_offset'] = 0.0
            self.update()

    def _check_delayed_collapse(self):
        """Called 1 second after releasing a swipe outside the boundaries."""
        self._post_swipe_delay = False
        
        # Check where the cursor currently is globally
        pos = self.mapFromGlobal(QCursor.pos())
        if not self.rect().contains(pos) and not getattr(self, '_right_dragging', False):
            self._collapse_panel()

    # ---------- Mouse events ----------
    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self._right_dragging = True
            self._right_drag_start_x = event.x()
            self._drag_start_volume = self._current_volume
            self.update()
            return

        if event.button() == Qt.LeftButton and self._expanded:   # no more _rise_progress check
            pos = event.pos()

            # Buttons – keep the immediate action
            idx = self._button_index_at(pos)
            if idx >= 0:
                self._pressed_btn_index = idx
                self._btn_states[idx]['target_alpha'] = 0.5
                self._btn_states[idx]['last_change'] = time.time()
                QTimer.singleShot(100, lambda i=idx: self._on_press_release(i))

                final_key = self._resolve_icon_key(idx)
                if final_key in ("play", "pause"):
                    self.playPauseClicked.emit()
                elif final_key == "next":
                    self.nextClicked.emit()
                elif final_key == "previous":
                    self.previousClicked.emit()
                elif final_key in ("like", "liked"):
                    self.likeClicked.emit()
                self.update()
                return

            # ---- Double Click Detection (Manual Time Tracking) ----
            now = time.time()
            region = self._region_from_pos(pos)
            is_double_click = False

            if region == 'album':
                if now - self._last_album_click_time <= self.ALBUM_DOUBLE_CLICK_THRESHOLD:
                    self.albumDoubleClicked.emit()
                    self._last_album_click_time = 0.0
                    is_double_click = True
                else:
                    self._last_album_click_time = now
            elif region == 'title':
                if now - self._last_title_click_time <= self.ALBUM_DOUBLE_CLICK_THRESHOLD:
                    self.titleClicked.emit()
                    self._last_title_click_time = 0.0
                    is_double_click = True
                else:
                    self._last_title_click_time = now
            elif region == 'artist':
                if now - self._last_artist_click_time <= self.ALBUM_DOUBLE_CLICK_THRESHOLD:
                    self.artistClicked.emit()
                    self._last_artist_click_time = 0.0
                    is_double_click = True
                else:
                    self._last_artist_click_time = now

            if is_double_click:
                self._cancel_single_click_detection()
                self._press_region = None
                self.update()
                return

            # ---- Anything else – start swipe tracking ----
            self._press_region = region
            self._swipe_start_pos = pos
            self._swipe_last_pos = pos
            self._swipe_last_time = now
            self._swipe_active = False
            self._swipe_offset = 0.0
            self._swipe_triggered = False

            self.update()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        # 1. Update boundary check: Don't collapse if we are actively dragging/swiping
        is_interacting = (getattr(self, '_swipe_active', False) or
                          getattr(self, '_right_dragging', False) or
                          getattr(self, '_post_swipe_delay', False))
        if self._expanded and not self.rect().contains(event.pos()) and not is_interacting:
            self._collapse_panel()

        if self._right_dragging:
            dx = event.x() - self._right_drag_start_x
            new_vol = max(0.0, min(1.0, self._drag_start_volume + dx * 0.005))
            self._anim_volume = new_vol
            self._current_volume = new_vol
            self.volumeChanged.emit(new_vol)
            self.update()
            return

        pos = event.pos()

        # ---- Swipe logic (Live Threshold) ----
        if (event.buttons() & Qt.LeftButton) and self._press_region is not None:
            delta = pos - self._swipe_start_pos
            if not self._swipe_active:
                if delta.manhattanLength() > self.SWIPE_DEAD_ZONE:
                    self._swipe_active = True
                    self._swipe_last_pos = pos
                    self._swipe_last_time = time.time()

            if self._swipe_active:
                self._swipe_offset = delta.x()
                now = time.time()
                dt = now - self._swipe_last_time
                if dt > 0.001:
                    self._swipe_velocity = (pos.x() - self._swipe_last_pos.x()) / dt
                self._swipe_last_pos = pos
                self._swipe_last_time = now

                # Live threshold trigger during the drag!
                if not self._swipe_triggered:
                    if self._swipe_offset > self.SWIPE_THRESHOLD_PX:
                        self.previousClicked.emit()
                        self._swipe_triggered = True
                    elif self._swipe_offset < -self.SWIPE_THRESHOLD_PX:
                        self.nextClicked.emit()
                        self._swipe_triggered = True

                self.update()
                return

        # ---- Hover, button highlighting, activation (existing code) ----
        if self._expanded:
            self._update_button_hover(pos)

            hovering_title = False
            hovering_artist = False
            if self._title_rect.isValid() and self._title_rect.contains(pos):
                hovering_title = True
            elif self._artist_rect.isValid() and self._artist_rect.contains(pos):
                hovering_artist = True

            if hovering_title != self._hovering_title:
                if hovering_title:
                    if self._title_car['state'] == 'idle':
                        self._title_car['state'] = 'waiting'
                        self._title_car['hover_start_time'] = time.time()
                        self._title_car['scroll_offset'] = 0.0
                else:
                    self._title_car['state'] = 'idle'
                self._hovering_title = hovering_title
                self.update()

            if hovering_artist != self._hovering_artist:
                if hovering_artist:
                    if self._artist_car['state'] == 'idle':
                        self._artist_car['state'] = 'waiting'
                        self._artist_car['hover_start_time'] = time.time()
                else:
                    self._artist_car['state'] = 'idle'
                self._hovering_artist = hovering_artist
                self.update()

            if self._hovered_btn_index >= 0 or hovering_title or hovering_artist or self._get_album_rect().contains(pos):
                self.setCursor(Qt.PointingHandCursor)
            else:
                self.setCursor(Qt.ArrowCursor)
        else:
            self.setCursor(Qt.ArrowCursor)
            cursor_y = QCursor.pos().y()
            if cursor_y >= self.screen_geom.bottom() - self.ACTIVATION_ZONE_HEIGHT:
                self._expanded = True
                self._animate_morph(1.0)

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.RightButton:
            self._right_dragging = False
            self.update()
            return

        if event.button() == Qt.LeftButton:
            # ---- Swipe completion ----
            if getattr(self, '_swipe_active', False):
                self._swipe_active = False
                
                # Fallback: Only check flick velocity if the distance threshold wasn't already triggered
                if not getattr(self, '_swipe_triggered', False):
                    if self._swipe_velocity > self.SWIPE_VELOCITY_THRESHOLD:
                        self.previousClicked.emit()
                    elif self._swipe_velocity < -self.SWIPE_VELOCITY_THRESHOLD:
                        self.nextClicked.emit()

                # Animate back to 0
                self._swipe_anim = QPropertyAnimation(self, b"swipe_offset")
                self._swipe_anim.setDuration(self.SWIPE_SNAP_DURATION)
                self._swipe_anim.setEasingCurve(QEasingCurve.OutCubic)
                self._swipe_anim.setStartValue(self._swipe_offset)
                self._swipe_anim.setEndValue(0.0)
                self._swipe_anim.start()
                
                self._press_region = None
                self._swipe_triggered = False
                
                # Flat 1-second delay after ANY swipe finishes before allowing collapse
                self._post_swipe_delay = True
                QTimer.singleShot(self.POST_SWIPE_DELAY_MS, self._check_delayed_collapse)
                
                return

            # ---- Single-click handling ----
            if self._press_region is not None:
                timeout_ms = int(self.ALBUM_DOUBLE_CLICK_THRESHOLD * 1000)
                self._click_timer.start(timeout_ms)
                self._press_region = None
                return

        super().mouseReleaseEvent(event)

    def leaveEvent(self, event):
        # Prevent collapsing if we are actively interacting/dragging or in the 1‑second grace period
        if (getattr(self, '_swipe_active', False) or
            getattr(self, '_right_dragging', False) or
            getattr(self, '_post_swipe_delay', False)):
            super().leaveEvent(event)
            return

        self._collapse_panel()
        super().leaveEvent(event)

    def _on_press_release(self, idx):
        if self._hovered_btn_index == idx:
            self._btn_states[idx]['target_alpha'] = self.BUTTON_ALPHA_HOVER
        else:
            self._btn_states[idx]['target_alpha'] = self.BUTTON_ALPHA_INACTIVE
        self._btn_states[idx]['last_change'] = time.time()
        self._pressed_btn_index = -1

    def _update_button_hover(self, pos):
        idx = self._button_index_at(pos)
        if idx != self._hovered_btn_index:
            self._hovered_btn_index = idx
            self._update_button_targets()

    def _button_index_at(self, pos):
        if not self._expanded:
            return -1
            
        w, h = self.width(), self.height()
        
        # Calculate actual Y position during animation so buttons are clickable mid-rise
        offset = self.PANEL_HEIGHT * self._rise_progress
        panel_y_top = h - offset
        
        base = self.BUTTON_BASE_SIZE
        spacing = self.BUTTON_SPACING
        padding = self.BUTTON_PADDING
        state_map = self._state_map()

        items = []
        for key, scale in self.BUTTONS:
            final = state_map.get(key, key)
            items.append((final, scale))

        total_w = sum(int(base * s) for _, s in items) + spacing * (len(items) - 1)
        start_x = self.WIDTH - total_w - self.BUTTON_RIGHT_MARGIN
        center_y = panel_y_top + self.PANEL_HEIGHT // 2

        cur_x = start_x
        for idx, (final_key, scale) in enumerate(items):
            nominal_size = int(base * scale)
            hit_rect = QRectF(cur_x - padding, center_y - nominal_size / 2 - padding,
                              nominal_size + 2 * padding, nominal_size + 2 * padding)
            if hit_rect.contains(pos):
                return idx
            cur_x += nominal_size + spacing
        return -1

    def _resolve_icon_key(self, index):
        state_map = self._state_map()
        key, _ = self.BUTTONS[index]
        return state_map.get(key, key)

    def _state_map(self):
        return {
            "play": "play" if not self._is_playing else "pause",
            "like": "like" if not self._is_liked else "liked",
        }

    def _update_button_targets(self):
        now = time.time()
        for i, state in enumerate(self._btn_states):
            if i == self._hovered_btn_index and i != self._pressed_btn_index:
                state['target_alpha'] = self.BUTTON_ALPHA_HOVER
            else:
                state['target_alpha'] = self.BUTTON_ALPHA_INACTIVE
            state['last_change'] = now

    def _update_button_animations(self):
        now = time.time()
        speed = self.BTN_ANIM_SPEED
        updated = False
        for state in self._btn_states:
            cur = state['current_alpha']
            target = state['target_alpha']
            if abs(cur - target) > 0.001:
                state['current_alpha'] += (target - cur) * speed
                updated = True
        if updated:
            self.update()

    # ---------- Painting ----------
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)
        w, h = self.width(), self.height()
        ph = self._hover_progress
        pm = self._morph_progress
        pr = self._rise_progress

        painter.setBrush(QColor(0, 0, 0, self.MAIN_ALPHA))
        painter.setPen(Qt.NoPen)
        painter.drawRect(0, 0, w, h)

        parent_height = self.CHILD_HEIGHT + 2 * self.MARGIN
        parent_y = h - parent_height - 1
        dot_y_rest = parent_y + self.MARGIN + self.CHILD_HEIGHT / 2

        offset = self.PANEL_HEIGHT * pr

        if pr > 0.0:
            full_rect = QRectF(0, h, w, self.PANEL_HEIGHT)
            content_rect = full_rect.translated(0, -offset)            # vertical rise
            content_rect.translate(self._swipe_offset, 0)             # horizontal swipe
            painter.save()
            painter.setClipRect(QRectF(0, 0, w, h))
            panel_alpha = int(255 * min(1.0, pr * 1.5))
            painter.setOpacity(panel_alpha / 255.0)

            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            painter.drawPixmap(content_rect.topLeft(), self._static_cache)

            painter.setRenderHint(QPainter.SmoothPixmapTransform, False)
            self._draw_text_overlay(painter, content_rect, panel_alpha)

            self._draw_buttons(painter, content_rect, panel_alpha)

            painter.setOpacity(1.0)
            painter.restore()

        # Dot / Bar (unchanged)
        base_w = self.REST_CHILD_WIDTH + (self.CHILD_WIDTH - self.REST_CHILD_WIDTH) * ph
        base_h = self.CHILD_HEIGHT
        base_r = int(self.REST_CHILD_R + (self.CHILD_R - self.REST_CHILD_R) * ph)
        base_g = int(self.REST_CHILD_G + (self.CHILD_G - self.REST_CHILD_G) * ph)
        base_b = int(self.REST_CHILD_B + (self.CHILD_B - self.REST_CHILD_B) * ph)
        base_a = int(self.REST_CHILD_ALPHA + (self.CHILD_ALPHA - self.REST_CHILD_ALPHA) * ph)
        base_radius = self.CHILD_RADIUS

        circle_w = self.CIRCLE_DIAMETER
        circle_h = self.CIRCLE_DIAMETER
        circle_r, circle_g, circle_b = self.CIRCLE_R, self.CIRCLE_G, self.CIRCLE_B
        circle_a = self.CIRCLE_ALPHA
        circle_radius = self.CIRCLE_RADIUS

        final_w = base_w + (circle_w - base_w) * pm
        thr = self.SNAP_THRESHOLD
        snap = 0.0
        if pm > 1.0 - thr:
            snap = (pm - (1.0 - thr)) / thr
        final_h = base_h + (circle_h - base_h) * snap
        final_radius = base_radius + (circle_radius - base_radius) * snap
        final_r = int(base_r + (circle_r - base_r) * snap)
        final_g = int(base_g + (circle_g - base_g) * snap)
        final_b = int(base_b + (circle_b - base_b) * snap)
        final_a = int(base_a + (circle_a - base_a) * snap)

        dot_y = dot_y_rest - offset
        dot_rect = QRectF(w/2 - final_w/2, dot_y - final_h/2, final_w, final_h)
        dot_path = QPainterPath()
        dot_path.addRoundedRect(dot_rect, final_radius, final_radius)
        painter.setBrush(QColor(final_r, final_g, final_b, final_a))
        painter.setPen(Qt.NoPen)
        painter.drawPath(dot_path)

        parent_rect = QRectF(0, h - parent_height, w, parent_height)
        painter.setBrush(QColor(self.PARENT_R, self.PARENT_G, self.PARENT_B, self.PARENT_ALPHA))
        painter.drawRect(parent_rect)

        if self._right_dragging:
            vol_bar_x = (w - self.VOLUME_BAR_WIDTH) // 2
            vol_bar_y = h - self.VOLUME_BAR_HEIGHT - 1

            bg_rect = QRectF(vol_bar_x, vol_bar_y, self.VOLUME_BAR_WIDTH, self.VOLUME_BAR_HEIGHT)
            bg_path = QPainterPath()
            bg_path.addRoundedRect(bg_rect, self.VOLUME_BAR_RADIUS, self.VOLUME_BAR_RADIUS)
            painter.setBrush(self.VOLUME_BAR_BG_COLOR)
            painter.setPen(Qt.NoPen)
            painter.drawPath(bg_path)

            vol_fill = self.VOLUME_BAR_WIDTH * self._anim_volume
            if vol_fill > 0.5:
                fill_rect = QRectF(vol_bar_x, vol_bar_y, vol_fill, self.VOLUME_BAR_HEIGHT)
                fill_path = QPainterPath()
                fill_path.addRoundedRect(fill_rect, self.VOLUME_BAR_RADIUS, self.VOLUME_BAR_RADIUS)
                painter.setBrush(self.VOLUME_BAR_COLOR)
                painter.setPen(Qt.NoPen)
                painter.drawPath(fill_path)

    def _draw_buttons(self, painter, panel_rect, panel_alpha):
        x, y, w, h = panel_rect.x(), panel_rect.y(), panel_rect.width(), panel_rect.height()
        base = self.BUTTON_BASE_SIZE
        spacing = self.BUTTON_SPACING
        state_map = self._state_map()

        items = []
        for idx, (key, scale) in enumerate(self.BUTTONS):
            final = state_map.get(key, key)
            items.append((final, scale))

        total_w = sum(int(base * s) for _, s in items) + spacing * (len(items) - 1)
        start_x = x + w - total_w - self.BUTTON_RIGHT_MARGIN
        center_y = y + h // 2

        cur_x = start_x
        for idx, (final_key, nominal_scale) in enumerate(items):
            nominal_size = int(base * nominal_scale)
            visual_rect = QRectF(cur_x, center_y - nominal_size / 2,
                                 nominal_size, nominal_size)

            bg_alpha = int(panel_alpha * 0)
            painter.setBrush(QColor(255, 255, 255, bg_alpha))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(visual_rect, 6, 6)

            pix = self._icons.get(final_key)
            if pix:
                icon_alpha = int(panel_alpha * self._btn_states[idx]['current_alpha'])
                painter.save()
                painter.setOpacity(icon_alpha / 255.0)
                painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
                painter.drawPixmap(visual_rect.topLeft(), pix)
                painter.setRenderHint(QPainter.SmoothPixmapTransform, False)
                painter.restore()

            cur_x += nominal_size + spacing

    def _quit_app(self):
        QApplication.quit()

    def closeEvent(self, event):
        self.tray_icon.hide()
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    overlay = SpotifyOverlay()
    overlay.titleClicked.connect(lambda: print("Title double‑clicked!"))
    overlay.artistClicked.connect(lambda: print("Artist double‑clicked!"))
    overlay.set_track_info("A very long song title that needs to scroll because it's too long", "Artist Name")
    overlay.set_playing(True)
    overlay.set_volume(0.7)
    sys.exit(app.exec_())