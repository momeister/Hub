"""
core/utils.py — Gemeinsame Hilfsfunktionen für alle Skills
=============================================================
Enthält: Logging, Telegram, JSON-Repair, Datei-I/O
Extrahiert aus builder.py v9
"""

import json
import logging
import os
import re
import sys
import urllib.parse
import urllib.request
from typing import Optional

# ---------------------------------------------------------------------------
# ENCODING FIX (Windows)
# ---------------------------------------------------------------------------
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("ai-hub")

try:
    from rich.console import Console
    from rich.table import Table
    console = Console()
    RICH = True
except ImportError:
    RICH = False
    class _FakeConsole:
        def print(self, *a, **kw): print(*a)
        def rule(self, *a, **kw): print("─" * 60)
    console = _FakeConsole()


def phase(msg: str) -> None:
    if RICH:
        console.rule(f"[bold cyan]{msg}[/bold cyan]")
    else:
        print(f"\n{'─'*20} {msg} {'─'*20}")


def info(msg: str) -> None:
    if RICH:
        console.print(f"  [green]✓[/green] {msg}")
    else:
        print(f"  ✓ {msg}")


def warn(msg: str) -> None:
    if RICH:
        console.print(f"  [yellow]⚠[/yellow] {msg}")
    else:
        print(f"  ⚠ {msg}")


def err(msg: str) -> None:
    if RICH:
        console.print(f"  [red]✗[/red] {msg}")
    else:
        print(f"  ✗ {msg}")


# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def send_telegram(message: str, reply_markup: dict = None) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message[:4096],
        "parse_mode": "Markdown",
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    payload = urllib.parse.urlencode(data).encode()
    try:
        req = urllib.request.Request(url, data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                log.debug("Telegram ✓")
    except Exception as e:
        log.warning(f"Telegram: {e}")


def send_telegram_photo(
    photo_path: str,
    caption: str = "",
    chat_id: str = "",
) -> bool:
    """
    Send a photo to Telegram using the sendPhoto API endpoint.
    Uses multipart/form-data upload.

    Args:
        photo_path: Local file path to the image
        caption: Optional caption text (max 1024 chars)
        chat_id: Override chat_id (default: TELEGRAM_CHAT_ID)

    Returns:
        True if sent successfully, False otherwise
    """
    if not TELEGRAM_TOKEN:
        return False
    target_chat = chat_id or TELEGRAM_CHAT_ID
    if not target_chat:
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    boundary = "----AiHubPhotoBoundary"

    with open(photo_path, "rb") as f:
        file_data = f.read()

    filename = os.path.basename(photo_path)

    # Build multipart body
    parts = []

    # chat_id field
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
        f"{target_chat}\r\n"
    )

    # caption field
    if caption:
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="caption"\r\n\r\n'
            f"{caption[:1024]}\r\n"
        )
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="parse_mode"\r\n\r\n'
            f"Markdown\r\n"
        )

    # photo file
    photo_header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'
        f"Content-Type: image/png\r\n\r\n"
    )

    body = "".join(parts).encode() + photo_header.encode() + file_data + f"\r\n--{boundary}--\r\n".encode()

    try:
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status == 200:
                log.debug("Telegram photo sent")
                return True
    except Exception as e:
        log.warning(f"Telegram photo send failed: {e}")

    return False


def send_telegram_video(
    video_path: str,
    caption: str = "",
    chat_id: str = "",
) -> bool:
    """
    Send a video to Telegram using the sendVideo API endpoint.
    Uses multipart/form-data upload.
    """
    if not TELEGRAM_TOKEN:
        return False
    target_chat = chat_id or TELEGRAM_CHAT_ID
    if not target_chat:
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendVideo"
    boundary = "----AiHubVideoBoundary"

    with open(video_path, "rb") as f:
        file_data = f.read()

    filename = os.path.basename(video_path)

    parts = []
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
        f"{target_chat}\r\n"
    )
    if caption:
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="caption"\r\n\r\n'
            f"{caption[:1024]}\r\n"
        )
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="parse_mode"\r\n\r\n'
            f"Markdown\r\n"
        )

    video_header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="video"; filename="{filename}"\r\n'
        f"Content-Type: video/mp4\r\n\r\n"
    )

    body = "".join(parts).encode() + video_header.encode() + file_data + f"\r\n--{boundary}--\r\n".encode()

    try:
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status == 200:
                log.debug("Telegram video sent")
                return True
    except Exception as e:
        log.warning(f"Telegram video send failed: {e}")

    return False


# ---------------------------------------------------------------------------
# JSON REPAIR
# ---------------------------------------------------------------------------
def _sanitize_llm_json(text: str) -> str:
    """Bereinigt typische LLM-JSON-Fehler."""
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = re.sub(r",\s*([\]}])", r"\1", text)
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return text


def _repair_json(text: str, end_char: str) -> str:
    sanitized = _sanitize_llm_json(text)
    try:
        json.loads(sanitized)
        return sanitized
    except json.JSONDecodeError:
        pass
    if end_char != "]":
        return sanitized
    last_obj = sanitized.rfind("}")
    if last_obj != -1:
        candidate = sanitized[:last_obj + 1].rstrip().rstrip(",") + "]"
        start_idx = candidate.find("[")
        if start_idx != -1:
            candidate = candidate[start_idx:]
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                pass
    lines = sanitized.split("\n")
    for i in range(len(lines) - 1, 0, -1):
        chunk = "\n".join(lines[:i])
        last_obj = chunk.rfind("}")
        if last_obj == -1:
            continue
        candidate = chunk[:last_obj + 1].rstrip().rstrip(",") + "]"
        s = candidate.find("[")
        if s != -1:
            candidate = candidate[s:]
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                continue
    return sanitized


def clean_json_output(text: str) -> str:
    """Extrahiert JSON aus LLM-Output (think-blocks, fences, prosa)."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = text.strip()
    m = re.search(r"```(?:json)?\s*([\[{].*?)\s*```", text, flags=re.DOTALL)
    if m:
        candidate = _sanitize_llm_json(m.group(1).strip())
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass
    for start_char, end_char in ("{", "}"), ("[", "]"):
        start = text.find(start_char)
        if start == -1:
            continue
        end = text.rfind(end_char)
        if end > start:
            candidate = text[start:end + 1]
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                candidate = _repair_json(candidate, end_char)
                try:
                    json.loads(candidate)
                    return candidate
                except json.JSONDecodeError:
                    pass
    return text


def strip_code_fences(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"^```[\w]*\n?", "", text.strip(), flags=re.MULTILINE)
    text = re.sub(r"\n?```$", "", text.strip(), flags=re.MULTILINE)
    return text.strip()


# ---------------------------------------------------------------------------
# DATEI I/O
# ---------------------------------------------------------------------------
def read_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def write_file(path: str, content: str) -> None:
    abs_path = os.path.abspath(path)
    if os.path.isdir(abs_path):
        raise IsADirectoryError(f"Pfad ist ein Verzeichnis: {path}")
    os.makedirs(os.path.dirname(abs_path) or ".", exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(content)


def is_safe_path(base_dir: str, target_path: str) -> bool:
    abs_base = os.path.abspath(base_dir)
    abs_target = os.path.abspath(os.path.join(base_dir, target_path))
    return abs_target.startswith(abs_base + os.sep) or abs_target == abs_base


def estimate_tokens(text: str, chars_per_token: float = 3.5) -> int:
    return int(len(text) / chars_per_token)
