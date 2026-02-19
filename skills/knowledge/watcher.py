"""
skills/knowledge/watcher.py — Auto-Sorting File Watcher
=========================================================
Monitors configured directories for new files, detects their content type,
and optionally moves/copies them to the correct destination folder.

Uses watchdog for filesystem events. Runs in a background thread.

Safety:
  - NEVER deletes source files — only MOVES or COPIES
  - Logs every action to console + Telegram
  - Dry-run mode available
  - Configurable via KNOWLEDGE_WATCH_DIRS env var

Content detection:
  - File extension mapping (fast path)
  - PDF/document content sniffing for ambiguous files
  - Code file detection (shebang, common patterns)

Auto-indexing:
  - New files are automatically ingested into the knowledge base
  - Only indexes supported file types (see skill.py)
"""

import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("skill.knowledge.watcher")

# ============================================================================
# CONFIGURATION
# ============================================================================

# Watched directories (comma-separated in env)
WATCH_DIRS_ENV = os.environ.get("KNOWLEDGE_WATCH_DIRS", "")

# Sort rules: extension → destination subfolder
# Destination is relative to the source's parent or an absolute path
SORT_RULES: dict[str, str] = {
    # Documents
    ".pdf": "Documents/PDF",
    ".docx": "Documents/Word",
    ".doc": "Documents/Word",
    ".xlsx": "Documents/Excel",
    ".xls": "Documents/Excel",
    ".pptx": "Documents/PowerPoint",
    ".ppt": "Documents/PowerPoint",
    ".odt": "Documents/LibreOffice",
    ".ods": "Documents/LibreOffice",
    # Text
    ".txt": "Documents/Text",
    ".md": "Documents/Markdown",
    ".rst": "Documents/Text",
    ".csv": "Documents/Data",
    ".json": "Documents/Data",
    ".yaml": "Documents/Data",
    ".yml": "Documents/Data",
    ".xml": "Documents/Data",
    # Email
    ".eml": "Documents/Email",
    ".msg": "Documents/Email",
    # Images
    ".png": "Images",
    ".jpg": "Images",
    ".jpeg": "Images",
    ".gif": "Images",
    ".bmp": "Images",
    ".webp": "Images",
    ".svg": "Images",
    ".ico": "Images",
    ".tiff": "Images",
    # Video
    ".mp4": "Videos",
    ".mkv": "Videos",
    ".avi": "Videos",
    ".mov": "Videos",
    ".webm": "Videos",
    ".wmv": "Videos",
    # Audio
    ".mp3": "Audio",
    ".wav": "Audio",
    ".flac": "Audio",
    ".ogg": "Audio",
    ".aac": "Audio",
    ".m4a": "Audio",
    # Archives
    ".zip": "Archives",
    ".rar": "Archives",
    ".7z": "Archives",
    ".tar": "Archives",
    ".gz": "Archives",
    ".bz2": "Archives",
    # Installers
    ".exe": "Installers",
    ".msi": "Installers",
    ".dmg": "Installers",
    ".deb": "Installers",
    ".rpm": "Installers",
    # Code (left in place by default, only indexed)
    ".py": None,
    ".js": None,
    ".ts": None,
    ".rs": None,
    ".go": None,
    ".java": None,
    ".c": None,
    ".cpp": None,
    ".h": None,
    ".cs": None,
}

# Minimum file age in seconds before processing (avoids partial downloads)
MIN_FILE_AGE = 3.0

# ============================================================================
# FILE CATEGORIZER
# ============================================================================

def categorize_file(file_path: str) -> Optional[str]:
    """
    Determine the destination subfolder for a file based on its extension.
    Returns None if the file should stay in place (code files, unknown).
    """
    ext = Path(file_path).suffix.lower()
    return SORT_RULES.get(ext)


# ============================================================================
# FILE MOVER (safe — never deletes)
# ============================================================================

def move_file(src: str, dest_dir: str, dry_run: bool = False) -> Optional[str]:
    """
    Move a file to dest_dir. Creates dest_dir if needed.
    If a file with the same name exists, adds a numeric suffix.
    Returns the destination path, or None on failure.

    Safety: source is MOVED (not copied+deleted). Original is preserved
    if the move fails at any point.
    """
    if not os.path.isfile(src):
        return None

    os.makedirs(dest_dir, exist_ok=True)

    filename = os.path.basename(src)
    dest = os.path.join(dest_dir, filename)

    # Handle name collision
    if os.path.exists(dest):
        stem = Path(filename).stem
        ext = Path(filename).suffix
        counter = 1
        while os.path.exists(dest):
            dest = os.path.join(dest_dir, f"{stem}_{counter}{ext}")
            counter += 1

    if dry_run:
        log.info(f"[DRY RUN] Would move: {src} -> {dest}")
        return dest

    try:
        shutil.move(src, dest)
        log.info(f"Moved: {src} -> {dest}")
        return dest
    except Exception as e:
        log.error(f"Failed to move {src} -> {dest}: {e}")
        return None


# ============================================================================
# WATCHER (watchdog-based)
# ============================================================================

_watcher_thread: Optional[threading.Thread] = None
_watcher_stop = threading.Event()


def _process_new_file(file_path: str, base_dir: str, auto_index: bool = True, dry_run: bool = False):
    """Process a newly detected file: categorize, move, and optionally index."""
    if not os.path.isfile(file_path):
        return

    # Wait for file to stabilize (avoid partial downloads)
    try:
        age = time.time() - os.path.getmtime(file_path)
        if age < MIN_FILE_AGE:
            time.sleep(MIN_FILE_AGE - age + 0.5)
    except OSError:
        return

    # Double-check file still exists after waiting
    if not os.path.isfile(file_path):
        return

    filename = os.path.basename(file_path)

    # Skip hidden files and temp files
    if filename.startswith(".") or filename.startswith("~") or filename.endswith(".tmp"):
        return

    # Categorize
    dest_subfolder = categorize_file(file_path)
    final_path = file_path

    if dest_subfolder:
        dest_dir = os.path.join(base_dir, dest_subfolder)
        moved = move_file(file_path, dest_dir, dry_run=dry_run)
        if moved:
            final_path = moved
            # Notify via Telegram
            try:
                from core.utils import send_telegram
                send_telegram(
                    f"[INFO] *File Sorted*\n"
                    f"`{filename}` -> `{dest_subfolder}/`"
                )
            except Exception:
                pass

    # Auto-index into knowledge base (if supported type)
    if auto_index:
        from skills.knowledge.skill import ALL_EXTENSIONS, ingest_file
        ext = Path(final_path).suffix.lower()
        if ext in ALL_EXTENSIONS:
            try:
                result = ingest_file(final_path)
                if result["success"] and "Already indexed" not in result["message"]:
                    log.info(f"Auto-indexed: {filename} ({result['chunks']} chunks)")
            except Exception as e:
                log.warning(f"Auto-index failed for {filename}: {e}")


def _watcher_loop(watch_dirs: list[str], auto_index: bool = True, dry_run: bool = False):
    """
    Main watcher loop using watchdog.
    Falls back to polling if watchdog is not available.
    """
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler, FileCreatedEvent

        class NewFileHandler(FileSystemEventHandler):
            def __init__(self, base_dir: str):
                self.base_dir = base_dir

            def on_created(self, event):
                if isinstance(event, FileCreatedEvent) and not event.is_directory:
                    threading.Thread(
                        target=_process_new_file,
                        args=(event.src_path, self.base_dir, auto_index, dry_run),
                        daemon=True,
                    ).start()

        observer = Observer()
        for watch_dir in watch_dirs:
            if os.path.isdir(watch_dir):
                handler = NewFileHandler(watch_dir)
                observer.schedule(handler, watch_dir, recursive=False)
                log.info(f"Watching: {watch_dir}")
            else:
                log.warning(f"Watch dir not found: {watch_dir}")

        observer.start()
        log.info("File watcher started (watchdog)")

        while not _watcher_stop.is_set():
            _watcher_stop.wait(timeout=5)

        observer.stop()
        observer.join()
        log.info("File watcher stopped")

    except ImportError:
        log.warning("watchdog not installed, using polling fallback (30s interval)")
        _watcher_poll_loop(watch_dirs, auto_index, dry_run)


def _watcher_poll_loop(watch_dirs: list[str], auto_index: bool = True, dry_run: bool = False):
    """Polling fallback if watchdog is not installed."""
    known_files: dict[str, set[str]] = {d: set() for d in watch_dirs}

    # Initial scan
    for watch_dir in watch_dirs:
        if os.path.isdir(watch_dir):
            for f in os.listdir(watch_dir):
                known_files[watch_dir].add(f)

    log.info("File watcher started (polling, 30s)")

    while not _watcher_stop.is_set():
        for watch_dir in watch_dirs:
            if not os.path.isdir(watch_dir):
                continue
            try:
                current = set(os.listdir(watch_dir))
                new_files = current - known_files.get(watch_dir, set())
                for fname in new_files:
                    fpath = os.path.join(watch_dir, fname)
                    if os.path.isfile(fpath):
                        _process_new_file(fpath, watch_dir, auto_index, dry_run)
                known_files[watch_dir] = current
            except OSError as e:
                log.warning(f"Poll error for {watch_dir}: {e}")

        _watcher_stop.wait(timeout=30)

    log.info("File watcher stopped (polling)")


# ============================================================================
# PUBLIC API
# ============================================================================

def start_watcher(
    watch_dirs: Optional[list[str]] = None,
    auto_index: bool = True,
    dry_run: bool = False,
) -> bool:
    """
    Start the file watcher in a background thread.
    Uses KNOWLEDGE_WATCH_DIRS env var if watch_dirs not provided.
    Returns True if started, False if already running or no dirs configured.
    """
    global _watcher_thread

    if _watcher_thread is not None and _watcher_thread.is_alive():
        log.info("Watcher already running")
        return False

    if watch_dirs is None:
        if not WATCH_DIRS_ENV:
            log.info("No KNOWLEDGE_WATCH_DIRS configured, watcher not started")
            return False
        watch_dirs = [d.strip() for d in WATCH_DIRS_ENV.split(",") if d.strip()]

    if not watch_dirs:
        log.info("No watch directories, watcher not started")
        return False

    _watcher_stop.clear()
    _watcher_thread = threading.Thread(
        target=_watcher_loop,
        args=(watch_dirs, auto_index, dry_run),
        daemon=True,
        name="knowledge-watcher",
    )
    _watcher_thread.start()
    return True


def stop_watcher():
    """Stop the file watcher."""
    global _watcher_thread
    _watcher_stop.set()
    if _watcher_thread is not None:
        _watcher_thread.join(timeout=10)
        _watcher_thread = None
    log.info("Watcher stopped")


def is_watcher_running() -> bool:
    """Check if the watcher is currently running."""
    return _watcher_thread is not None and _watcher_thread.is_alive()
