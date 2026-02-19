"""
core/telegram_gateway.py — Telegram Bot Gateway v3
====================================================
Modular entrypoint that wires command handlers and shared state.
"""

import logging
import sys

log = logging.getLogger("ai-hub.telegram")

try:
    from telegram.ext import (
        Application,
        CommandHandler,
        MessageHandler,
        CallbackQueryHandler,
        filters,
    )
    TELEGRAM_OK = True
except ImportError:
    TELEGRAM_OK = False
    print("[FATAL] python-telegram-bot nicht installiert")
    sys.exit(1)

from core.skill_registry import get_available_skills
from core.telegram.handlers.callbacks import cb_handler
from core.telegram.handlers.dispatcher import cmd_chat, cmd_stop, msg_handler
from core.telegram.handlers.image import cmd_image, photo_handler
from core.telegram.handlers.voice import cmd_voice, voice_handler
from core.telegram.handlers.knowledge import cmd_knowledge
from core.telegram.handlers.run import cmd_run
from core.telegram.handlers.system import (
    cmd_start,
    cmd_help,
    cmd_status,
    cmd_services,
    cmd_builder_status,
    cmd_skills,
    error_handler,
    set_bot_commands,
)
from core.telegram.handlers.build import cmd_build, cmd_edit, cmd_cancel, cmd_cleanup
from core.telegram.handlers.optimize import cmd_optimize
from core.telegram.settings import (
    TELEGRAM_TOKEN,
    DISPATCHER_MODEL,
    CHAT_MODEL,
    BUILDER_IMAGE,
    OUTPUT_BASE,
)
from core.utils import info, phase


def main():
    if not TELEGRAM_TOKEN:
        print("[FATAL] TELEGRAM_TOKEN not set")
        sys.exit(1)
    phase("AI Hub -- Telegram Gateway v3")
    info(f"Dispatcher model : {DISPATCHER_MODEL}")
    info(f"Chat model       : {CHAT_MODEL}")
    info(f"Builder image    : {BUILDER_IMAGE}")
    info(f"Output dir       : {OUTPUT_BASE}")
    info(f"Skills           : {', '.join(get_available_skills())}")

    try:
        from skills.knowledge.watcher import start_watcher, is_watcher_running
        if start_watcher():
            info("Knowledge file watcher started")
        elif is_watcher_running():
            info("Knowledge file watcher already running")
    except Exception as exc:
        log.warning(f"File watcher not started: {exc}")

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(set_bot_commands)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("builder", cmd_builder_status))
    app.add_handler(CommandHandler("build", cmd_build))
    app.add_handler(CommandHandler("edit", cmd_edit))
    app.add_handler(CommandHandler("chat", cmd_chat))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("image", cmd_image))
    app.add_handler(CommandHandler("voice", cmd_voice))
    app.add_handler(CommandHandler("skills", cmd_skills))
    app.add_handler(CommandHandler("services", cmd_services))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("cleanup", cmd_cleanup))
    app.add_handler(CommandHandler("knowledge", cmd_knowledge))
    app.add_handler(CommandHandler("optimize", cmd_optimize))
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, voice_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))
    app.add_error_handler(error_handler)
    info("Bot started...")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"],
        connect_timeout=30,
        read_timeout=30,
        write_timeout=30,
        pool_timeout=10,
    )


if __name__ == "__main__":
    main()
