"""
skills/downloader/skill.py — Download Manager
===============================================
Unterstützt: Steam (steamcmd), YouTube (yt-dlp), direkte URLs (aria2c/wget).
Fortschritts-Updates per Telegram.
"""

import logging
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from core.utils import info, warn, err, send_telegram

log = logging.getLogger("skill.downloader")

DEFAULT_DEST = os.environ.get("DOWNLOAD_DIR", str(Path.home() / "Downloads"))
STEAM_USER   = os.environ.get("STEAM_USER", "anonymous")
STEAM_PASS   = os.environ.get("STEAM_PASS", "")


def _detect_type(request: str) -> str:
    r = request.lower()
    if "youtube.com" in r or "youtu.be" in r:
        return "youtube"
    if "store.steampowered.com" in r or r.startswith("steam:") or "app id" in r:
        return "steam"
    if r.startswith("http://") or r.startswith("https://"):
        return "url"
    # Heuristik: Game-Namen
    if any(kw in r for kw in ["steam", "game", "download game", "update game"]):
        return "steam"
    return "url"


def _download_url(url: str, dest: str) -> str:
    os.makedirs(dest, exist_ok=True)
    # Versuche aria2c, dann wget, dann curl
    for tool, cmd in [
        ("aria2c", ["aria2c", "-x", "8", "-s", "8", "-d", dest, url]),
        ("wget",   ["wget", "-P", dest, url]),
        ("curl",   ["curl", "-L", "-o", os.path.join(dest, url.split("/")[-1][:64]), url]),
    ]:
        if shutil.which(tool):
            info(f"Verwende {tool}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            if result.returncode == 0:
                return f"✅ Download abgeschlossen → {dest}"
            else:
                err(f"{tool} Fehler: {result.stderr[:200]}")
    return "❌ Kein Download-Tool verfügbar (aria2c/wget/curl)"


def _download_youtube(url: str, dest: str) -> str:
    os.makedirs(dest, exist_ok=True)
    if not shutil.which("yt-dlp"):
        return "❌ yt-dlp nicht installiert. pip install yt-dlp"
    cmd = ["yt-dlp", "-f", "bestvideo+bestaudio/best", "-o",
           os.path.join(dest, "%(title)s.%(ext)s"), url]
    info(f"yt-dlp: {url}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if result.returncode == 0:
        return f"✅ Video heruntergeladen → {dest}"
    return f"❌ yt-dlp Fehler:\n{result.stderr[:300]}"


def _download_steam(request: str, dest: str) -> str:
    if not shutil.which("steamcmd"):
        return (
            "❌ SteamCMD nicht installiert.\n"
            "Windows: https://developer.valvesoftware.com/wiki/SteamCMD\n"
            "Dann STEAM_USER und STEAM_PASS in .env setzen."
        )
    # App-ID extrahieren
    app_id_match = re.search(r"\b(\d{5,8})\b", request)
    if not app_id_match:
        return (
            "❌ Steam App-ID nicht gefunden.\n"
            "Beispiel: 'Downloade Cyberpunk 2077 (AppID: 1091500)'\n"
            "App-IDs findest du auf store.steampowered.com"
        )
    app_id = app_id_match.group(1)
    os.makedirs(dest, exist_ok=True)

    login = f"+login {STEAM_USER}"
    if STEAM_PASS:
        login += f" {STEAM_PASS}"

    cmd = [
        "steamcmd",
        login,
        f"+force_install_dir {dest}",
        f"+app_update {app_id} validate",
        "+quit",
    ]
    info(f"SteamCMD: App {app_id} → {dest}")
    send_telegram(f"🎮 Steam Download gestartet\nApp-ID: {app_id}\nZiel: {dest}")

    result = subprocess.run(
        " ".join(cmd), shell=True,
        capture_output=True, text=True, timeout=7200,
        encoding="utf-8", errors="replace",
    )
    if result.returncode == 0 or "Success" in result.stdout:
        return f"✅ Steam-Download abgeschlossen!\nApp {app_id} → {dest}"
    return f"⚠️ SteamCMD Output:\n{result.stdout[-500:]}"


def run(
    request: str,
    type: str = "auto",
    destination: str = "",
) -> str:
    """
    Lädt etwas herunter.
    
    Args:
        request:     URL, Game-Name, oder Beschreibung
        type:        url | steam | youtube | auto
        destination: Zielordner (default: ~/Downloads)
    """
    dest = destination or DEFAULT_DEST
    dl_type = type if type != "auto" else _detect_type(request)

    info(f"Downloader: type={dl_type}  dest={dest}")
    send_telegram(f"⬇️ Download gestartet\nTyp: {dl_type}\nAnfrage: {request[:100]}")

    if dl_type == "youtube":
        return _download_youtube(request, dest)
    elif dl_type == "steam":
        return _download_steam(request, dest)
    else:
        return _download_url(request, dest)
