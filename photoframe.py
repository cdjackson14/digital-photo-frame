#!/usr/bin/env python3
"""Raspberry Pi Zero 2 W Digital Photo Frame — Main Entry Point."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from config import ConfigManager
from display import PhotoFrameDisplay, SharedState, get_local_ip
from web import create_app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Raspberry Pi Digital Photo Frame")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--photos", default=None, help="Directory for photos (overrides config)")
    parser.add_argument("--port", type=int, default=None, help="Web UI port (overrides config)")
    parser.add_argument("--windowed", action="store_true", help="Run display in a window (for desktop testing)")
    parser.add_argument("--headless", action="store_true", help="Run web server only without video output")
    parser.add_argument("--width", type=int, default=960, help="Window width when using --windowed")
    parser.add_argument("--height", type=int, default=540, help="Window height when using --windowed")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("photoframe")

    args = parse_args()
    base_dir = Path(__file__).resolve().parent
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = base_dir / config_path

    config_manager = ConfigManager(config_path)
    cfg = config_manager.get()

    photos_dir = Path(args.photos or cfg["photos_dir"])
    if not photos_dir.is_absolute():
        photos_dir = base_dir / photos_dir
    photos_dir.mkdir(parents=True, exist_ok=True)

    port = args.port or cfg["port"]
    state = SharedState()
    app = create_app(config_manager, photos_dir, state)

    ip = get_local_ip()
    logger.info("Web interface starting at http://%s:%d", ip, port)

    if args.headless:
        logger.info("Running in headless (web-only) mode")
        app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)
        return

    # Silence noisy per-request Werkzeug logs in normal operation
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    web_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False),
        name="WebServerThread",
        daemon=True,
    )
    web_thread.start()

    display = PhotoFrameDisplay(
        config_manager=config_manager,
        photos_dir=photos_dir,
        state=state,
        windowed=args.windowed,
        window_size=(args.width, args.height),
    )

    def _handle_signal(signum: int, _frame: object) -> None:
        logger.info("Received signal %d, shutting down...", signum)
        display.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        display.run()
    except Exception as exc:
        logger.exception("Display engine error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
