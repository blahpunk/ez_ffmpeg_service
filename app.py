from __future__ import annotations

import configparser
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue


APP_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = APP_ROOT / "static"
SETTINGS_PATH = APP_ROOT / "service_settings.json"
EXCLUSIONS_PATH = APP_ROOT / "service_exclusions.json"
LEGACY_SETTINGS_PATH = APP_ROOT.parent / "EZ_ffmpeg" / "settings.ini"
HOST = "127.0.0.1"
PORT = 2323
SCAN_INTERVAL_SECONDS = 4
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

VIDEO_EXTENSIONS = {
    ".3g2",
    ".3gp",
    ".avi",
    ".divx",
    ".flv",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".ogm",
    ".ogv",
    ".ts",
    ".vob",
    ".webm",
    ".wmv",
}

ACTIVE_STATUSES = {
    "Probing",
    "Checking thresholds",
    "Copying to cache",
    "Launching encoder",
    "Processing",
    "Finalizing",
    "Replacing",
    "Moving output",
}
PROCESSABLE_STATUSES = {"Ready", "Stopped"}
RECHECK_ON_SETTINGS_STATUSES = {"Ready", "Below threshold", "Skipped", "Stopped", "Queued", "Analyzing"}
TERMINAL_STATUSES = {"Completed", "Skipped", "Below threshold"}
PROCESSED_NAME_PATTERN = re.compile(r"_processed(?:_\d+)?$", re.IGNORECASE)
TIME_PATTERN = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
SPEED_PATTERN = re.compile(r"speed=\s*([0-9.]+)x")


ENCODER_PROFILES = {
    "auto": {
        "label": "Auto",
        "default_speed": 0.9,
    },
    "libx265": {
        "label": "CPU H.265 (libx265)",
        "default_speed": 0.35,
    },
    "h264_nvenc": {
        "label": "GPU H.264 (h264_nvenc)",
        "default_speed": 2.8,
    },
    "hevc_nvenc": {
        "label": "GPU H.265 (hevc_nvenc)",
        "default_speed": 2.0,
    },
    "av1_nvenc": {
        "label": "GPU AV1 (av1_nvenc)",
        "default_speed": 1.2,
    },
}
AUTO_PRIORITY = ["hevc_nvenc", "h264_nvenc", "av1_nvenc", "libx265"]
MAX_HISTORY_ITEMS = 200


@dataclass
class ServiceSettings:
    monitor_enabled: bool = True
    normalize: bool = True
    stereo: bool = True
    replace: bool = True
    convert: bool = True
    mb_min: int = 12
    threshold: float = 2.0
    encoder_mode: str = "hevc_nvenc"
    temp_folder: str = field(default_factory=lambda: os.path.join(tempfile.gettempdir(), "ez_ffmpeg_cache"))
    monitored_folders: list[str] = field(default_factory=list)


def normalize_path(path: str) -> str:
    expanded = os.path.expandvars(os.path.expanduser(path.strip().strip('"')))
    return os.path.normpath(os.path.abspath(expanded))


def path_is_under(path: str, folder: str) -> bool:
    try:
        common = os.path.commonpath([os.path.abspath(path), os.path.abspath(folder)])
    except ValueError:
        return False
    return common == os.path.abspath(folder)


def safe_float(value, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def safe_int(value, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def format_seconds(seconds) -> str:
    if seconds is None:
        return "--"
    total_seconds = max(int(round(seconds)), 0)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    remaining_seconds = total_seconds % 60
    return f"{hours:02}:{minutes:02}:{remaining_seconds:02}"


def format_speed(speed_multiplier: float) -> str:
    if not speed_multiplier or speed_multiplier <= 0:
        return ""
    return f"{speed_multiplier:.2f}x"


def file_id_for_path(path: str) -> str:
    return hashlib.sha1(os.path.normcase(path).encode("utf-8", errors="replace")).hexdigest()


def is_video_path(path: str) -> bool:
    suffix = Path(path).suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        return True
    mime_type, _ = mimetypes.guess_type(path)
    return bool(mime_type and mime_type.startswith("video"))


def should_ignore_video(path: str, cache_folder: str) -> bool:
    if cache_folder and path_is_under(path, cache_folder):
        return True
    stem = Path(path).stem
    if PROCESSED_NAME_PATTERN.search(stem):
        return True
    if ".ez_ffmpeg_backup" in path:
        return True
    return False


def get_filesystem_roots() -> list[dict]:
    if os.name == "nt":
        roots = []
        try:
            import ctypes

            bitmask = ctypes.windll.kernel32.GetLogicalDrives()
            for index, letter in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
                if bitmask & (1 << index):
                    root_path = f"{letter}:\\"
                    roots.append({"name": root_path, "path": root_path})
        except Exception:
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                root_path = f"{letter}:\\"
                if os.path.exists(root_path):
                    roots.append({"name": root_path, "path": root_path})
        return roots
    return [{"name": "/", "path": "/"}]


def is_filesystem_root(path: str) -> bool:
    normalized = os.path.abspath(path)
    if os.name == "nt":
        drive, tail = os.path.splitdrive(normalized)
        return bool(drive) and tail in {"\\", "/"}
    return normalized == os.path.abspath(os.sep)


def browse_directory(path: str = "") -> dict:
    roots = get_filesystem_roots()
    if not path.strip():
        return {
            "current_path": "",
            "parent_path": "",
            "roots": roots,
            "entries": roots,
        }

    current_path = normalize_path(path)
    if not os.path.isdir(current_path):
        raise ValueError(f"Folder does not exist: {current_path}")

    entries = []
    try:
        with os.scandir(current_path) as scan:
            for entry in scan:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        entries.append({"name": entry.name, "path": entry.path})
                except OSError:
                    continue
    except PermissionError as exc:
        raise ValueError(f"Permission denied: {current_path}") from exc

    entries.sort(key=lambda item: item["name"].lower())
    parent_path = "" if is_filesystem_root(current_path) else os.path.dirname(current_path)
    return {
        "current_path": current_path,
        "parent_path": parent_path,
        "roots": roots,
        "entries": entries,
    }


def load_settings() -> ServiceSettings:
    settings = ServiceSettings()

    if LEGACY_SETTINGS_PATH.exists():
        legacy = configparser.ConfigParser()
        legacy.read(LEGACY_SETTINGS_PATH)
        if legacy.has_section("Settings"):
            section = legacy["Settings"]
            settings.normalize = section.getboolean("normalize", settings.normalize)
            settings.stereo = section.getboolean("stereo", settings.stereo)
            settings.replace = section.getboolean("replace", settings.replace)
            settings.convert = section.getboolean("convert", settings.convert)
            settings.encoder_mode = section.get("encoder_mode", settings.encoder_mode)
            legacy_temp = section.get("temp_folder", "")
            if legacy_temp:
                settings.temp_folder = legacy_temp

    if SETTINGS_PATH.exists():
        try:
            payload = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            for key in asdict(settings):
                if key in payload:
                    setattr(settings, key, payload[key])
        except Exception as exc:
            print(f"Unable to load service settings: {exc}")

    settings.mb_min = min(max(safe_int(settings.mb_min, 12), 2), 45)
    settings.threshold = max(safe_float(settings.threshold, 2.0), 0.0)
    settings.monitored_folders = [
        normalize_path(folder)
        for folder in settings.monitored_folders
        if isinstance(folder, str) and folder.strip()
    ]
    if not settings.temp_folder:
        settings.temp_folder = os.path.join(tempfile.gettempdir(), "ez_ffmpeg_cache")
    return settings


class EZFfmpegService:
    def __init__(self):
        self.lock = threading.RLock()
        self.settings = load_settings()
        self.items: dict[str, dict] = {}
        self.analysis_queue: Queue[str] = Queue()
        self.folder_progress: dict[str, dict] = {}
        self.exclusions: dict[str, dict] = self.load_exclusions()
        self.scan_generation = 0
        self.scan_event = threading.Event()
        self.shutdown_event = threading.Event()
        self.run_event = threading.Event()
        self.abort_event = threading.Event()
        self.current_process: subprocess.Popen | None = None
        self.current_cached_file_path: str | None = None
        self.current_output_file: str | None = None
        self.current_item_path: str | None = None
        self.available_encoders = self.detect_available_encoders()
        self.encode_history: list[dict] = []
        self.status_message = ""
        self.last_scan_started = None
        self.last_scan_finished = None
        self.last_scan_error = ""
        self.ffmpeg_available = self.check_tool_available("ffmpeg") and self.check_tool_available("ffprobe")
        self.set_cache_folder(self.settings.temp_folder, persist=False)
        self.save_settings()
        self.start_background_threads()

    def start_background_threads(self):
        threading.Thread(target=self.monitor_loop, name="ez-monitor", daemon=True).start()
        threading.Thread(target=self.conversion_loop, name="ez-conversion", daemon=True).start()
        self.scan_event.set()

    def check_tool_available(self, tool_name: str) -> bool:
        try:
            result = subprocess.run(
                [tool_name, "-version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
                creationflags=CREATE_NO_WINDOW,
            )
            return result.returncode == 0
        except Exception:
            return False

    def save_settings(self):
        try:
            SETTINGS_PATH.write_text(json.dumps(asdict(self.settings), indent=2), encoding="utf-8")
        except Exception as exc:
            print(f"Unable to save service settings: {exc}")

    def load_exclusions(self) -> dict[str, dict]:
        if not EXCLUSIONS_PATH.exists():
            return {}
        try:
            payload = json.loads(EXCLUSIONS_PATH.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception as exc:
            print(f"Unable to load exclusions: {exc}")
            return {}

    def save_exclusions(self):
        try:
            EXCLUSIONS_PATH.write_text(json.dumps(self.exclusions, indent=2), encoding="utf-8")
        except Exception as exc:
            print(f"Unable to save exclusions: {exc}")

    def detect_available_encoders(self) -> set[str]:
        detected = {"libx265"}
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=20,
                creationflags=CREATE_NO_WINDOW,
            )
            output = f"{result.stdout}\n{result.stderr}"
            for encoder_key in ("h264_nvenc", "hevc_nvenc", "av1_nvenc", "libx265"):
                if encoder_key in output:
                    detected.add(encoder_key)
        except Exception as exc:
            print(f"Unable to detect FFmpeg encoders: {exc}")
        return detected

    def get_encoder_label(self, encoder_key: str) -> str:
        profile = ENCODER_PROFILES.get(encoder_key)
        if profile:
            return profile["label"]
        return encoder_key

    def resolve_encoder_mode(self, selected_mode: str | None = None) -> str:
        selected_mode = selected_mode or self.settings.encoder_mode
        if selected_mode and selected_mode != "auto" and selected_mode in self.available_encoders:
            return selected_mode

        for encoder_key in AUTO_PRIORITY:
            if encoder_key in self.available_encoders:
                return encoder_key
        return "libx265"

    def encoder_options(self) -> list[dict]:
        options = [{"value": "auto", "label": self.get_encoder_label("auto")}]
        for encoder_key in AUTO_PRIORITY:
            if encoder_key in self.available_encoders:
                options.append({"value": encoder_key, "label": self.get_encoder_label(encoder_key)})
        if len(options) == 1:
            options.append({"value": "libx265", "label": self.get_encoder_label("libx265")})
        return options

    def set_cache_folder(self, folder_path: str, persist: bool = True):
        try:
            normalized_path = normalize_path(folder_path)
            os.makedirs(normalized_path, exist_ok=True)
        except Exception as exc:
            fallback = os.path.join(tempfile.gettempdir(), "ez_ffmpeg_cache")
            print(f"Unable to use cache folder {folder_path}: {exc}; using {fallback}")
            normalized_path = fallback
            os.makedirs(normalized_path, exist_ok=True)

        with self.lock:
            self.settings.temp_folder = normalized_path
            self.history_path = os.path.join(normalized_path, "encode_history.json")
            self.encode_history = self.load_encode_history()
            self.cleanup_stale_cache()
            if persist:
                self.save_settings()

    def load_encode_history(self) -> list[dict]:
        if not os.path.exists(self.history_path):
            return []
        try:
            with open(self.history_path, "r", encoding="utf-8") as history_file:
                payload = json.load(history_file)
            return payload if isinstance(payload, list) else []
        except Exception as exc:
            print(f"Unable to load encode history: {exc}")
            return []

    def save_encode_history(self):
        try:
            with open(self.history_path, "w", encoding="utf-8") as history_file:
                json.dump(self.encode_history[-MAX_HISTORY_ITEMS:], history_file, indent=2)
        except Exception as exc:
            print(f"Unable to save encode history: {exc}")

    def cleanup_stale_cache(self):
        cache_folder = self.settings.temp_folder
        if not os.path.isdir(cache_folder):
            return
        for entry in os.listdir(cache_folder):
            entry_path = os.path.join(cache_folder, entry)
            if os.path.abspath(entry_path) == os.path.abspath(self.history_path):
                continue
            try:
                if os.path.isdir(entry_path):
                    shutil.rmtree(entry_path, ignore_errors=True)
                else:
                    os.remove(entry_path)
            except Exception as exc:
                print(f"Error cleaning cache entry {entry_path}: {exc}")

    def add_folder(self, folder_path: str) -> dict:
        normalized = normalize_path(folder_path)
        if not os.path.isdir(normalized):
            raise ValueError(f"Folder does not exist: {normalized}")
        with self.lock:
            if normalized not in self.settings.monitored_folders:
                self.settings.monitored_folders.append(normalized)
                self.folder_progress[normalized] = self.create_folder_progress(normalized, phase="Waiting")
                self.save_settings()
            if self.settings.monitor_enabled:
                self.status_message = f"Monitoring {normalized}"
                self.scan_event.set()
            else:
                self.status_message = f"Added {normalized}; monitor is off"
        return self.snapshot()

    def remove_folder(self, folder_path: str) -> dict:
        normalized = normalize_path(folder_path)
        with self.lock:
            self.settings.monitored_folders = [
                folder for folder in self.settings.monitored_folders if folder != normalized
            ]
            self.folder_progress.pop(normalized, None)
            for path in list(self.items):
                if path_is_under(path, normalized) and path != self.current_item_path:
                    del self.items[path]
            for path in list(self.exclusions):
                if path_is_under(path, normalized):
                    del self.exclusions[path]
            self.save_settings()
            self.save_exclusions()
            self.status_message = f"Stopped monitoring {normalized}"
        self.scan_event.set()
        return self.snapshot()

    def update_settings(self, payload: dict) -> dict:
        should_scan = False
        with self.lock:
            if "monitor_enabled" in payload:
                was_enabled = self.settings.monitor_enabled
                self.settings.monitor_enabled = bool(payload["monitor_enabled"])
                if self.settings.monitor_enabled and not was_enabled:
                    should_scan = True
                    self.status_message = "Monitor is on"
                elif not self.settings.monitor_enabled:
                    self.scan_generation += 1
                    self.mark_folder_progress_paused_locked()
                    self.status_message = "Monitor is off"
                    self.scan_event.set()
            for key in ("normalize", "stereo", "replace", "convert"):
                if key in payload:
                    setattr(self.settings, key, bool(payload[key]))
            if "mb_min" in payload:
                self.settings.mb_min = min(max(safe_int(payload["mb_min"], self.settings.mb_min), 2), 45)
            if "threshold" in payload:
                self.settings.threshold = max(safe_float(payload["threshold"], self.settings.threshold), 0.0)
            if "encoder_mode" in payload:
                encoder_mode = str(payload["encoder_mode"])
                if encoder_mode in ENCODER_PROFILES:
                    self.settings.encoder_mode = encoder_mode
            if "temp_folder" in payload and str(payload["temp_folder"]).strip():
                temp_folder = str(payload["temp_folder"])
            else:
                temp_folder = None

        if temp_folder:
            self.set_cache_folder(temp_folder, persist=False)

        with self.lock:
            self.refresh_nonterminal_estimates_locked()
            self.save_settings()
        if should_scan:
            self.scan_event.set()
        return self.snapshot()

    def clear_queue(self) -> dict:
        with self.lock:
            self.scan_generation += 1
            current_item = self.items.get(self.current_item_path) if self.current_item_path else None
            self.items.clear()
            if current_item:
                self.items[self.current_item_path] = current_item
            self.folder_progress = {
                folder: self.create_folder_progress(folder, phase="Waiting")
                for folder in self.settings.monitored_folders
            }
            self.last_scan_started = None
            self.last_scan_finished = None
            self.last_scan_error = ""
            self.status_message = "Queue cleared"
            while True:
                try:
                    self.analysis_queue.get_nowait()
                except Empty:
                    break

            should_scan = self.settings.monitor_enabled
            if should_scan:
                self.status_message = "Queue cleared; restarting monitor scan"
                self.scan_event.set()
        return self.snapshot()

    def clear_exclusions(self) -> dict:
        with self.lock:
            self.exclusions.clear()
            for item in self.items.values():
                if item.get("excluded"):
                    item["excluded"] = False
                    item["status"] = "Queued"
                    item["error"] = ""
            self.save_exclusions()
            self.status_message = "Exclusions cleared"
            if self.settings.monitor_enabled:
                self.scan_generation += 1
                self.scan_event.set()
        return self.snapshot()

    def clear_exclusion(self, file_path: str) -> dict:
        normalized = normalize_path(file_path)
        with self.lock:
            self.exclusions.pop(normalized, None)
            item = self.items.get(normalized)
            if item and item.get("excluded"):
                item["excluded"] = False
                item["status"] = "Queued"
                item["error"] = ""
            self.save_exclusions()
            self.status_message = f"Exclusion cleared for {os.path.basename(normalized)}"
            if self.settings.monitor_enabled:
                self.scan_generation += 1
                self.scan_event.set()
        return self.snapshot()

    def refresh_nonterminal_estimates_locked(self):
        for item in self.items.values():
            if item.get("status") in ACTIVE_STATUSES or item.get("status") == "Completed":
                continue
            if item.get("excluded"):
                continue
            if item.get("source_info"):
                analysis = self.build_analysis_from_source_info(item["source_info"], item["size_mb"])
                self.apply_analysis_to_item_locked(item, analysis)
            elif item.get("status") in RECHECK_ON_SETTINGS_STATUSES:
                item["encoder"] = self.get_encoder_label(self.resolve_encoder_mode())

    def start_conversion(self) -> dict:
        with self.lock:
            self.run_event.set()
            self.abort_event.clear()
            self.status_message = "Conversion monitor is running"
        return self.snapshot()

    def stop_conversion(self, abort: bool = False) -> dict:
        with self.lock:
            self.run_event.clear()
            if abort:
                self.abort_event.set()
                process = self.current_process
                self.status_message = "Aborting current conversion"
            else:
                process = None
                self.status_message = "Conversion monitor will stop after the current file"

        if abort and process and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        return self.snapshot()

    def create_folder_progress(self, folder: str, phase: str = "Waiting") -> dict:
        return {
            "path": folder,
            "phase": phase,
            "total_files": 0,
            "processed_files": 0,
            "loaded_files": 0,
            "counted_files": 0,
            "current_path": "",
            "started_at": None,
            "finished_at": None,
        }

    def mark_folder_progress_paused_locked(self):
        for progress in self.folder_progress.values():
            if progress.get("phase") in {"Counting", "Loading"}:
                progress["phase"] = "Paused"

    def monitor_loop(self):
        while not self.shutdown_event.is_set():
            self.scan_event.wait(SCAN_INTERVAL_SECONDS)
            self.scan_event.clear()
            if self.shutdown_event.is_set():
                break
            with self.lock:
                monitor_enabled = self.settings.monitor_enabled
            if not monitor_enabled:
                continue
            self.scan_once()

    def scan_once(self):
        with self.lock:
            if not self.settings.monitor_enabled:
                return
            generation = self.scan_generation
            folders = list(self.settings.monitored_folders)
            cache_folder = self.settings.temp_folder
            self.last_scan_started = time.time()
            self.last_scan_error = ""
            self.status_message = "Scanning monitored folders"
            for folder in folders:
                self.folder_progress.setdefault(folder, self.create_folder_progress(folder, phase="Waiting"))

        discovered_paths: set[str] = set()
        completed_scan = False
        try:
            for folder in folders:
                if self.scan_cancelled(generation):
                    return
                if not os.path.isdir(folder):
                    with self.lock:
                        progress = self.folder_progress.setdefault(
                            folder,
                            self.create_folder_progress(folder),
                        )
                        progress.update(
                            {
                                "phase": "Missing",
                                "current_path": folder,
                                "finished_at": time.time(),
                            }
                        )
                    continue

                total_files = self.count_folder_videos(folder, cache_folder, generation)
                if total_files is None:
                    return

                with self.lock:
                    progress = self.folder_progress.setdefault(
                        folder,
                        self.create_folder_progress(folder),
                    )
                    progress.update(
                        {
                            "phase": "Loading",
                            "total_files": total_files,
                            "processed_files": 0,
                            "loaded_files": 0,
                            "current_path": folder,
                            "started_at": progress.get("started_at") or time.time(),
                            "finished_at": None,
                        }
                    )
                    self.status_message = f"Loading {folder}"

                for file_path, stat_result in self.iter_video_files(folder, cache_folder, generation):
                    if self.scan_cancelled(generation):
                        return
                    discovered_paths.add(file_path)
                    self.process_discovered_file(file_path, stat_result, folder, generation)

                with self.lock:
                    if self.scan_cancelled(generation):
                        return
                    progress = self.folder_progress.setdefault(
                        folder,
                        self.create_folder_progress(folder),
                    )
                    progress.update(
                        {
                            "phase": "Complete",
                            "processed_files": progress.get("total_files", 0),
                            "current_path": folder,
                            "finished_at": time.time(),
                        }
                    )
            completed_scan = True
        except Exception as exc:
            with self.lock:
                self.last_scan_error = str(exc)

        with self.lock:
            if not self.settings.monitor_enabled:
                return
            if not completed_scan:
                return
            current_paths = set(self.items)

            for file_path in list(current_paths - discovered_paths):
                if file_path == self.current_item_path:
                    continue
                if any(path_is_under(file_path, folder) for folder in folders):
                    del self.items[file_path]

            self.last_scan_finished = time.time()
            self.status_message = "Monitor is on"

    def count_folder_videos(self, folder: str, cache_folder: str, generation: int) -> int | None:
        count = 0
        with self.lock:
            progress = self.folder_progress.setdefault(folder, self.create_folder_progress(folder))
            progress.update(
                {
                    "phase": "Counting",
                    "total_files": 0,
                    "processed_files": 0,
                    "loaded_files": 0,
                    "counted_files": 0,
                    "current_path": folder,
                    "started_at": time.time(),
                    "finished_at": None,
                }
            )
            self.status_message = f"Counting {folder}"

        try:
            for root, dirs, files in os.walk(folder):
                if self.scan_cancelled(generation):
                    return None
                dirs[:] = [
                    dirname
                    for dirname in dirs
                    if not should_ignore_video(os.path.join(root, dirname), cache_folder)
                ]
                for file_name in files:
                    if self.scan_cancelled(generation):
                        return None
                    file_path = os.path.normpath(os.path.join(root, file_name))
                    if not is_video_path(file_path) or should_ignore_video(file_path, cache_folder):
                        continue
                    count += 1
                    if count == 1 or count % 25 == 0:
                        with self.lock:
                            progress = self.folder_progress.setdefault(
                                folder,
                                self.create_folder_progress(folder),
                            )
                            progress["counted_files"] = count
                            progress["total_files"] = count
                            progress["current_path"] = root
        except Exception as exc:
            with self.lock:
                self.last_scan_error = str(exc)
            return None

        with self.lock:
            progress = self.folder_progress.setdefault(folder, self.create_folder_progress(folder))
            progress["counted_files"] = count
            progress["total_files"] = count
            progress["current_path"] = folder
        return count

    def iter_video_files(self, folder: str, cache_folder: str, generation: int):
        for root, dirs, files in os.walk(folder):
            if self.scan_cancelled(generation):
                return
            dirs[:] = [
                dirname
                for dirname in dirs
                if not should_ignore_video(os.path.join(root, dirname), cache_folder)
            ]
            for file_name in files:
                if self.scan_cancelled(generation):
                    return
                file_path = os.path.normpath(os.path.join(root, file_name))
                if not is_video_path(file_path) or should_ignore_video(file_path, cache_folder):
                    continue
                try:
                    stat_result = os.stat(file_path)
                except OSError:
                    continue
                yield file_path, stat_result

    def process_discovered_file(
        self,
        file_path: str,
        stat_result: os.stat_result,
        folder: str,
        generation: int,
    ):
        action = self.prepare_discovered_file(file_path, stat_result, folder, generation)
        if action == "analyze":
            with self.lock:
                item = self.items.get(file_path)
                if not item:
                    return
                item["status"] = "Analyzing"
                fingerprint = item.get("fingerprint")
                size_mb = item.get("size_mb", 0.0)

            analysis = self.analyze_file(file_path, size_mb)

            with self.lock:
                item = self.items.get(file_path)
                if (
                    not item
                    or item.get("fingerprint") != fingerprint
                    or self.scan_generation != generation
                    or not self.settings.monitor_enabled
                ):
                    return
                if analysis is None:
                    item["status"] = "Error analyzing"
                    item["error"] = "ffprobe could not read this file"
                else:
                    self.apply_analysis_to_item_locked(item, analysis)
                progress = self.folder_progress.setdefault(folder, self.create_folder_progress(folder))
                progress["loaded_files"] = progress.get("loaded_files", 0) + 1

        with self.lock:
            progress = self.folder_progress.setdefault(folder, self.create_folder_progress(folder))
            progress["processed_files"] = min(
                progress.get("processed_files", 0) + 1,
                progress.get("total_files", 0) or progress.get("processed_files", 0) + 1,
            )
            progress["current_path"] = file_path

    def prepare_discovered_file(
        self,
        file_path: str,
        stat_result: os.stat_result,
        folder: str,
        generation: int,
    ) -> str:
        with self.lock:
            if not self.settings.monitor_enabled or self.scan_generation != generation:
                return "skip"

            fingerprint = self.fingerprint_for_stat(stat_result)
            size_bytes = stat_result.st_size
            exclusion = self.exclusions.get(file_path)
            if exclusion:
                if exclusion.get("source_size_bytes") == size_bytes:
                    item = self.items.get(file_path)
                    if item is None:
                        item = self.create_item(file_path, stat_result)
                        self.items[file_path] = item
                    item["excluded"] = True
                    item["status"] = "Excluded: output not smaller"
                    item["error"] = exclusion.get("reason", "Output was not smaller")
                    item["fingerprint"] = fingerprint
                    item["mtime"] = stat_result.st_mtime
                    item["size_mb"] = stat_result.st_size / (1024 * 1024)
                    return "skip"

                self.exclusions.pop(file_path, None)
                self.save_exclusions()

            existing = self.items.get(file_path)
            if existing is None:
                self.items[file_path] = self.create_item(file_path, stat_result)
                return "analyze"

            if existing.get("fingerprint") == fingerprint:
                if not existing.get("source_info") and existing.get("status") == "Queued":
                    return "analyze"
                return "skip"

            if (
                existing.get("status") == "Completed"
                and existing.get("suppress_change_until", 0) > time.time()
            ):
                existing["fingerprint"] = fingerprint
                existing["mtime"] = stat_result.st_mtime
                return "skip"

            if file_path == self.current_item_path:
                existing["pending_change"] = True
                return "skip"

            self.reset_item_for_change_locked(existing, stat_result)
            return "analyze"

    def scan_cancelled(self, generation: int | None = None) -> bool:
        with self.lock:
            generation_changed = generation is not None and self.scan_generation != generation
            if self.settings.monitor_enabled and not generation_changed:
                return False
            if generation_changed:
                self.status_message = "Scan restarting"
            else:
                self.status_message = "Monitor is off; scan paused"
            return True

    def fingerprint_for_stat(self, stat_result: os.stat_result) -> str:
        return f"{stat_result.st_size}:{getattr(stat_result, 'st_mtime_ns', int(stat_result.st_mtime * 1_000_000_000))}"

    def create_item(self, file_path: str, stat_result: os.stat_result) -> dict:
        size_mb = stat_result.st_size / (1024 * 1024)
        resolved_encoder = self.resolve_encoder_mode()
        return {
            "id": file_id_for_path(file_path),
            "path": file_path,
            "filename": os.path.basename(file_path),
            "folder": self.find_owning_folder_locked(file_path),
            "size_mb": size_mb,
            "mtime": stat_result.st_mtime,
            "fingerprint": self.fingerprint_for_stat(stat_result),
            "status": "Queued",
            "encoder": self.get_encoder_label(resolved_encoder),
            "video_codec_label": "",
            "resolution_label": "",
            "audio_label": "",
            "length_formatted": "",
            "mb_per_min_before": None,
            "estimated_seconds": None,
            "eta_seconds": None,
            "eta_display": "--",
            "elapsed_seconds": 0.0,
            "elapsed_display": "",
            "avg_speed_multiplier": 0.0,
            "avg_speed_display": "",
            "output_size_mb": None,
            "mb_per_min_after": None,
            "progress": 0.0,
            "source_info": None,
            "resolved_encoder": resolved_encoder,
            "error": "",
            "excluded": False,
            "suppress_change_until": 0.0,
        }

    def reset_item_for_change_locked(self, item: dict, stat_result: os.stat_result):
        item.update(
            {
                "size_mb": stat_result.st_size / (1024 * 1024),
                "mtime": stat_result.st_mtime,
                "fingerprint": self.fingerprint_for_stat(stat_result),
                "status": "Queued",
                "video_codec_label": "",
                "resolution_label": "",
                "audio_label": "",
                "length_formatted": "",
                "mb_per_min_before": None,
                "estimated_seconds": None,
                "eta_seconds": None,
                "eta_display": "--",
                "elapsed_seconds": 0.0,
                "elapsed_display": "",
                "avg_speed_multiplier": 0.0,
                "avg_speed_display": "",
                "output_size_mb": None,
                "mb_per_min_after": None,
                "progress": 0.0,
                "source_info": None,
                "resolved_encoder": self.resolve_encoder_mode(),
                "encoder": self.get_encoder_label(self.resolve_encoder_mode()),
                "error": "",
                "excluded": False,
            }
        )

    def find_owning_folder_locked(self, file_path: str) -> str:
        matches = [folder for folder in self.settings.monitored_folders if path_is_under(file_path, folder)]
        if not matches:
            return ""
        return max(matches, key=len)

    def analysis_loop(self):
        while not self.shutdown_event.is_set():
            with self.lock:
                monitor_enabled = self.settings.monitor_enabled
            if not monitor_enabled:
                time.sleep(0.25)
                continue

            try:
                file_path = self.analysis_queue.get(timeout=0.25)
            except Empty:
                continue

            with self.lock:
                item = self.items.get(file_path)
                if not self.settings.monitor_enabled:
                    self.analysis_queue.put(file_path)
                    continue
                if not item or item.get("status") in ACTIVE_STATUSES:
                    continue
                previous_status = item.get("status") or "Queued"
                item["status"] = "Analyzing"
                fingerprint = item.get("fingerprint")
                size_mb = item.get("size_mb", 0.0)

            analysis = self.analyze_file(file_path, size_mb)

            with self.lock:
                item = self.items.get(file_path)
                if not item or item.get("fingerprint") != fingerprint:
                    continue
                if not self.settings.monitor_enabled:
                    if item.get("status") == "Analyzing":
                        item["status"] = previous_status if previous_status != "Analyzing" else "Queued"
                    continue
                if analysis is None:
                    item["status"] = "Error analyzing"
                    item["error"] = "ffprobe could not read this file"
                else:
                    self.apply_analysis_to_item_locked(item, analysis)

    def analyze_file(self, file_path: str, size_mb: float) -> dict | None:
        source_info = self.probe_media_info(file_path)
        if not source_info:
            return None
        duration_seconds = source_info.get("duration_seconds")
        if not duration_seconds:
            return None
        return self.build_analysis_from_source_info(source_info, size_mb)

    def build_analysis_from_source_info(self, source_info: dict, size_mb: float) -> dict:
        duration_seconds = source_info.get("duration_seconds")
        resolved_encoder = self.resolve_encoder_mode()
        width = source_info.get("width") or 0
        height = source_info.get("height") or 0
        mb_per_min_before = self.calculate_mb_per_min(size_mb, duration_seconds)
        estimated_seconds = self.estimate_encode_seconds(source_info, resolved_encoder)
        audio_channels = source_info.get("audio_channels") or 0
        audio_codec = source_info.get("audio_codec") or "None"
        return {
            **source_info,
            "length_formatted": format_seconds(duration_seconds),
            "mb_per_min_before": mb_per_min_before,
            "estimated_seconds": estimated_seconds,
            "estimated_display": format_seconds(estimated_seconds),
            "estimated_output_size_mb": self.settings.mb_min * (duration_seconds / 60.0),
            "resolved_encoder": resolved_encoder,
            "encoder_label": self.get_encoder_label(resolved_encoder),
            "video_codec_label": (source_info.get("video_codec") or "Unknown").upper(),
            "resolution_label": f"{width}x{height}" if width and height else "--",
            "audio_label": f"{audio_codec.upper()} {audio_channels}ch" if audio_channels else audio_codec.upper(),
        }

    def apply_analysis_to_item_locked(self, item: dict, analysis: dict):
        item["source_info"] = {
            "duration_seconds": analysis.get("duration_seconds"),
            "video_codec": analysis.get("video_codec"),
            "audio_codec": analysis.get("audio_codec"),
            "audio_channels": analysis.get("audio_channels"),
            "width": analysis.get("width"),
            "height": analysis.get("height"),
        }
        item["resolved_encoder"] = analysis.get("resolved_encoder")
        item["encoder"] = analysis.get("encoder_label", "")
        item["video_codec_label"] = analysis.get("video_codec_label", "")
        item["resolution_label"] = analysis.get("resolution_label", "")
        item["audio_label"] = analysis.get("audio_label", "")
        item["length_formatted"] = analysis.get("length_formatted", "")
        item["mb_per_min_before"] = analysis.get("mb_per_min_before")
        item["estimated_seconds"] = analysis.get("estimated_seconds")
        item["eta_display"] = analysis.get("estimated_display", "--")
        item["error"] = ""
        self.refresh_item_eligibility_locked(item)

    def refresh_item_eligibility_locked(self, item: dict):
        if item.get("excluded"):
            item["status"] = "Excluded: output not smaller"
            return
        if item.get("status") in ACTIVE_STATUSES or item.get("status") == "Completed":
            return
        mb_per_min_before = item.get("mb_per_min_before")
        if mb_per_min_before is None:
            item["status"] = "Queued"
            return
        if mb_per_min_before < (self.settings.mb_min + self.settings.threshold):
            item["status"] = "Below threshold"
        else:
            item["status"] = "Ready"

    def probe_media_info(self, file_path: str) -> dict | None:
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-print_format",
                    "json",
                    "-show_format",
                    "-show_streams",
                    file_path,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=90,
                creationflags=CREATE_NO_WINDOW,
            )
            if result.returncode != 0:
                stderr_text = result.stderr.decode("utf-8", errors="replace").strip()
                print(f"Error getting media info for {file_path}: {stderr_text}")
                return None

            probe_data = json.loads(result.stdout.decode("utf-8", errors="replace"))
            format_info = probe_data.get("format", {})
            streams = probe_data.get("streams", [])
            video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
            audio_stream = next((stream for stream in streams if stream.get("codec_type") == "audio"), {})

            duration_seconds = safe_float(
                format_info.get("duration") or video_stream.get("duration") or audio_stream.get("duration")
            )
            return {
                "duration_seconds": duration_seconds,
                "video_codec": video_stream.get("codec_name") or "Unknown",
                "audio_codec": audio_stream.get("codec_name") or "None",
                "audio_channels": safe_int(audio_stream.get("channels")),
                "width": safe_int(video_stream.get("width")),
                "height": safe_int(video_stream.get("height")),
            }
        except Exception as exc:
            print(f"Exception getting media info for {file_path}: {exc}")
            return None

    def calculate_mb_per_min(self, size_mb: float, length_seconds: float) -> float:
        minutes = length_seconds / 60 if length_seconds else 0
        return size_mb / (minutes if minutes else 1)

    def estimate_encode_seconds(self, source_info: dict, encoder_key: str) -> float | None:
        duration_seconds = source_info.get("duration_seconds")
        if not duration_seconds:
            return None
        speed_multiplier = self.estimate_speed_multiplier(source_info, encoder_key)
        if speed_multiplier <= 0:
            return None
        return duration_seconds / speed_multiplier

    def estimate_speed_multiplier(self, source_info: dict, encoder_key: str) -> float:
        pixels = (source_info.get("width") or 0) * (source_info.get("height") or 0)
        weighted_total = 0.0
        total_weight = 0.0
        with self.lock:
            history = list(self.encode_history)
            normalize = self.settings.normalize
            stereo = self.settings.stereo

        for entry in reversed(history):
            if entry.get("encoder") != encoder_key:
                continue
            weight = 1.0
            entry_pixels = entry.get("pixels") or 0
            if pixels and entry_pixels:
                similarity = min(pixels, entry_pixels) / max(pixels, entry_pixels)
                weight += similarity
            if entry.get("normalize") == normalize:
                weight += 0.25
            if entry.get("stereo") == stereo:
                weight += 0.25
            weighted_total += entry.get("avg_speed", 0.0) * weight
            total_weight += weight
            if total_weight >= 8:
                break

        if total_weight > 0:
            return weighted_total / total_weight
        return ENCODER_PROFILES.get(encoder_key, {}).get("default_speed", 1.0)

    def conversion_loop(self):
        while not self.shutdown_event.is_set():
            if not self.run_event.wait(0.25):
                continue
            item = self.select_next_processable_item()
            if item is None:
                time.sleep(0.5)
                continue
            self.process_video(item["path"])

    def select_next_processable_item(self) -> dict | None:
        with self.lock:
            for item in sorted(self.items.values(), key=self.item_sort_key):
                if item.get("excluded"):
                    continue
                if item.get("status") in PROCESSABLE_STATUSES and os.path.exists(item.get("path", "")):
                    return dict(item)
            return None

    def item_sort_key(self, item: dict):
        status = item.get("status", "")
        if item.get("excluded"):
            status_priority = 5
        elif status in ACTIVE_STATUSES:
            status_priority = 0
        elif status in PROCESSABLE_STATUSES:
            status_priority = 1
        elif status in {"Queued", "Analyzing"}:
            status_priority = 2
        elif status in TERMINAL_STATUSES or status == "Completed":
            status_priority = 3
        elif status.startswith("Error") or status.startswith("Exception"):
            status_priority = 4
        else:
            status_priority = 2
        analyzed_priority = 0 if item.get("mb_per_min_before") is not None else 1
        score = item.get("mb_per_min_before")
        if score is None:
            score = item.get("size_mb", 0.0)
        return (status_priority, analyzed_priority, -score, item.get("filename", "").lower())

    def process_video(self, file_path: str):
        process = None
        cached_file_path = self.build_cached_input_path(file_path)
        output_file = self.build_cache_output_path(file_path)
        last_avg_speed_multiplier = 0.0
        length_seconds = None

        try:
            self.abort_event.clear()
            with self.lock:
                item = self.items.get(file_path)
                if not item or item.get("excluded") or item.get("status") not in PROCESSABLE_STATUSES:
                    return
                self.current_item_path = file_path
                self.current_cached_file_path = cached_file_path
                self.current_output_file = output_file
                item["status"] = "Probing"
                item["progress"] = 0.0
                item["elapsed_display"] = ""
                item["avg_speed_display"] = ""

            source_info = self.probe_media_info(file_path)
            with self.lock:
                item = self.items.get(file_path)
                if not item:
                    return
                if not source_info:
                    item["status"] = "Error analyzing"
                    item["error"] = "ffprobe could not read this file"
                    return
                analysis = self.build_analysis_from_source_info(source_info, item["size_mb"])
                self.apply_analysis_to_item_locked(item, analysis)
                item["status"] = "Checking thresholds"
                length_seconds = analysis["duration_seconds"]
                mb_per_min_before = analysis["mb_per_min_before"]
                mb_min_target = self.settings.mb_min
                threshold = self.settings.threshold
                replace_original = self.settings.replace
                resolved_encoder = analysis["resolved_encoder"]
                normalize = self.settings.normalize
                stereo = self.settings.stereo
                convert = self.settings.convert
                source_size_mb = item["size_mb"]

            if mb_per_min_before < (mb_min_target + threshold):
                with self.lock:
                    item = self.items.get(file_path)
                    if item:
                        item["output_size_mb"] = item["size_mb"]
                        item["mb_per_min_after"] = mb_per_min_before
                        item["eta_seconds"] = 0.0
                        item["eta_display"] = "00:00:00"
                        item["elapsed_display"] = ""
                        item["avg_speed_display"] = ""
                        item["progress"] = 0.0
                        item["status"] = "Skipped"
                return

            if self.abort_event.is_set():
                self.mark_aborted(file_path, cached_file_path, output_file)
                return

            if not os.path.exists(cached_file_path):
                with self.lock:
                    self.items[file_path]["status"] = "Copying to cache"
                shutil.copy2(file_path, cached_file_path)

            target_bitrate = (mb_min_target * 1024 * 1024 * 8) / 60 * 0.9
            audio_bitrate = 192 * 1024 if (convert or normalize or stereo) else 0
            video_bitrate = max(target_bitrate - audio_bitrate, 100 * 1024)
            cmd = self.build_ffmpeg_command(
                cached_file_path,
                output_file,
                resolved_encoder,
                video_bitrate,
                normalize=normalize,
                stereo=stereo,
                convert=convert,
            )

            with self.lock:
                item = self.items.get(file_path)
                if not item:
                    return
                item["encoder"] = self.get_encoder_label(resolved_encoder)
                item["status"] = "Launching encoder"
                item["status"] = "Processing"

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=CREATE_NO_WINDOW,
            )
            with self.lock:
                self.current_process = process

            queue: Queue[str] = Queue()
            threading.Thread(target=self.enqueue_output, args=(process.stderr, queue), daemon=True).start()
            start_time = time.time()
            current_seconds = 0.0
            last_speed_multiplier = 0.0

            while True:
                if self.abort_event.is_set():
                    self.terminate_process(process)
                    self.mark_aborted(file_path, cached_file_path, output_file)
                    return

                try:
                    line = queue.get(timeout=0.1)
                except Empty:
                    if process.poll() is not None:
                        break
                    continue

                parsed_time = self.parse_progress_time(line)
                if parsed_time is not None and length_seconds:
                    current_seconds = parsed_time
                    progress = min((current_seconds / length_seconds) * 100, 100.0)
                    with self.lock:
                        if file_path in self.items:
                            self.items[file_path]["progress"] = progress

                parsed_speed = self.parse_speed(line)
                if parsed_speed is not None:
                    _, last_speed_multiplier = parsed_speed

                if current_seconds and length_seconds:
                    elapsed_seconds = max(time.time() - start_time, 0.0)
                    last_avg_speed_multiplier = current_seconds / elapsed_seconds if elapsed_seconds else 0.0
                    eta_seconds = None
                    if last_speed_multiplier > 0:
                        eta_seconds = max((length_seconds - current_seconds) / last_speed_multiplier, 0.0)
                    with self.lock:
                        item = self.items.get(file_path)
                        if item:
                            item["eta_seconds"] = eta_seconds
                            item["eta_display"] = format_seconds(eta_seconds)
                            item["elapsed_seconds"] = elapsed_seconds
                            item["elapsed_display"] = format_seconds(elapsed_seconds)
                            item["avg_speed_multiplier"] = last_avg_speed_multiplier
                            item["avg_speed_display"] = format_speed(last_avg_speed_multiplier)

            process.wait()
            if process.returncode != 0:
                self.delete_path(output_file)
                self.delete_path(cached_file_path)
                with self.lock:
                    if file_path in self.items:
                        self.items[file_path]["status"] = "Error: See log"
                return

            with self.lock:
                if file_path in self.items:
                    self.items[file_path]["status"] = "Finalizing"

            output_size_mb = os.path.getsize(output_file) / (1024 * 1024)
            mb_per_min_after = self.calculate_mb_per_min(output_size_mb, length_seconds)
            output_length = self.get_video_length(output_file)
            length_check = output_length is not None and abs(output_length - length_seconds) <= 8
            size_check = output_size_mb < source_size_mb

            if not (length_check and size_check):
                self.delete_path(output_file)
                self.delete_path(cached_file_path)
                reason_parts = []
                if not length_check:
                    reason_parts.append("length mismatch")
                if not size_check:
                    reason_parts.append("output not smaller")
                with self.lock:
                    if file_path in self.items:
                        if not size_check:
                            self.exclude_item_locked(
                                self.items[file_path],
                                reason="Output was not smaller",
                                output_size_mb=output_size_mb,
                                mb_per_min_after=mb_per_min_after,
                            )
                        else:
                            self.items[file_path]["status"] = f"Error: {', '.join(reason_parts)}"
                return

            with self.lock:
                item = self.items.get(file_path)
                if item:
                    item["output_size_mb"] = output_size_mb
                    item["mb_per_min_after"] = mb_per_min_after
                    item["eta_seconds"] = 0.0
                    item["eta_display"] = "00:00:00"
                    item["elapsed_seconds"] = time.time() - start_time
                    item["elapsed_display"] = format_seconds(time.time() - start_time)
                    item["avg_speed_multiplier"] = last_avg_speed_multiplier
                    item["avg_speed_display"] = format_speed(last_avg_speed_multiplier)
                    item["progress"] = 100.0

            if replace_original:
                with self.lock:
                    if file_path in self.items:
                        self.items[file_path]["status"] = "Replacing"
                if not self.replace_file(file_path, output_file):
                    self.delete_path(cached_file_path)
                    with self.lock:
                        if file_path in self.items:
                            self.items[file_path]["status"] = "Error: Failed to replace file"
                    return
            else:
                with self.lock:
                    if file_path in self.items:
                        self.items[file_path]["status"] = "Moving output"
                final_output_path = self.build_final_output_path(file_path)
                try:
                    shutil.move(output_file, final_output_path)
                except Exception as exc:
                    print(f"Error moving processed file to {final_output_path}: {exc}")
                    self.delete_path(cached_file_path)
                    with self.lock:
                        if file_path in self.items:
                            self.items[file_path]["status"] = "Error: Failed to move processed file"
                    return

            self.record_encode_history(source_info, resolved_encoder, last_avg_speed_multiplier)
            self.delete_path(cached_file_path)
            refreshed_info = self.probe_media_info(file_path) if replace_original else None
            with self.lock:
                item = self.items.get(file_path)
                if item:
                    try:
                        stat_result = os.stat(file_path)
                        item["fingerprint"] = self.fingerprint_for_stat(stat_result)
                        if replace_original:
                            item["size_mb"] = stat_result.st_size / (1024 * 1024)
                            if refreshed_info:
                                refreshed_analysis = self.build_analysis_from_source_info(refreshed_info, item["size_mb"])
                                self.apply_analysis_to_item_locked(item, refreshed_analysis)
                    except OSError:
                        pass
                    item["output_size_mb"] = output_size_mb
                    item["mb_per_min_after"] = mb_per_min_after
                    item["status"] = "Completed"
                    item["suppress_change_until"] = time.time() + 30
                    item["error"] = ""

        except Exception as exc:
            print(f"Exception processing {file_path}: {exc}")
            self.delete_path(output_file)
            self.delete_path(cached_file_path)
            with self.lock:
                if file_path in self.items:
                    self.items[file_path]["status"] = f"Exception: {exc}"
        finally:
            if process and process.stderr:
                try:
                    process.stderr.close()
                except Exception:
                    pass
            with self.lock:
                self.current_process = None
                self.current_output_file = None
                self.current_cached_file_path = None
                self.current_item_path = None
                self.abort_event.clear()
            self.scan_event.set()

    def build_cached_input_path(self, file_path: str) -> str:
        return os.path.join(self.settings.temp_folder, f"{file_id_for_path(file_path)[:12]}_{os.path.basename(file_path)}")

    def build_cache_output_path(self, file_path: str) -> str:
        base_name, extension = os.path.splitext(os.path.basename(file_path))
        return os.path.join(self.settings.temp_folder, f"{file_id_for_path(file_path)[:12]}_{base_name}_processed{extension}")

    def build_final_output_path(self, file_path: str) -> str:
        source_dir = os.path.dirname(file_path)
        base_name, extension = os.path.splitext(os.path.basename(file_path))
        candidate = os.path.join(source_dir, f"{base_name}_processed{extension}")
        if not os.path.exists(candidate):
            return candidate
        counter = 1
        while True:
            candidate = os.path.join(source_dir, f"{base_name}_processed_{counter}{extension}")
            if not os.path.exists(candidate):
                return candidate
            counter += 1

    def build_ffmpeg_command(
        self,
        input_path: str,
        output_path: str,
        resolved_encoder: str,
        video_bitrate: float,
        *,
        normalize: bool,
        stereo: bool,
        convert: bool,
    ) -> list[str]:
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-i",
            input_path,
            "-map",
            "-0:d?",
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-map",
            "0:s?",
        ]
        cmd.extend(self.build_video_args(resolved_encoder, video_bitrate))
        cmd.extend(self.build_audio_args(normalize=normalize, stereo=stereo, convert=convert))
        cmd.extend(["-c:s", "copy"])
        cmd.extend(["-y", output_path])
        return cmd

    def build_video_args(self, encoder_key: str, video_bitrate: float) -> list[str]:
        bitrate_kbps = max(int(video_bitrate / 1000), 100)
        buffer_kbps = max(int(video_bitrate / 500), 200)
        return [
            "-c:v",
            encoder_key,
            "-b:v",
            f"{bitrate_kbps}k",
            "-maxrate",
            f"{bitrate_kbps}k",
            "-bufsize",
            f"{buffer_kbps}k",
        ]

    def build_audio_args(self, *, normalize: bool, stereo: bool, convert: bool) -> list[str]:
        needs_audio_processing = convert or normalize or stereo
        if not needs_audio_processing:
            return ["-c:a", "copy"]
        args = ["-c:a", "aac", "-b:a", "192k"]
        if normalize:
            args.extend(["-af", "dynaudnorm"])
        if stereo:
            args.extend(["-ac", "2"])
        return args

    def enqueue_output(self, stream, queue: Queue[str]):
        try:
            buffer = ""
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="replace")
                parts = re.split(r"[\r\n]+", buffer)
                buffer = parts.pop() if parts else ""
                for line in parts:
                    if line:
                        queue.put(line)
            if buffer.strip():
                queue.put(buffer)
        except Exception as exc:
            print(f"Error in enqueue_output: {exc}")
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def parse_progress_time(self, line: str) -> float | None:
        match = TIME_PATTERN.search(line)
        if not match:
            return None
        hours, minutes, seconds = match.groups()
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    def parse_speed(self, line: str) -> tuple[str, float] | None:
        match = SPEED_PATTERN.search(line)
        if not match:
            return None
        speed_multiplier = safe_float(match.group(1))
        if speed_multiplier <= 0:
            return None
        return f"{speed_multiplier:.2f}x", speed_multiplier

    def get_video_length(self, file_path: str) -> float | None:
        source_info = self.probe_media_info(file_path)
        if source_info:
            return source_info.get("duration_seconds")
        return None

    def terminate_process(self, process: subprocess.Popen):
        if process and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

    def mark_aborted(self, file_path: str, cached_file_path: str, output_file: str):
        self.delete_path(output_file)
        self.delete_path(cached_file_path)
        with self.lock:
            item = self.items.get(file_path)
            if item:
                item["status"] = "Stopped"
                item["progress"] = 0.0
                item["eta_seconds"] = None
                item["eta_display"] = "--"
                item["elapsed_display"] = ""
                item["avg_speed_display"] = ""

    def exclude_item_locked(
        self,
        item: dict,
        *,
        reason: str,
        output_size_mb: float | None = None,
        mb_per_min_after: float | None = None,
    ):
        file_path = item["path"]
        size_bytes = None
        try:
            size_bytes = os.path.getsize(file_path)
        except OSError:
            size_bytes = int((item.get("size_mb") or 0.0) * 1024 * 1024)

        item["excluded"] = True
        item["status"] = "Excluded: output not smaller"
        item["error"] = reason
        item["output_size_mb"] = output_size_mb
        item["mb_per_min_after"] = mb_per_min_after
        item["progress"] = 0.0
        item["eta_seconds"] = None
        item["eta_display"] = "--"

        self.exclusions[file_path] = {
            "path": file_path,
            "filename": item.get("filename") or os.path.basename(file_path),
            "reason": reason,
            "source_size_bytes": size_bytes,
            "source_size_mb": item.get("size_mb"),
            "output_size_mb": output_size_mb,
            "mb_per_min_before": item.get("mb_per_min_before"),
            "mb_per_min_after": mb_per_min_after,
            "length_formatted": item.get("length_formatted"),
            "created_at": time.time(),
        }
        self.save_exclusions()

    def delete_path(self, path: str | None):
        if not path:
            return
        for attempt in range(3):
            try:
                if os.path.exists(path):
                    os.chmod(path, 0o666)
                    os.remove(path)
                return
            except PermissionError:
                time.sleep(0.5)
            except Exception as exc:
                print(f"Error deleting {path}: {exc}")
                return

    def replace_file(self, original_path: str, new_path: str) -> bool:
        try:
            if not os.access(original_path, os.W_OK):
                os.chmod(original_path, 0o666)
            if os.path.exists(new_path) and not os.access(new_path, os.W_OK):
                os.chmod(new_path, 0o666)

            if os.name == "nt":
                import ctypes

                file_attribute_archive = 0x20
                current_attributes = ctypes.windll.kernel32.GetFileAttributesW(original_path)
                if current_attributes & file_attribute_archive:
                    ctypes.windll.kernel32.SetFileAttributesW(
                        original_path,
                        current_attributes & ~file_attribute_archive,
                    )

            original_drive = os.path.splitdrive(os.path.abspath(original_path))[0].lower()
            new_drive = os.path.splitdrive(os.path.abspath(new_path))[0].lower()
            if original_drive == new_drive:
                os.replace(new_path, original_path)
            else:
                backup_path = self.build_backup_path(original_path)
                os.replace(original_path, backup_path)
                try:
                    shutil.move(new_path, original_path)
                except Exception:
                    if os.path.exists(backup_path):
                        os.replace(backup_path, original_path)
                    raise
                else:
                    if os.path.exists(backup_path):
                        os.remove(backup_path)
            return True
        except Exception as exc:
            print(f"Error replacing file {original_path}: {exc}")
            return False

    def build_backup_path(self, original_path: str) -> str:
        base_path = f"{original_path}.ez_ffmpeg_backup"
        if not os.path.exists(base_path):
            return base_path
        counter = 1
        while True:
            candidate = f"{base_path}_{counter}"
            if not os.path.exists(candidate):
                return candidate
            counter += 1

    def record_encode_history(self, source_info: dict, encoder_key: str, avg_speed_multiplier: float):
        if avg_speed_multiplier <= 0:
            return
        entry = {
            "encoder": encoder_key,
            "pixels": (source_info.get("width") or 0) * (source_info.get("height") or 0),
            "duration_seconds": source_info.get("duration_seconds"),
            "normalize": self.settings.normalize,
            "stereo": self.settings.stereo,
            "avg_speed": avg_speed_multiplier,
            "timestamp": time.time(),
        }
        with self.lock:
            self.encode_history.append(entry)
            self.encode_history = self.encode_history[-MAX_HISTORY_ITEMS:]
            self.save_encode_history()

    def sorted_items_locked(self) -> list[dict]:
        rows = [self.public_item(item) for item in self.items.values() if not item.get("excluded")]
        rows.sort(key=self.item_sort_key)
        return rows

    def sorted_exclusions_locked(self) -> list[dict]:
        rows = []
        for path, exclusion in self.exclusions.items():
            item = self.items.get(path)
            if item:
                row = self.public_item(item)
            else:
                row = {
                    "id": file_id_for_path(path),
                    "path": path,
                    "filename": exclusion.get("filename") or os.path.basename(path),
                    "status": "Excluded: output not smaller",
                    "error": exclusion.get("reason", "Output was not smaller"),
                    "size_mb": exclusion.get("source_size_mb"),
                    "mb_per_min_before": exclusion.get("mb_per_min_before"),
                    "length_formatted": exclusion.get("length_formatted"),
                    "output_size_mb": exclusion.get("output_size_mb"),
                    "mb_per_min_after": exclusion.get("mb_per_min_after"),
                    "excluded": True,
                }
            row["excluded_at"] = exclusion.get("created_at")
            rows.append(row)
        rows.sort(key=lambda row: (row.get("filename") or "").lower())
        return rows

    def public_item(self, item: dict) -> dict:
        public = {key: value for key, value in item.items() if key not in {"source_info", "fingerprint"}}
        public["is_current"] = item.get("path") == self.current_item_path
        return public

    def build_summary_locked(self) -> dict:
        total_remaining_seconds = 0.0
        current_eta = "--"
        completed = 0
        skipped = 0
        failed = 0
        processing = 0
        queued = 0
        saved_mb = 0.0

        for item in self.items.values():
            if item.get("excluded"):
                continue
            status = item.get("status", "Queued")
            if status == "Completed":
                completed += 1
            elif status in {"Skipped", "Below threshold"}:
                skipped += 1
            elif status.startswith("Error") or status.startswith("Exception"):
                failed += 1
            elif status in ACTIVE_STATUSES:
                processing += 1
            else:
                queued += 1

            if item.get("output_size_mb") is not None:
                saved_mb += max(item.get("size_mb", 0.0) - item.get("output_size_mb", 0.0), 0.0)

            if status == "Processing":
                if item.get("eta_seconds") is not None:
                    total_remaining_seconds += item["eta_seconds"]
                    current_eta = item.get("eta_display", "--")
                elif item.get("estimated_seconds"):
                    total_remaining_seconds += item["estimated_seconds"]
                    current_eta = format_seconds(item["estimated_seconds"])
            elif status in PROCESSABLE_STATUSES:
                if item.get("estimated_seconds"):
                    total_remaining_seconds += item["estimated_seconds"]

        finish_text = "--"
        if total_remaining_seconds > 0:
            finish_at = datetime.now() + timedelta(seconds=total_remaining_seconds)
            finish_text = finish_at.strftime("%I:%M %p").lstrip("0")

        return {
            "current_eta": current_eta,
            "queue_remaining": format_seconds(total_remaining_seconds),
            "finish": finish_text,
            "queued": queued,
            "processing": processing,
            "completed": completed,
            "skipped": skipped,
            "failed": failed,
            "saved_mb": saved_mb,
        }

    def snapshot(self) -> dict:
        with self.lock:
            folder_progress = []
            for folder in self.settings.monitored_folders:
                progress = self.folder_progress.get(folder) or self.create_folder_progress(folder)
                total_files = progress.get("total_files", 0) or 0
                processed_files = progress.get("processed_files", 0) or 0
                percent = None
                if total_files > 0 and progress.get("phase") != "Counting":
                    percent = min((processed_files / total_files) * 100, 100.0)
                folder_progress.append({**progress, "percent": percent})
            return {
                "settings": asdict(self.settings),
                "encoder_options": self.encoder_options(),
                "folders": list(self.settings.monitored_folders),
                "folder_progress": folder_progress,
                "items": self.sorted_items_locked(),
                "excluded_items": self.sorted_exclusions_locked(),
                "summary": self.build_summary_locked(),
                "run_enabled": self.run_event.is_set(),
                "processing_active": self.current_item_path is not None,
                "current_path": self.current_item_path,
                "status_message": self.status_message,
                "ffmpeg_available": self.ffmpeg_available,
                "last_scan_started": self.last_scan_started,
                "last_scan_finished": self.last_scan_finished,
                "last_scan_error": self.last_scan_error,
            }


ENGINE = EZFfmpegService()


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "EZFfmpegService/1.0"

    def log_message(self, format_string, *args):
        print(f"{self.address_string()} - {format_string % args}")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/state":
            self.send_json(ENGINE.snapshot())
            return
        if parsed.path == "/api/browse":
            query = urllib.parse.parse_qs(parsed.query)
            browse_path = query.get("path", [""])[0]
            try:
                self.send_json(browse_directory(browse_path))
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        if parsed.path in {"", "/"}:
            self.serve_static(STATIC_ROOT / "index.html", "text/html; charset=utf-8")
            return
        if parsed.path.startswith("/static/"):
            relative = parsed.path.removeprefix("/static/")
            target = (STATIC_ROOT / relative).resolve()
            if not str(target).startswith(str(STATIC_ROOT.resolve())):
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            self.serve_static(target)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        try:
            payload = self.read_json()
            if parsed.path == "/api/folders":
                self.send_json(ENGINE.add_folder(str(payload.get("path", ""))))
                return
            if parsed.path == "/api/folders/remove":
                self.send_json(ENGINE.remove_folder(str(payload.get("path", ""))))
                return
            if parsed.path == "/api/settings":
                self.send_json(ENGINE.update_settings(payload))
                return
            if parsed.path == "/api/run":
                enabled = bool(payload.get("enabled"))
                if enabled:
                    self.send_json(ENGINE.start_conversion())
                else:
                    self.send_json(ENGINE.stop_conversion(abort=payload.get("mode") == "abort"))
                return
            if parsed.path == "/api/queue/clear":
                self.send_json(ENGINE.clear_queue())
                return
            if parsed.path == "/api/exclusions/clear":
                self.send_json(ENGINE.clear_exclusions())
                return
            if parsed.path == "/api/exclusions/remove":
                self.send_json(ENGINE.clear_exclusion(str(payload.get("path", ""))))
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            print(f"Request failed: {exc}")
            self.send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def read_json(self) -> dict:
        length = safe_int(self.headers.get("Content-Length"), 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK):
        encoded = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def serve_static(self, path: Path, content_type: str | None = None):
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = path.read_bytes()
        if content_type is None:
            content_type = self.guess_content_type(path)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def guess_content_type(self, path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == ".css":
            return "text/css; charset=utf-8"
        if suffix == ".js":
            return "application/javascript; charset=utf-8"
        if suffix == ".html":
            return "text/html; charset=utf-8"
        return "application/octet-stream"


def main():
    server = ThreadingHTTPServer((HOST, PORT), RequestHandler)
    print(f"EZ_ffmpeg service running at http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping service...")
    finally:
        ENGINE.shutdown_event.set()
        ENGINE.run_event.clear()
        if ENGINE.current_process and ENGINE.current_process.poll() is None:
            ENGINE.stop_conversion(abort=True)
        server.server_close()


if __name__ == "__main__":
    main()
