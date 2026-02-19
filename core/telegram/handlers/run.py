"""
core/telegram/handlers/run.py — Project runner command
=======================================================
"""

from __future__ import annotations

import threading
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

from core.telegram.settings import OUTPUT_BASE, is_allowed
from core.utils import send_telegram, send_telegram_photo


def _run_project_thread(chat_id: int, project_dir: str):
    try:
        from core.project_runner import run_project, detect_project_language, capture_screenshot

        language = detect_project_language(project_dir)
        result = run_project(project_dir, language=language, timeout=60)

        project_name = Path(project_dir).name

        if result.get("error"):
            send_telegram(
                f"[ERROR] *{project_name}*\n"
                f"Language: {result['language']}\n"
                f"Error: `{result['error'][:300]}`"
            )
            return

        exit_code = result.get("exit_code", -1)
        output = result.get("output", "")[:3000]
        method = result.get("method", "?")

        if exit_code == 0:
            send_telegram(
                f"[SUCCESS] *{project_name}*\n"
                f"Language : {result['language']}\n"
                f"Method   : {method}\n"
                f"Exit     : {exit_code}\n\n"
                f"*Output:*\n```\n{output}\n```"
            )
        else:
            send_telegram(
                f"[ERROR] *{project_name}* (exit {exit_code})\n"
                f"Language : {result['language']}\n"
                f"Method   : {method}\n\n"
                f"*Output:*\n```\n{output}\n```"
            )

        screenshot_path = capture_screenshot(project_dir, language=language)
        if screenshot_path:
            send_telegram_photo(
                screenshot_path,
                caption=f"Screenshot: {project_name}",
            )
    except Exception as exc:
        send_telegram(f"[ERROR] Project runner failed: `{str(exc)[:300]}`")


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_allowed(chat_id):
        return

    args_text = " ".join(context.args) if context.args else ""
    output_base = Path(OUTPUT_BASE).resolve()

    if not output_base.exists():
        await update.message.reply_text("[ERROR] Output directory does not exist.")
        return

    if args_text:
        project_dir = output_base / args_text
    else:
        dirs = [d for d in output_base.iterdir() if d.is_dir()]
        if not dirs:
            await update.message.reply_text(
                "[INFO] No projects found in output/\n"
                "Use /build to create a project first."
            )
            return
        project_dir = max(dirs, key=lambda d: d.stat().st_mtime)

    if not project_dir.is_dir():
        dirs = [d.name for d in output_base.iterdir() if d.is_dir()]
        project_list = "\n".join(f"  `{d}`" for d in dirs[:15])
        await update.message.reply_text(
            f"[ERROR] Project not found: `{args_text}`\n\n"
            f"*Available projects:*\n{project_list}\n\n"
            f"Usage: `/run project_name`",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(
        f"[INFO] Running `{project_dir.name}`...\n"
        "_This may take up to 60 seconds._",
        parse_mode="Markdown",
    )

    threading.Thread(
        target=_run_project_thread,
        args=(chat_id, str(project_dir)),
        daemon=True,
    ).start()
