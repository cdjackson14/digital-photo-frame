"""Flask web interface and REST API for configuring and managing the Digital Photo Frame."""

from __future__ import annotations

import logging
import shutil
import socket
from pathlib import Path
from typing import Any

from flask import Flask, abort, jsonify, render_template, request, send_file
from PIL import Image, ImageOps
from werkzeug.utils import secure_filename

from config import (
    VALID_FIT_MODES,
    VALID_ORDERS,
    VALID_ROTATIONS,
    VALID_TRANSITIONS,
    ConfigManager,
)
from display import SUPPORTED_EXTENSIONS, SharedState, get_local_ip, list_photo_files

logger = logging.getLogger("photoframe.web")

THUMB_SIZE = (420, 420)
MAX_UPLOAD_MB = 30


def _safe_photo_path(photos_dir: Path, filename: str) -> Path:
    """Resolve filename strictly inside photos_dir to prevent path traversal."""
    clean = Path(filename).name
    if not clean or clean.startswith(".") or clean != filename:
        abort(400, description="Invalid filename")
    path = (photos_dir / clean).resolve()
    if path.parent != photos_dir.resolve():
        abort(400, description="Invalid filename")
    return path


def _thumb_path(photos_dir: Path, filename: str) -> Path:
    thumbs_dir = photos_dir / ".thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    return thumbs_dir / f"{Path(filename).name}.jpg"


def generate_thumbnail(photo_path: Path, thumb_path: Path) -> bool:
    """Generate a fast JPEG thumbnail with EXIF orientation applied."""
    try:
        with Image.open(photo_path) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail(THUMB_SIZE, Image.Resampling.LANCZOS)
            thumb_path.parent.mkdir(parents=True, exist_ok=True)
            img.save(thumb_path, format="JPEG", quality=82, optimize=True)
        return True
    except Exception as exc:
        logger.warning("Thumbnail generation failed for %s: %s", photo_path.name, exc)
        return False


def create_app(
    config_manager: ConfigManager,
    photos_dir: Path,
    state: SharedState,
) -> Flask:
    """Create and configure the Flask web application."""
    photos_dir.mkdir(parents=True, exist_ok=True)
    (photos_dir / ".thumbs").mkdir(parents=True, exist_ok=True)

    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
    )
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

    @app.route("/")
    def index() -> str:
        return render_template("index.html")

    @app.route("/api/status", methods=["GET"])
    def api_status() -> Any:
        disk = shutil.disk_usage(photos_dir)
        snap = state.snapshot()
        snap["total_photos"] = len(list_photo_files(photos_dir))
        snap["disk_total_gb"] = round(disk.total / (1024**3), 2)
        snap["disk_free_gb"] = round(disk.free / (1024**3), 2)
        snap["disk_used_pct"] = round((disk.used / disk.total) * 100, 1) if disk.total else 0
        snap["ip"] = get_local_ip()
        snap["hostname"] = socket.gethostname()
        return jsonify(snap)

    @app.route("/api/config", methods=["GET"])
    def api_get_config() -> Any:
        return jsonify(
            {
                "config": config_manager.get(),
                "options": {
                    "orders": list(VALID_ORDERS),
                    "transitions": list(VALID_TRANSITIONS),
                    "fit_modes": list(VALID_FIT_MODES),
                    "rotations": list(VALID_ROTATIONS),
                },
            }
        )

    @app.route("/api/config", methods=["POST"])
    def api_update_config() -> Any:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Invalid JSON body"}), 400
        updated = config_manager.update(data)
        return jsonify({"config": updated, "ok": True})

    @app.route("/api/photos", methods=["GET"])
    def api_list_photos() -> Any:
        order = request.args.get("order", "newest")
        files = list_photo_files(photos_dir, order=order)
        items = []
        for p in files:
            st = p.stat()
            items.append(
                {
                    "name": p.name,
                    "size_bytes": st.st_size,
                    "mtime": int(st.st_mtime),
                    "thumb_url": f"/api/photos/{p.name}/thumb?v={int(st.st_mtime)}",
                    "full_url": f"/api/photos/{p.name}/full?v={int(st.st_mtime)}",
                }
            )
        return jsonify({"photos": items, "count": len(items)})

    @app.route("/api/photos/upload", methods=["POST"])
    def api_upload_photos() -> Any:
        uploaded_files = request.files.getlist("files")
        if not uploaded_files:
            return jsonify({"error": "No files provided"}), 400

        saved: list[str] = []
        errors: list[dict[str, str]] = []

        for f in uploaded_files:
            orig_name = f.filename or ""
            safe = secure_filename(orig_name)
            if not safe:
                errors.append({"file": orig_name, "error": "Invalid filename"})
                continue

            ext = Path(safe).suffix.lower()
            if ext not in SUPPORTED_EXTENSIONS:
                errors.append({"file": orig_name, "error": f"Unsupported format ({ext})"})
                continue

            # Deduplicate filename if it already exists
            dest = photos_dir / safe
            if dest.exists():
                stem = dest.stem
                idx = 1
                while dest.exists():
                    dest = photos_dir / f"{stem}_{idx}{ext}"
                    idx += 1

            try:
                f.save(dest)
                # Validate that it is a readable image
                with Image.open(dest) as img:
                    img.verify()
                generate_thumbnail(dest, _thumb_path(photos_dir, dest.name))
                saved.append(dest.name)
            except Exception as exc:
                dest.unlink(missing_ok=True)
                errors.append({"file": orig_name, "error": f"Invalid image: {exc}"})

        if saved:
            state.push_command("reload")

        return jsonify({"saved": saved, "errors": errors, "ok": len(saved) > 0})

    @app.route("/api/photos/<filename>", methods=["DELETE"])
    def api_delete_photo(filename: str) -> Any:
        path = _safe_photo_path(photos_dir, filename)
        if not path.exists():
            return jsonify({"error": "Photo not found"}), 404
        path.unlink(missing_ok=True)
        _thumb_path(photos_dir, filename).unlink(missing_ok=True)
        state.push_command("reload")
        return jsonify({"ok": True, "deleted": filename})

    @app.route("/api/photos/delete-batch", methods=["POST"])
    def api_delete_batch() -> Any:
        data = request.get_json(silent=True) or {}
        names = data.get("filenames", [])
        if not isinstance(names, list):
            return jsonify({"error": "filenames must be a list"}), 400

        deleted: list[str] = []
        for name in names:
            if not isinstance(name, str):
                continue
            try:
                path = _safe_photo_path(photos_dir, name)
            except Exception:
                continue
            if path.exists():
                path.unlink(missing_ok=True)
                _thumb_path(photos_dir, name).unlink(missing_ok=True)
                deleted.append(name)

        if deleted:
            state.push_command("reload")
        return jsonify({"ok": True, "deleted": deleted})

    @app.route("/api/photos/<filename>/rotate", methods=["POST"])
    def api_rotate_photo(filename: str) -> Any:
        path = _safe_photo_path(photos_dir, filename)
        if not path.exists():
            return jsonify({"error": "Photo not found"}), 404

        data = request.get_json(silent=True) or {}
        direction = data.get("direction", "cw")
        angle = -90 if direction == "cw" else 90

        try:
            with Image.open(path) as raw:
                img = ImageOps.exif_transpose(raw)
                rotated = img.rotate(angle, expand=True)
                fmt = raw.format or "JPEG"
                if fmt.upper() in ("JPEG", "JPG") and rotated.mode != "RGB":
                    rotated = rotated.convert("RGB")
                save_kwargs: dict[str, Any] = {}
                if fmt.upper() in ("JPEG", "JPG"):
                    save_kwargs["quality"] = 92
                rotated.save(path, format=fmt, **save_kwargs)
            generate_thumbnail(path, _thumb_path(photos_dir, filename))
            state.push_command("reload")
            return jsonify({"ok": True, "name": filename})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/photos/<filename>/thumb", methods=["GET"])
    def api_photo_thumb(filename: str) -> Any:
        path = _safe_photo_path(photos_dir, filename)
        if not path.exists():
            abort(404)
        tpath = _thumb_path(photos_dir, filename)
        if not tpath.exists() or tpath.stat().st_mtime < path.stat().st_mtime:
            if not generate_thumbnail(path, tpath):
                return send_file(path)
        return send_file(tpath, mimetype="image/jpeg")

    @app.route("/api/photos/<filename>/full", methods=["GET"])
    def api_photo_full(filename: str) -> Any:
        path = _safe_photo_path(photos_dir, filename)
        if not path.exists():
            abort(404)
        return send_file(path)

    @app.route("/api/control", methods=["POST"])
    def api_control() -> Any:
        data = request.get_json(silent=True) or {}
        action = data.get("action")
        if action in ("next", "prev", "pause", "resume", "toggle_pause", "reload"):
            state.push_command(action)
            return jsonify({"ok": True, "action": action})
        if action == "jump" and isinstance(data.get("filename"), str):
            _safe_photo_path(photos_dir, data["filename"])
            state.push_command("jump", data["filename"])
            return jsonify({"ok": True, "action": "jump", "filename": data["filename"]})
        return jsonify({"error": "Invalid action"}), 400

    return app
