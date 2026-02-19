"""
core/telegram/handlers/system.py — System and help commands
===========================================================
"""

from __future__ import annotations

import time
import subprocess
from typing import TYPE_CHECKING

from telegram import BotCommand, Update
from telegram.ext import ContextTypes
from telegram.error import NetworkError, TimedOut

from core.scheduler import gpu_status
from core.services_status import get_services_status, get_ollama_models
from core.skill_registry import get_available_skills, get_skill_descriptions
from core.telegram.keyboards import kb_services
from core.telegram.settings import (
    DISPATCHER_MODEL,
    CHAT_MODEL,
    OUTPUT_BASE,
    SERVICES_CFG,
    VOICEBOX_URL,
    is_allowed,
)
from core.telegram import state as tg_state
from core.utils import info, warn


def _esc(text: str) -> str:
    """Escape Telegram Markdown v1 special characters in dynamic text."""
    if not text:
        return text
    for ch in ('_', '*', '`', '['):
        text = text.replace(ch, '\\' + ch)
    return text


if TYPE_CHECKING:
    from telegram.ext import Application


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_chat.id):
        return
    await update.message.reply_text(
        "*AI Hub v3*\n\n"
        "Schreib was du brauchst -- ich erkenne es automatisch.\n\n"
        "*Commands:*\n"
        "/build   -- Projekt bauen\n"
        "/edit    -- Bestehendes Projekt bearbeiten\n"
        "/image   -- Bild generieren\n"
        "/voice   -- Voice-Profile & TTS (Voicebox)\n"
        "/chat    -- Mit KI chatten\n"
        "/skills  -- Alle Skills anzeigen\n"
        "/knowledge -- Wissen suchen/indexieren\n"
        "/optimize -- Code selbst verbessern (Self-Optimizer)\n"
        "/run     -- Generiertes Projekt starten\n"
        "/status  -- CPU/RAM/GPU/Modelle\n"
        "/services -- Services starten/stoppen\n"
        "/builder -- Builder-Status\n"
        "/help    -- Alle Infos\n\n"
        "*Beispiele:*\n"
        "  _Bau mir einen Port-Scanner in Rust_\n"
        "  _/image a red dragon, 1024x768, flux2_\n"
        "  _/voice Hello, this is a test_\n"
        "  _Was ist der Unterschied zwischen async und threads?_",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_chat.id):
        return
    skills = get_available_skills()
    await update.message.reply_text(
        "*Hilfe*\n\n"
        "*Commands:*\n"
        "/build    -- Builder starten\n"
        "/edit     -- Bestehendes Projekt bearbeiten\n"
        "/cancel   -- Aktiven Build oder Wizard abbrechen\n"
        "/image    -- Bild generieren (ComfyUI)\n"
        "/voice    -- Voice-Profile & TTS (Voicebox)\n"
        "/skills   -- Alle Skills anzeigen\n"
        "/knowledge -- Wissen suchen/indexieren\n"
        "/optimize  -- Self-Optimizer (Codebase verbessern)\n"
        "/chat     -- Chat mit KI (/chat Frage direkt)\n"
        "/run      -- Letztes Projekt ausfuehren\n"
        "/status   -- System: CPU, RAM, GPU, Ollama\n"
        "/services -- Services starten/stoppen\n"
        "/builder  -- Builder-Fortschritt\n"
        "/stop     -- Chat-Modus beenden\n\n"
        "*Voice (Voicebox TTS/STT):*\n"
        "/voice               -- Einstellungen & Profile\n"
        "/voice profiles      -- Profile auflisten\n"
        "/voice status        -- Voicebox Status\n"
        "/voice <text>        -- Text direkt vorlesen\n\n"
        "*Image Generation:*\n"
        "/image               -- Wizard (Schritt fuer Schritt)\n"
        "/image cute cat, flux2  -- Direkt generieren\n"
        "/image dragon, 768x1024, wan2  -- Mit Groesse\n\n"
        "*Knowledge Base:*\n"
        "/knowledge <query>     -- Wissen suchen\n"
        "/knowledge status      -- DB Statistiken\n"
        "/knowledge ingest <path> -- Dateien indexieren\n\n"
        "*Self-Optimizer:*\n"
        "/optimize              -- Wizard starten\n"
        "/optimize stop         -- Laufenden Optimizer stoppen\n"
        "/optimize status       -- Fortschritt anzeigen\n\n"
        "*Builder Modi:*\n"
        "FAST    -- Schnell, kleine Projekte\n"
        "AVERAGE -- Ausgewogen\n"
        "GOD     -- Beste Qualitaet, langsamer\n"
        "UNCEN   -- Ohne Filter\n"
        "Custom  -- Eigenes Modell\n\n"
        f"*Skills:* {', '.join(f'`{s}`' for s in skills)}\n\n"
        "*LLM-Timeout:*\n"
        "GOD MODE kann 30+ Min pro Datei brauchen.",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_chat.id):
        return
    si = get_system_info()
    gpu = gpu_status()
    models = get_ollama_models(SERVICES_CFG)
    disp = "Aktiv" if tg_state.dispatcher_alive.is_set() else "Pausiert (Build)"
    gpu_line = (
        f"GPU  : {si['gpu_name']}\n   VRAM : {si['gpu_vram_used']}/{si['gpu_vram_total']} GB  ({si['gpu_util']})"
        if si["gpu_name"] != "?" else
        f"GPU  : {'Belegt: ' + str(gpu['owner']) if not gpu['free'] else 'Frei'}"
    )
    model_txt = "\n".join(f"  `{m}`" for m in models[:10]) if models else "  (Ollama nicht erreichbar)"
    build_txt = ""
    if tg_state.active_build:
        el = int(time.time() - (tg_state.build_state["started_at"] or time.time()))
        m2, s2 = divmod(el, 60)
        done = min(tg_state.build_state['files_done'], tg_state.build_state['files_total'])
        build_txt = (
            f"\n\n*Build laeuft* ({m2}m {s2}s)\n"
            f"  Phase  : {_esc(tg_state.build_state.get('detailed_phase', tg_state.build_state['phase'])[:60])}\n"
            f"  Files  : {done}/{tg_state.build_state['files_total']}\n"
            f"  File   : `{tg_state.build_state['current_file'] or '--'}`\n"
            f"  Action : {_esc(tg_state.build_state.get('current_action', '--'))}"
        )

    services = get_services_status(SERVICES_CFG)
    svc_lines = []
    for svc in services:
        status = "Online" if svc["online"] else "Offline"
        svc_lines.append(f"  {svc['name']:10s}: {status} ({svc['port']})")
    svc_txt = "\n".join(svc_lines)

    await update.message.reply_text(
        f"*System Status*\n\n"
        f"CPU  : {si['cpu_pct']}\n"
        f"RAM  : {si['ram_used']}/{si['ram_total']} GB\n"
        f"{gpu_line}\n\n"
        f"Dispatcher : {disp}\n"
        f"Model      : `{DISPATCHER_MODEL}`"
        f"{build_txt}\n\n"
        f"*Services:*\n{svc_txt}\n\n"
        f"*Ollama Models:*\n{model_txt}",
        parse_mode="Markdown",
    )


async def cmd_services(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_chat.id):
        return
    services = get_services_status(SERVICES_CFG)
    lines = ["*Services*\n"]
    for svc in services:
        status = "Online" if svc["online"] else "Offline"
        lines.append(f"  {svc['name']:10s}: {status} ({svc['port']})")
    lines.append("\nUse the buttons below to start/stop services.")
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=kb_services(),
    )


async def cmd_builder_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_chat.id):
        return
    if not tg_state.active_build:
        await update.message.reply_text("Kein Build aktiv.\nStarte mit /build")
        return

    el = int(time.time() - (tg_state.build_state["started_at"] or time.time()))
    m2, s2 = divmod(el, 60)

    capped_done = min(tg_state.build_state["files_done"], tg_state.build_state["files_total"])
    if tg_state.build_state["files_total"] > 0:
        progress_pct = int((capped_done / tg_state.build_state["files_total"]) * 100)
        bar_len = 15
        filled = int((progress_pct / 100) * bar_len)
        progress_bar = "=" * filled + "-" * (bar_len - filled)
        progress_text = f"[{progress_bar}] {progress_pct}%"
    else:
        progress_text = f"{tg_state.build_state['files_done']} files"

    err_txt = ""
    if tg_state.build_state["errors"]:
        err_txt = (
            f"\n\n*Errors ({len(tg_state.build_state['errors'])}):*\n"
            + "\n".join(f"  `{e[:80]}`" for e in tg_state.build_state["errors"][-5:])
        )

    done = min(tg_state.build_state['files_done'], tg_state.build_state['files_total'])
    await update.message.reply_text(
        f"*Builder Status* -- {m2}m {s2}s\n\n"
        f"*Progress*\n"
        f"   {progress_text}\n"
        f"   Files: {done}/{tg_state.build_state['files_total']}\n\n"
        f"*Current*\n"
        f"   Phase  : {_esc(tg_state.build_state.get('detailed_phase', tg_state.build_state['phase']))}\n"
        f"   Action : {_esc(tg_state.build_state.get('current_action', '--'))}\n"
        f"   File   : `{tg_state.build_state['current_file'] or '--'}`\n\n"
        f"*Models*\n"
        f"   Active  : `{tg_state.build_state.get('active_model', '--')}`\n"
        f"   Manager : `{tg_state.build_state.get('manager_model', '?')}`\n"
        f"   Coder   : `{tg_state.build_state.get('coder_model', '?')}`\n\n"
        f"*Project*\n"
        f"   Language: {_esc(tg_state.build_state['language'] or '--')}"
        + err_txt,
        parse_mode="Markdown",
    )


async def cmd_skills(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_allowed(chat_id):
        return

    skill_infos = get_skill_descriptions()
    if not skill_infos:
        await update.message.reply_text("No skills found.")
        return

    lines = ["*Available Skills*\n"]
    for s in skill_infos:
        name = s["name"]
        desc = s["description"]
        first_sentence = desc.split(". ")[0] + "."
        if len(first_sentence) > 120:
            first_sentence = first_sentence[:117] + "..."
        lines.append(f"  `{name}` -- {first_sentence}")

    lines.append("\n_Use /help for detailed command info._")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    exc = context.error
    if isinstance(exc, (NetworkError, TimedOut)):
        warn(f"Telegram network error (ignored, bot continues): {type(exc).__name__}")
        return
    err_str = str(exc).lower()
    if any(k in err_str for k in ("getaddrinfo", "connect", "connection", "network", "httpx")):
        warn(f"Network error ignored: {exc}")
        return
    warn(f"Unhandled Telegram error: {exc}")


async def set_bot_commands(app: "Application") -> None:
    commands = [
        BotCommand("start",   "Willkommen & Uebersicht"),
        BotCommand("help",    "Hilfe & alle Funktionen"),
        BotCommand("build",   "Neues Projekt bauen lassen"),
        BotCommand("edit",    "Bestehendes Projekt bearbeiten"),
        BotCommand("cancel",  "Aktiven Build / Wizard abbrechen"),
        BotCommand("image",   "Bild generieren (ComfyUI)"),
        BotCommand("voice",   "Voice-Profile & TTS (Voicebox)"),
        BotCommand("skills",  "Alle verfuegbaren Skills anzeigen"),
        BotCommand("services", "Services starten/stoppen (Voicebox, ComfyUI)"),
        BotCommand("chat",    "Mit KI chatten (/chat Frage)"),
        BotCommand("run",     "Generiertes Projekt ausfuehren"),
        BotCommand("status",  "CPU / RAM / GPU / Modelle"),
        BotCommand("builder", "Builder-Fortschritt abfragen"),
        BotCommand("stop",    "Chat-Modus beenden"),
        BotCommand("cleanup", "ComfyUI temp files loeschen/archivieren"),
        BotCommand("knowledge", "Wissenssuche / Dokumente indexieren"),
        BotCommand("optimize", "Self-Optimizer (Codebase verbessern)"),
    ]
    await app.bot.set_my_commands(commands)
    info("Bot menu registered")


def get_system_info() -> dict:
    d = {
        "cpu_pct": "?",
        "ram_used": "?",
        "ram_total": "?",
        "gpu_name": "?",
        "gpu_vram_used": "?",
        "gpu_vram_total": "?",
        "gpu_util": "?",
    }
    try:
        import psutil
        d["cpu_pct"] = f"{psutil.cpu_percent(interval=0.5):.0f}%"
        vm = psutil.virtual_memory()
        d["ram_used"] = f"{vm.used / 1024 ** 3:.1f}"
        d["ram_total"] = f"{vm.total / 1024 ** 3:.0f}"
    except ImportError:
        pass
    try:
        r = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0:
            parts = [p.strip() for p in r.stdout.strip().split(",")]
            if len(parts) >= 4:
                d["gpu_name"] = parts[0]
                d["gpu_vram_used"] = f"{int(parts[1]) // 1024:.1f}"
                d["gpu_vram_total"] = f"{int(parts[2]) // 1024:.1f}"
                d["gpu_util"] = f"{parts[3]}%"
    except Exception:
        pass
    return d
