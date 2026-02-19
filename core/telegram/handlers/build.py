"""
core/telegram/handlers/build.py — Build and edit commands
=========================================================
"""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

from core.telegram.builder import run_builder
from core.telegram.keyboards import kb_mode, kb_project_list, kb_yesno
from core.telegram.settings import OUTPUT_BASE, is_allowed
from core.telegram import state as tg_state
from core.utils import warn


async def cmd_build(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_allowed(chat_id):
        return
    if tg_state.active_build is not None:
        await update.message.reply_text("Build laeuft. Status: /builder")
        return
    tg_state.user_sessions[chat_id] = {"flow": "builder"}
    await update.message.reply_text("*Modus waehlen:*", parse_mode="Markdown", reply_markup=kb_mode())


async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_allowed(chat_id):
        return
    if tg_state.active_build is not None:
        await update.message.reply_text("Build laeuft. Status: /builder")
        return

    tg_state.user_sessions[chat_id] = {
        "flow": "builder",
        "mode": "3",
        "opmode": "2",
        "internet": "n",
        "awaiting": "edit_project_select",
    }
    await update.message.reply_text(
        "*Projekt zum Bearbeiten waehlen:*",
        parse_mode="Markdown",
        reply_markup=kb_project_list(),
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_allowed(chat_id):
        return

    if tg_state.active_build_proc is not None and tg_state.active_build is not None:
        try:
            # Write file-based cancellation signal
            signal_path = getattr(tg_state, 'build_signal_path', None)
            if signal_path:
                try:
                    with open(signal_path, "w") as f:
                        f.write("cancelled\n")
                except OSError:
                    pass
            tg_state.active_build_proc.terminate()
            try:
                tg_state.active_build_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                tg_state.active_build_proc.kill()
        except Exception as exc:
            warn(f"Error cancelling build: {exc}")
        tg_state.active_build = None
        tg_state.active_build_proc = None
        tg_state.build_state["phase"] = "Abgebrochen"
        tg_state.dispatcher_alive.set()
        await update.message.reply_text("*Build abgebrochen.*\nDispatcher wieder aktiv.", parse_mode="Markdown")
    else:
        if chat_id in tg_state.user_sessions:
            tg_state.user_sessions.pop(chat_id, None)
            await update.message.reply_text("Wizard abgebrochen.")
        else:
            await update.message.reply_text("Kein aktiver Build oder Wizard.")


async def cmd_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_allowed(chat_id):
        return

    args_text = " ".join(context.args).lower().strip() if context.args else ""

    import tempfile as _tf
    import glob as _glob
    temp_dir = _tf.gettempdir()
    temp_files = _glob.glob(os.path.join(temp_dir, "comfyui_*"))

    if not temp_files:
        await update.message.reply_text("No ComfyUI temp files found.")
        return

    if args_text == "delete":
        deleted = 0
        for f in temp_files:
            try:
                os.remove(f)
                deleted += 1
            except OSError:
                pass
        await update.message.reply_text(f"Deleted {deleted}/{len(temp_files)} temp files.")
    elif args_text == "keep":
        archive_dir = os.path.join(OUTPUT_BASE, "_comfyui_archive")
        os.makedirs(archive_dir, exist_ok=True)
        import shutil as _shutil
        moved = 0
        for f in temp_files:
            try:
                _shutil.move(f, os.path.join(archive_dir, os.path.basename(f)))
                moved += 1
            except OSError:
                pass
        await update.message.reply_text(
            f"Archived {moved} file(s) to `{archive_dir}`",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "*File Cleanup*\n"
            f"{len(temp_files)} ComfyUI temp file(s) found.\n\n"
            "`/cleanup delete` - Remove all\n"
            "`/cleanup keep` - Archive to output/",
            parse_mode="Markdown",
        )
