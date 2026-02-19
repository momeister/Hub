"""
core/telegram/handlers/knowledge.py — Knowledge base commands
=============================================================
"""

from __future__ import annotations

import threading

from telegram import Update
from telegram.ext import ContextTypes

from core.telegram.settings import is_allowed
from core.utils import send_telegram


def _run_knowledge_thread(chat_id: int, action: str, query: str, path: str):
    try:
        from skills.knowledge.skill import run as knowledge_run
        result = knowledge_run(request=query, action=action, path=path)
        msg = result.get("message", "No result")
        if len(msg) > 3900:
            msg = msg[:3900] + "\n\n...(truncated)"
        send_telegram(msg)
    except Exception as exc:
        send_telegram(f"[ERROR] Knowledge: `{str(exc)[:300]}`")


def _run_knowledge_delete_thread(chat_id: int, path: str):
    try:
        from skills.knowledge.skill import delete_source
        result = delete_source(path)
        send_telegram(f"[{'SUCCESS' if result['success'] else 'ERROR'}] {result['message']}")
    except Exception as exc:
        send_telegram(f"[ERROR] Knowledge delete: `{str(exc)[:300]}`")


async def cmd_knowledge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_allowed(chat_id):
        return

    args_text = " ".join(context.args).strip() if context.args else ""

    if not args_text:
        await update.message.reply_text(
            "*Knowledge Base*\n\n"
            "`/knowledge <query>` - Search\n"
            "`/knowledge status` - DB stats\n"
            "`/knowledge ingest <path>` - Index file/folder\n"
            "`/knowledge delete <path>` - Remove from DB\n\n"
            "Or just ask a question — the dispatcher will auto-route.",
            parse_mode="Markdown",
        )
        return

    if args_text.lower() == "status":
        await update.message.reply_chat_action("typing")
        threading.Thread(
            target=_run_knowledge_thread,
            args=(chat_id, "status", "", ""),
            daemon=True,
        ).start()
        return

    if args_text.lower().startswith("ingest "):
        path = args_text[7:].strip().strip('"').strip("'")
        if not path:
            await update.message.reply_text("Usage: `/knowledge ingest <path>`", parse_mode="Markdown")
            return
        await update.message.reply_text(
            f"*Indexing...*\n`{path}`\n_This may take a moment._",
            parse_mode="Markdown",
        )
        threading.Thread(
            target=_run_knowledge_thread,
            args=(chat_id, "ingest", "", path),
            daemon=True,
        ).start()
        return

    if args_text.lower().startswith("delete "):
        path = args_text[7:].strip().strip('"').strip("'")
        if not path:
            await update.message.reply_text("Usage: `/knowledge delete <path>`", parse_mode="Markdown")
            return
        threading.Thread(
            target=_run_knowledge_delete_thread,
            args=(chat_id, path),
            daemon=True,
        ).start()
        return

    await update.message.reply_chat_action("typing")
    threading.Thread(
        target=_run_knowledge_thread,
        args=(chat_id, "search", args_text, ""),
        daemon=True,
    ).start()
