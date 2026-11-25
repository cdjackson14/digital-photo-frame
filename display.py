"""Fullscreen slideshow display engine using Pygame/SDL2 and Pillow."""

from __future__ import annotations

import datetime
import logging
import os
import queue
import random
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image, ImageEnhance, ImageFilter, ImageOps

from config import ConfigManager

logger = logging.getLogger("photoframe.display")

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
TRANSITION_CHOICES = ("fade", "slide_left", "slide_right", "slide_up", "slide_down", "zoom")

# Find EXIF tag ID for DateTimeOriginal once
_EXIF_DATE_TAG = next(
    (k for k, v in ExifTags.TAGS.items() if v == "DateTimeOriginal"),
    36867,
)


def get_local_ip() -> str:
    """Return the Pi's primary LAN IP address without requiring internet access."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def list_photo_files(photos_dir: Path, order: str = "cycle") -> list[Path]:
    """Return sorted list of supported image files in photos_dir."""
    if not photos_dir.exists():
        return []
    files = [
        p
        for p in photos_dir.iterdir()
        if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in SUPPORTED_EXTENSIONS
    ]
    if order == "newest":
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    else:
        files.sort(key=lambda p: p.name.lower())
    return files


def is_in_sleep_window(now: datetime.time, start_str: str, end_str: str) -> bool:
    """Return True if `now` falls within [start_str, end_str), supporting overnight wrap."""
    try:
        sh, sm = (int(x) for x in start_str.split(":"))
        eh, em = (int(x) for x in end_str.split(":"))
    except ValueError:
        return False
    start = datetime.time(sh, sm)
    end = datetime.time(eh, em)
    if start == end:
        return False
    if start < end:
        return start <= now < end
    # Overnight wrap (e.g., 22:00 -> 07:00)
    return now >= start or now < end


def set_hdmi_power(on: bool) -> None:
    """Best-effort HDMI power toggle on Raspberry Pi (fails silently if unsupported)."""
    val = "1" if on else "0"
    for cmd in (
        ["vcgencmd", "display_power", val],
        ["xset", "dpms", "force", "on" if on else "off"],
    ):
        try:
            subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )
            return
        except (FileNotFoundError, subprocess.SubprocessError):
            continue


def extract_caption(img: Image.Image, path: Path) -> str:
    """Build a friendly caption from EXIF DateTimeOriginal or filename."""
    try:
        exif = img.getexif()
        raw_date = exif.get(_EXIF_DATE_TAG) if exif else None
        if raw_date and isinstance(raw_date, str):
            dt = datetime.datetime.strptime(raw_date.strip(), "%Y:%m:%d %H:%M:%S")
            return dt.strftime("%B %d, %Y")
    except Exception:
        pass
    stem = path.stem.replace("_", " ").replace("-", " ").strip()
    return stem[:60]


def prepare_image_pil(
    path: Path,
    target_size: tuple[int, int],
    fit_mode: str = "blur_fit",
) -> tuple[Image.Image, str]:
    """Load, EXIF-orient, and compose an RGB image at `target_size`."""
    tw, th = target_size
    with Image.open(path) as raw_img:
        caption = extract_caption(raw_img, path)
        img = ImageOps.exif_transpose(raw_img)
        if img.mode != "RGB":
            img = img.convert("RGB")

        iw, ih = img.size
        if iw <= 0 or ih <= 0:
            return Image.new("RGB", (tw, th), (0, 0, 0)), caption

        if fit_mode == "fill":
            scale = max(tw / iw, th / ih)
            nw, nh = max(1, int(round(iw * scale))), max(1, int(round(ih * scale)))
            resized = img.resize((nw, nh), Image.Resampling.LANCZOS)
            left = (nw - tw) // 2
            top = (nh - th) // 2
            canvas = resized.crop((left, top, left + tw, top + th))
            return canvas, caption

        # Fit foreground
        scale = min(tw / iw, th / ih)
        fw, fh = max(1, int(round(iw * scale))), max(1, int(round(ih * scale)))
        fg = img.resize((fw, fh), Image.Resampling.LANCZOS)

        if fit_mode == "blur_fit" and (fw < tw - 4 or fh < th - 4):
            # Fast low-res blur background (very lightweight on Pi Zero 2 W)
            small_w = max(32, tw // 8)
            small_h = max(32, th // 8)
            bg_scale = max(small_w / iw, small_h / ih)
            bw, bh = max(1, int(round(iw * bg_scale))), max(1, int(round(ih * bg_scale)))
            bg = img.resize((bw, bh), Image.Resampling.BILINEAR)
            bl = (bw - small_w) // 2
            bt = (bh - small_h) // 2
            bg = bg.crop((bl, bt, bl + small_w, bt + small_h))
            bg = bg.filter(ImageFilter.GaussianBlur(radius=10))
            bg = ImageEnhance.Brightness(bg).enhance(0.42)
            canvas = bg.resize((tw, th), Image.Resampling.BILINEAR)
        else:
            canvas = Image.new("RGB", (tw, th), (0, 0, 0))

        ox = (tw - fw) // 2
        oy = (th - fh) // 2
        canvas.paste(fg, (ox, oy))
        return canvas, caption


class SharedState:
    """Thread-safe playback state and command queue shared between web UI and display."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.paused: bool = False
        self.sleeping: bool = False
        self.current_photo: str | None = None
        self.total_photos: int = 0
        self.start_time: float = time.time()
        self.commands: queue.Queue[tuple[str, Any]] = queue.Queue()

    def push_command(self, cmd: str, payload: Any = None) -> None:
        self.commands.put((cmd, payload))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "paused": self.paused,
                "sleeping": self.sleeping,
                "current_photo": self.current_photo,
                "total_photos": self.total_photos,
                "uptime_seconds": int(time.time() - self.start_time),
            }

    def set_status(
        self,
        *,
        paused: bool | None = None,
        sleeping: bool | None = None,
        current_photo: str | None = ...,  # type: ignore[assignment]
        total_photos: int | None = None,
    ) -> None:
        with self._lock:
            if paused is not None:
                self.paused = paused
            if sleeping is not None:
                self.sleeping = sleeping
            if current_photo is not ...:
                self.current_photo = current_photo
            if total_photos is not None:
                self.total_photos = total_photos


class PhotoFrameDisplay:
    """Fullscreen Pygame/SDL2 slideshow renderer."""

    FPS = 30

    def __init__(
        self,
        config_manager: ConfigManager,
        photos_dir: Path,
        state: SharedState,
        *,
        windowed: bool = False,
        window_size: tuple[int, int] = (960, 540),
    ) -> None:
        self.config_manager = config_manager
        self.photos_dir = photos_dir
        self.state = state
        self.windowed = windowed
        self.window_size = window_size
        self._stop_event = threading.Event()

        self._playlist: list[Path] = []
        self._history: list[Path] = []
        self._index: int = -1
        self._shuffle_bag: list[Path] = []

        self._pygame: Any = None
        self._screen: Any = None
        self._phys_size: tuple[int, int] = (1280, 720)
        self._font_large: Any = None
        self._font_small: Any = None

    def stop(self) -> None:
        self._stop_event.set()

    def _init_pygame(self) -> None:
        # Configure SDL2 video driver cleanly for headless Raspberry Pi OS Lite (KMSDRM)
        if not self.windowed and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")
            os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
            # Force synchronous page flips; Pi vc4 KMS driver rejects async flips with -22 (EINVAL)
            os.environ.setdefault("SDL_RENDER_VSYNC", "1")

        os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
        import pygame  # local import so headless web tests don't require SDL

        self._pygame = pygame
        pygame.display.init()
        pygame.font.init()

        if self.windowed:
            self._screen = pygame.display.set_mode(self.window_size, pygame.RESIZABLE)
            pygame.display.set_caption("Digital Photo Frame (Windowed Preview)")
        else:
            # Bind to the display's exact native CRTC mode with VSync enabled
            info = pygame.display.Info()
            native_size = (
                (info.current_w, info.current_h)
                if (info.current_w > 0 and info.current_h > 0)
                else (0, 0)
            )
            try:
                if native_size != (0, 0):
                    self._screen = pygame.display.set_mode(
                        native_size, pygame.FULLSCREEN | pygame.SCALED, vsync=1
                    )
                else:
                    self._screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
            except pygame.error:
                self._screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
            pygame.mouse.set_visible(False)

        self._phys_size = self._screen.get_size()
        base_dim = min(self._phys_size)
        self._font_large = pygame.font.SysFont("dejavusans,arial,sans", max(24, base_dim // 18), bold=True)
        self._font_small = pygame.font.SysFont("dejavusans,arial,sans", max(16, base_dim // 32))
        logger.info("Display initialized at %dx%d", self._phys_size[0], self._phys_size[1])

    def _logical_size(self, rotation: int) -> tuple[int, int]:
        pw, ph = self._phys_size
        if rotation in (90, 270):
            return (ph, pw)
        return (pw, ph)

    def _pil_to_surface(self, pil_img: Image.Image) -> Any:
        pygame = self._pygame
        data = pil_img.tobytes()
        surf = pygame.image.frombuffer(data, pil_img.size, "RGB")
        return surf.convert()

    def _load_photo_surface(self, path: Path, cfg: dict[str, Any]) -> tuple[Any, str] | None:
        lsize = self._logical_size(cfg["rotation"])
        try:
            pil_img, caption = prepare_image_pil(path, lsize, cfg["fit_mode"])
            return self._pil_to_surface(pil_img), caption
        except Exception as exc:
            logger.warning("Failed to load %s: %s", path.name, exc)
            return None

    def _refresh_playlist(self, cfg: dict[str, Any]) -> None:
        files = list_photo_files(self.photos_dir, cfg["order"])
        self._playlist = files
        self.state.set_status(total_photos=len(files))
        # Prune shuffle bag of deleted files
        file_set = set(files)
        self._shuffle_bag = [p for p in self._shuffle_bag if p in file_set]

    def _pick_next(self, cfg: dict[str, Any], current: Path | None) -> Path | None:
        if not self._playlist:
            return None
        if len(self._playlist) == 1:
            self._index = 0
            return self._playlist[0]

        if cfg["order"] == "random":
            if not self._shuffle_bag:
                self._shuffle_bag = list(self._playlist)
                random.shuffle(self._shuffle_bag)
                # Avoid showing the exact same photo twice across reshuffles
                if current and len(self._shuffle_bag) > 1 and self._shuffle_bag[-1] == current:
                    self._shuffle_bag[0], self._shuffle_bag[-1] = self._shuffle_bag[-1], self._shuffle_bag[0]
            return self._shuffle_bag.pop()

        if current in self._playlist:
            self._index = (self._playlist.index(current) + 1) % len(self._playlist)
        else:
            self._index = (self._index + 1) % len(self._playlist)
        return self._playlist[self._index]

    def _pick_prev(self, cfg: dict[str, Any], current: Path | None) -> Path | None:
        if not self._playlist:
            return None
        if len(self._history) >= 2:
            # Pop current, return previous
            self._history.pop()
            prev = self._history[-1]
            if prev.exists():
                return prev
        if current in self._playlist:
            self._index = (self._playlist.index(current) - 1) % len(self._playlist)
        else:
            self._index = max(0, self._index - 1)
        return self._playlist[self._index]

    def _draw_pill_text(self, surface: Any, text: str, font: Any, pos: tuple[int, int], align: str = "left") -> None:
        pygame = self._pygame
        txt_surf = font.render(text, True, (245, 245, 245))
        pad_x, pad_y = 14, 8
        bw = txt_surf.get_width() + pad_x * 2
        bh = txt_surf.get_height() + pad_y * 2

        box = pygame.Surface((bw, bh), pygame.SRCALPHA)
        pygame.draw.rect(box, (0, 0, 0, 150), box.get_rect(), border_radius=10)
        box.blit(txt_surf, (pad_x, pad_y))

        x, y = pos
        if align == "right":
            x -= bw
        surface.blit(box, (x, y - bh))

    def _apply_overlays_and_present(
        self,
        logical_surf: Any,
        cfg: dict[str, Any],
        caption: str = "",
        paused: bool = False,
    ) -> None:
        pygame = self._pygame
        frame = logical_surf.copy()
        lw, lh = frame.get_size()
        margin = max(16, min(lw, lh) // 30)

        if cfg["show_caption"] and caption:
            self._draw_pill_text(frame, caption, self._font_small, (margin, lh - margin), align="left")

        if cfg["show_clock"]:
            now = datetime.datetime.now()
            fmt = "%H:%M" if cfg["clock_24h"] else "%I:%M %p"
            clock_str = now.strftime(fmt).lstrip("0")
            date_str = now.strftime("%a, %b %d")
            self._draw_pill_text(
                frame,
                f"{clock_str}  •  {date_str}",
                self._font_small,
                (lw - margin, lh - margin),
                align="right",
            )

        if paused:
            self._draw_pill_text(frame, "PAUSED", self._font_small, (lw - margin, margin + 40), align="right")

        brightness = cfg["brightness"]
        if brightness < 100:
            dim = pygame.Surface((lw, lh), pygame.SRCALPHA)
            alpha = int(255 * (100 - brightness) / 100)
            dim.fill((0, 0, 0, alpha))
            frame.blit(dim, (0, 0))

        rotation = cfg["rotation"]
        if rotation != 0:
            # Pygame rotates CCW, so negate for intuitive clockwise rotation
            frame = pygame.transform.rotate(frame, -rotation)

        self._screen.blit(frame, (0, 0))
        pygame.display.flip()

    def _render_empty_screen(self, cfg: dict[str, Any]) -> None:
        pygame = self._pygame
        lw, lh = self._logical_size(cfg["rotation"])
        canvas = pygame.Surface((lw, lh))
        canvas.fill((18, 22, 30))

        ip = get_local_ip()
        port = cfg["port"]
        lines = [
            ("Digital Photo Frame", self._font_large, (255, 255, 255)),
            ("No photos found yet.", self._font_small, (180, 190, 210)),
            (f"Upload photos at:  http://{ip}:{port}", self._font_small, (96, 180, 255)),
            (f"Or:  http://{socket.gethostname()}.local:{port}", self._font_small, (140, 160, 190)),
        ]

        total_h = sum(f.get_height() + 14 for _, f, _ in lines)
        y = (lh - total_h) // 2
        for text, font, color in lines:
            surf = font.render(text, True, color)
            canvas.blit(surf, ((lw - surf.get_width()) // 2, y))
            y += surf.get_height() + 14

        self._apply_overlays_and_present(canvas, cfg)

    def _run_transition(
        self,
        old_surf: Any | None,
        new_surf: Any,
        cfg: dict[str, Any],
        caption: str,
        paused: bool,
    ) -> None:
        pygame = self._pygame
        trans = cfg["transition"]
        if trans == "random":
            trans = random.choice(TRANSITION_CHOICES)

        dur = cfg["transition_duration"]
        if old_surf is None or trans == "none" or dur <= 0.05:
            self._apply_overlays_and_present(new_surf, cfg, caption, paused)
            return

        lw, lh = new_surf.get_size()
        frame = pygame.Surface((lw, lh))
        start = time.monotonic()
        clock = pygame.time.Clock()

        while not self._stop_event.is_set():
            elapsed = time.monotonic() - start
            t = min(1.0, elapsed / dur)
            # Smoothstep easing
            ease = t * t * (3.0 - 2.0 * t)

            if trans == "fade":
                frame.blit(old_surf, (0, 0))
                tmp = new_surf.copy()
                tmp.set_alpha(int(255 * ease))
                frame.blit(tmp, (0, 0))
            elif trans == "slide_left":
                offset = int(lw * ease)
                frame.blit(old_surf, (-offset, 0))
                frame.blit(new_surf, (lw - offset, 0))
            elif trans == "slide_right":
                offset = int(lw * ease)
                frame.blit(old_surf, (offset, 0))
                frame.blit(new_surf, (offset - lw, 0))
            elif trans == "slide_up":
                offset = int(lh * ease)
                frame.blit(old_surf, (0, -offset))
                frame.blit(new_surf, (0, lh - offset))
            elif trans == "slide_down":
                offset = int(lh * ease)
                frame.blit(old_surf, (0, offset))
                frame.blit(new_surf, (0, offset - lh))
            elif trans == "zoom":
                frame.blit(old_surf, (0, 0))
                scale = 0.85 + 0.15 * ease
                sw, sh = max(1, int(lw * scale)), max(1, int(lh * scale))
                scaled = pygame.transform.smoothscale(new_surf, (sw, sh))
                scaled.set_alpha(int(255 * ease))
                frame.blit(scaled, ((lw - sw) // 2, (lh - sh) // 2))
            else:
                frame.blit(new_surf, (0, 0))

            self._apply_overlays_and_present(frame, cfg, caption, paused)

            if t >= 1.0:
                break

            # Keep SDL event queue responsive during transition
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self._stop_event.set()
                    return
            clock.tick(self.FPS)

    def _poll_events(self) -> None:
        pygame = self._pygame
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self._stop_event.set()
            elif event.type == pygame.VIDEORESIZE and self.windowed:
                self._screen = pygame.display.set_mode((event.w, event.h), pygame.RESIZABLE)
                self._phys_size = self._screen.get_size()
                self.state.push_command("reload")
            elif event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_ESCAPE, pygame.K_q):
                    self._stop_event.set()
                elif event.key in (pygame.K_RIGHT, pygame.K_SPACE):
                    self.state.push_command("next")
                elif event.key == pygame.K_LEFT:
                    self.state.push_command("prev")
                elif event.key == pygame.K_p:
                    self.state.push_command("toggle_pause")

    def run(self) -> None:
        """Main blocking display loop (must run on the main thread)."""
        self._init_pygame()
        pygame = self._pygame

        cfg = self.config_manager.get()
        last_cfg_version = self.config_manager.version
        self._refresh_playlist(cfg)

        current_path: Path | None = None
        current_surf: Any | None = None
        current_caption: str = ""
        shown_at: float = 0.0
        last_overlay_tick: float = 0.0
        was_sleeping: bool = False
        need_advance: bool = True
        pending_target: Path | None = None

        clock = pygame.time.Clock()

        try:
            while not self._stop_event.is_set():
                self._poll_events()

                # Check for config changes
                if self.config_manager.version != last_cfg_version:
                    old_cfg = cfg
                    cfg = self.config_manager.get()
                    last_cfg_version = self.config_manager.version
                    self._refresh_playlist(cfg)
                    if (
                        cfg["fit_mode"] != old_cfg["fit_mode"]
                        or cfg["rotation"] != old_cfg["rotation"]
                    ) and current_path and current_path.exists():
                        loaded = self._load_photo_surface(current_path, cfg)
                        if loaded:
                            current_surf, current_caption = loaded
                    if current_surf is not None:
                        self._apply_overlays_and_present(
                            current_surf, cfg, current_caption, self.state.paused
                        )

                # Handle sleep schedule
                sleeping = cfg["sleep_enabled"] and is_in_sleep_window(
                    datetime.datetime.now().time(), cfg["sleep_start"], cfg["sleep_end"]
                )
                if sleeping != was_sleeping:
                    was_sleeping = sleeping
                    self.state.set_status(sleeping=sleeping)
                    if sleeping:
                        logger.info("Entering sleep mode")
                        self._screen.fill((0, 0, 0))
                        pygame.display.flip()
                        set_hdmi_power(False)
                    else:
                        logger.info("Waking up from sleep mode")
                        set_hdmi_power(True)
                        if current_surf is not None:
                            self._apply_overlays_and_present(
                                current_surf, cfg, current_caption, self.state.paused
                            )

                if sleeping:
                    # Drain reload commands while sleeping, sleep cheaply
                    try:
                        cmd, _ = self.state.commands.get_nowait()
                        if cmd == "reload":
                            self._refresh_playlist(cfg)
                    except queue.Empty:
                        pass
                    time.sleep(0.5)
                    continue

                # Process remote control commands
                try:
                    while True:
                        cmd, payload = self.state.commands.get_nowait()
                        if cmd == "next":
                            need_advance = True
                        elif cmd == "prev":
                            prev_path = self._pick_prev(cfg, current_path)
                            if prev_path:
                                pending_target = prev_path
                                need_advance = True
                        elif cmd == "pause":
                            self.state.set_status(paused=True)
                            if current_surf is not None:
                                self._apply_overlays_and_present(current_surf, cfg, current_caption, True)
                        elif cmd == "resume":
                            self.state.set_status(paused=False)
                            shown_at = time.monotonic()
                            if current_surf is not None:
                                self._apply_overlays_and_present(current_surf, cfg, current_caption, False)
                        elif cmd == "toggle_pause":
                            new_paused = not self.state.paused
                            self.state.set_status(paused=new_paused)
                            if not new_paused:
                                shown_at = time.monotonic()
                            if current_surf is not None:
                                self._apply_overlays_and_present(current_surf, cfg, current_caption, new_paused)
                        elif cmd == "reload":
                            self._refresh_playlist(cfg)
                            if current_path and not current_path.exists():
                                current_path = None
                                current_surf = None
                                need_advance = True
                            elif current_path and current_path.exists():
                                loaded = self._load_photo_surface(current_path, cfg)
                                if loaded:
                                    current_surf, current_caption = loaded
                                    self._apply_overlays_and_present(
                                        current_surf, cfg, current_caption, self.state.paused
                                    )
                            elif current_path is None and self._playlist:
                                need_advance = True
                        elif cmd == "jump" and isinstance(payload, str):
                            target = self.photos_dir / Path(payload).name
                            if target.exists():
                                pending_target = target
                                need_advance = True
                except queue.Empty:
                    pass

                # Empty folder state
                if not self._playlist:
                    if current_surf is not None or shown_at == 0.0:
                        current_path = None
                        current_surf = None
                        self.state.set_status(current_photo=None)
                        self._render_empty_screen(cfg)
                        shown_at = time.monotonic()
                    elif time.monotonic() - shown_at >= 5.0:
                        self._render_empty_screen(cfg)
                        shown_at = time.monotonic()
                    clock.tick(10)
                    continue

                # Check slideshow timer
                now_mono = time.monotonic()
                if not self.state.paused and (now_mono - shown_at >= cfg["duration"]):
                    need_advance = True

                if need_advance:
                    need_advance = False
                    target = pending_target or self._pick_next(cfg, current_path)
                    pending_target = None

                    if target is not None:
                        loaded = self._load_photo_surface(target, cfg)
                        if loaded:
                            new_surf, new_caption = loaded
                            self._run_transition(
                                current_surf, new_surf, cfg, new_caption, self.state.paused
                            )
                            current_surf = new_surf
                            current_caption = new_caption
                            current_path = target
                            shown_at = time.monotonic()
                            last_overlay_tick = shown_at
                            if not self._history or self._history[-1] != target:
                                self._history.append(target)
                                if len(self._history) > 50:
                                    self._history.pop(0)
                            self.state.set_status(current_photo=target.name)
                        else:
                            # Skip unreadable file
                            shown_at = time.monotonic()

                # Refresh clock overlay every 15 seconds if enabled
                if cfg["show_clock"] and current_surf is not None and (now_mono - last_overlay_tick >= 15.0):
                    self._apply_overlays_and_present(
                        current_surf, cfg, current_caption, self.state.paused
                    )
                    last_overlay_tick = now_mono

                clock.tick(15)
        finally:
            if was_sleeping:
                set_hdmi_power(True)
            pygame.quit()
