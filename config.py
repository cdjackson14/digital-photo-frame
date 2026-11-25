"""Configuration management for the Raspberry Pi Digital Photo Frame."""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

VALID_ORDERS = ("cycle", "newest", "random")
VALID_TRANSITIONS = (
    "none",
    "fade",
    "slide_left",
    "slide_right",
    "slide_up",
    "slide_down",
    "zoom",
    "random",
)
VALID_FIT_MODES = ("blur_fit", "fit", "fill")
VALID_ROTATIONS = (0, 90, 180, 270)

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

DEFAULT_CONFIG: dict[str, Any] = {
    "duration": 10,
    "order": "cycle",
    "transition": "fade",
    "transition_duration": 1.0,
    "fit_mode": "blur_fit",
    "rotation": 0,
    "brightness": 100,
    "show_clock": False,
    "clock_24h": False,
    "show_caption": False,
    "sleep_enabled": False,
    "sleep_start": "22:00",
    "sleep_end": "07:00",
    "port": 5000,
    "photos_dir": "photos",
}


def validate_config(raw: dict[str, Any], base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate and coerce incoming config values against defaults/base."""
    cfg = dict(DEFAULT_CONFIG if base is None else base)

    if "duration" in raw:
        try:
            val = float(raw["duration"])
            cfg["duration"] = max(1, min(86400, int(round(val))))
        except (TypeError, ValueError):
            pass

    if "order" in raw and raw["order"] in VALID_ORDERS:
        cfg["order"] = raw["order"]

    if "transition" in raw and raw["transition"] in VALID_TRANSITIONS:
        cfg["transition"] = raw["transition"]

    if "transition_duration" in raw:
        try:
            val = float(raw["transition_duration"])
            cfg["transition_duration"] = round(max(0.1, min(5.0, val)), 2)
        except (TypeError, ValueError):
            pass

    if "fit_mode" in raw and raw["fit_mode"] in VALID_FIT_MODES:
        cfg["fit_mode"] = raw["fit_mode"]

    if "rotation" in raw:
        try:
            rot = int(raw["rotation"])
            if rot in VALID_ROTATIONS:
                cfg["rotation"] = rot
        except (TypeError, ValueError):
            pass

    if "brightness" in raw:
        try:
            bri = int(raw["brightness"])
            cfg["brightness"] = max(10, min(100, bri))
        except (TypeError, ValueError):
            pass

    for bool_key in ("show_clock", "clock_24h", "show_caption", "sleep_enabled"):
        if bool_key in raw:
            val = raw[bool_key]
            if isinstance(val, bool):
                cfg[bool_key] = val
            elif isinstance(val, str):
                cfg[bool_key] = val.lower() in ("1", "true", "yes", "on")
            else:
                cfg[bool_key] = bool(val)

    for time_key in ("sleep_start", "sleep_end"):
        if time_key in raw and isinstance(raw[time_key], str):
            t = raw[time_key].strip()
            if TIME_RE.match(t):
                cfg[time_key] = t

    if "port" in raw:
        try:
            p = int(raw["port"])
            if 1 <= p <= 65535:
                cfg["port"] = p
        except (TypeError, ValueError):
            pass

    if "photos_dir" in raw and isinstance(raw["photos_dir"], str) and raw["photos_dir"].strip():
        cfg["photos_dir"] = raw["photos_dir"].strip()

    return cfg


class ConfigManager:
    """Thread-safe JSON-backed configuration manager."""

    def __init__(self, config_path: str | Path = "config.json") -> None:
        self.path = Path(config_path)
        self._lock = threading.RLock()
        self._version = 0
        self._config = dict(DEFAULT_CONFIG)
        self.load()

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def load(self) -> dict[str, Any]:
        with self._lock:
            if self.path.exists():
                try:
                    with self.path.open("r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        self._config = validate_config(data, DEFAULT_CONFIG)
                except (OSError, json.JSONDecodeError):
                    self._config = dict(DEFAULT_CONFIG)
            else:
                self._save_locked()
            self._version += 1
            return dict(self._config)

    def get(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._config)

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._config = validate_config(patch, self._config)
            self._save_locked()
            self._version += 1
            return dict(self._config)

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(self._config, f, indent=2)
        os.replace(tmp_path, self.path)
