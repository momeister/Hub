"""
core/telegram/handlers/dispatcher.py — Dispatcher + chat handlers
=================================================================
"""

from __future__ import annotations

import json
import os
import threading

from telegram import Update
from telegram.ext import ContextTypes

from core.dispatcher import dispatch
from core.telegram import state as tg_state
from core.telegram.settings import CHAT_MODEL, is_allowed
from core.telegram.keyboards import kb_mode, kb_scope
from core.utils import send_telegram

from core.telegram.handlers.image import handle_image_wizard_text
from core.telegram.handlers.voice import handle_voice_wizard_text
from core.telegram.keyboards import kb_yesno


def _run_skill_thread(skill, args, chat_id):
    try:
        if skill == "comfyui":
            from core.telegram.handlers.image import start_image_thread
            params = {
                "prompt": args.get("prompt", args.get("request", "")),
                "model": args.get("model", ""),
                "negative": args.get("negative", ""),
                "width": args.get("width", 1248),
                "height": args.get("height", 821),
                "steps": args.get("steps", 20),
                "amount": args.get("amount", 1),
                "reference_image_path": args.get("reference_image_path", ""),
            }
            start_image_thread(chat_id, params)
            return
        if skill == "downloader":
            from skills.downloader.skill import run as skill_run
        elif skill == "desktop":
            from skills.desktop.skill import run as skill_run
        elif skill == "knowledge":
            from skills.knowledge.skill import run as knowledge_run
            result = knowledge_run(**args)
            msg = result.get("message", "No result")
            if len(msg) > 3900:
                msg = msg[:3900] + "\n\n...(truncated)"
            send_telegram(msg)
            return
        elif skill == "websearch":
            from skills.websearch.skill import run as websearch_run
            result = websearch_run(**args)
            msg = result.get("message", "No result")
            if len(msg) > 3900:
                msg = msg[:3900] + "\n\n...(truncated)"
            send_telegram(msg)
            return
        elif skill == "audio":
            from skills.audio.skill import run as audio_run
            prefs = tg_state.voice_prefs.get(chat_id, {})
            if "profile_id" not in args and prefs.get("profile_id"):
                args["profile_id"] = prefs["profile_id"]
            if "language" not in args and prefs.get("language"):
                args["language"] = prefs["language"]

            result = audio_run(**args)
            msg = result.get("message", "No result")
            send_telegram(msg)
            if result.get("success") and result.get("path"):
                try:
                    from core.telegram.handlers.voice import _send_voice_file
                    _send_voice_file(chat_id, result["path"])
                    import os
                    os.remove(result["path"])
                except Exception:
                    pass
            return
        else:
            send_telegram(f"[ERROR] Unknown skill: `{skill}`")
            return
        send_telegram(f"[SUCCESS] *{skill}*:\n{str(skill_run(**args))[:500]}")
    except Exception as exc:
        send_telegram(f"[ERROR] *{skill}*: `{exc}`")


async def _handle_chat_message(update: Update, chat_id: int, text: str, session: dict = None):
    if session is None:
        session = tg_state.user_sessions.get(chat_id, {})
    history = session.get("history", [])
    history.append({"role": "user", "content": text})
    await update.message.reply_chat_action("typing")
    try:
        from core.llm_client import call_with_history, BASE_URL_V1
        response = call_with_history(
            model=session.get("model", CHAT_MODEL),
            messages=history[-20:],
            base_url=BASE_URL_V1,
            max_tokens=2048,
        )
    except Exception as exc:
        response = f"[ERROR] LLM: {exc}\n\nIst Ollama gestartet und `{CHAT_MODEL}` geladen?"
    history.append({"role": "assistant", "content": response})
    session["history"] = history[-20:]
    tg_state.user_sessions[chat_id] = session
    for i in range(0, len(response), 4000):
        await update.message.reply_text(response[i:i + 4000])


async def cmd_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_allowed(chat_id):
        return
    args_text = " ".join(context.args) if context.args else ""
    if args_text:
        session = {"flow": "chat", "history": [], "model": CHAT_MODEL}
        tg_state.user_sessions[chat_id] = session
        await _handle_chat_message(update, chat_id, args_text, session)
        return
    tg_state.user_sessions[chat_id] = {"flow": "chat", "history": [], "model": CHAT_MODEL}
    await update.message.reply_text(
        f"*Chat-Modus aktiv*\nModell: `{CHAT_MODEL}`\n\n"
        "Schreib deine Frage. Beenden: /stop",
        parse_mode="Markdown",
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_allowed(chat_id):
        return
    if tg_state.user_sessions.get(chat_id, {}).get("flow") == "chat":
        tg_state.user_sessions.pop(chat_id, None)
        await update.message.reply_text("Chat beendet.")
    else:
        await update.message.reply_text("Kein Chat aktiv. /chat zum Starten.")


async def msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_allowed(chat_id):
        return
    text = update.message.text.strip()
    session = tg_state.user_sessions.get(chat_id, {})

    if session.get("flow") == "chat":
        if text.lower() in ("exit", "quit", "stop", "/stop", "beenden"):
            tg_state.user_sessions.pop(chat_id, None)
            await update.message.reply_text("Chat beendet.")
            return
        await _handle_chat_message(update, chat_id, text, session)
        return

    if session.get("flow") == "builder" and session.get("awaiting") == "goal":
        session["goal"] = text
        tg_state.user_sessions[chat_id] = session
        await update.message.reply_text("*Scope:*", parse_mode="Markdown", reply_markup=kb_scope())
        session["awaiting"] = "scope"
        return

    if session.get("flow") == "builder" and session.get("awaiting") == "edit_goal":
        session["goal"] = text
        session["awaiting"] = "edit_confirm"
        tg_state.user_sessions[chat_id] = session
        mn = {"1": "FAST", "2": "AVERAGE", "3": "GOD MODE", "4": "UNCENSORED", "5": "Custom"}
        await update.message.reply_text(
            "*Edit-Modus*\n\n"
            f"Projekt  : `{session.get('project_name', '?')}`\n"
            f"Modus    : {mn.get(session.get('mode', '3'), '?')}\n"
            f"Aenderung: _{text[:200]}_\n\n"
            "Starten?",
            parse_mode="Markdown",
            reply_markup=kb_yesno("confirm_yes", "confirm_no"),
        )
        return

    if await handle_image_wizard_text(update, chat_id, text, session):
        return

    if await handle_voice_wizard_text(update, chat_id, text, session):
        return

    # Optimizer wizard: Task-Beschreibung eingeben
    if session.get("flow") == "optimizer" and session.get("awaiting") == "opt_task":
        session["opt_task"] = text
        session["awaiting"] = "opt_iterations"
        tg_state.user_sessions[chat_id] = session
        from core.telegram.keyboards import kb_optimizer_iterations
        await update.message.reply_text(
            "*Wie viele Iterationen?*",
            parse_mode="Markdown",
            reply_markup=kb_optimizer_iterations(),
        )
        return

    if session.get("flow") == "builder" and session.get("awaiting") == "custom_manager":
        session["custom_manager"] = text
        session["awaiting"] = "custom_coder"
        await update.message.reply_text("Coder-Modell:")
        return

    if session.get("flow") == "builder" and session.get("awaiting") == "custom_coder":
        session["custom_coder"] = text
        session["awaiting"] = "custom_ctx"
        await update.message.reply_text("Kontext-Tokens (leer=16384):")
        return

    if session.get("flow") == "builder" and session.get("awaiting") == "custom_ctx":
        session["custom_ctx"] = text if text.isdigit() else "16384"
        session["awaiting"] = "goal"
        await update.message.reply_text("Was soll gebaut werden?")
        return

    if not tg_state.dispatcher_alive.is_set():
        await update.message.reply_text(
            "Builder laeuft...\n"
            "/builder fuer Fortschritt\n"
            "/chat fuer Gespraeche\n"
            "/status fuer System"
        )
        return

    await update.message.reply_chat_action("typing")
    history = session.get("history", [])
    history.append({"role": "user", "content": text})
    result = dispatch(text, history)
    skill = result["skill"]

    if skill == "builder":
        if tg_state.active_build:
            await update.message.reply_text("Build laeuft. /builder fuer Status")
            return

        args = result.get("args", {})
        if args.get("auto_detected"):
            mn = {"1": "FAST", "2": "AVERAGE", "3": "GOD MODE", "4": "UNCENSORED", "5": "Custom"}
            sl = {"1": "Auto", "2": "Kompakt", "3": "Voll"}
            tl = {"1": "Nein", "2": "Ja"}
            lang_info = f"\nSprache  : `{args.get('language_hint', 'auto')}`" if args.get("language_hint") else ""
            internet_txt = "Ja" if args.get("internet", "n") == "y" else "Nein"

            session_data = {
                "flow": "builder",
                "goal": args.get("request", text),
                "mode": args.get("mode", "3"),
                "scope": args.get("scope", "1"),
                "tests": args.get("tests", "1"),
                "internet": args.get("internet", "n"),
                "opmode": args.get("opmode", "1"),
                "language_hint": args.get("language_hint", ""),
                "history": history,
            }
            tg_state.user_sessions[chat_id] = session_data

            await update.message.reply_text(
                "*Builder (Auto-Detected)*\n\n"
                f"Auftrag  : _{text[:200]}_\n"
                f"Modus    : {mn.get(session_data['mode'], '?')}\n"
                f"Scope    : {sl.get(session_data['scope'], '?')}\n"
                f"Tests    : {tl.get(session_data['tests'], '?')}\n"
                f"Internet : {internet_txt}"
                f"{lang_info}\n\n"
                "Starten?",
                parse_mode="Markdown",
                reply_markup=kb_yesno("confirm_yes", "confirm_no"),
            )
        else:
            tg_state.user_sessions[chat_id] = {"flow": "builder", "goal": text, "awaiting": "mode", "history": history}
            await update.message.reply_text(
                f"*Builder*\n_{text[:200]}_\n\nModus:",
                parse_mode="Markdown",
                reply_markup=kb_mode(),
            )
    elif skill == "chat":
        chat_session = {"flow": "chat", "history": history, "model": CHAT_MODEL}
        tg_state.user_sessions[chat_id] = chat_session
        await _handle_chat_message(update, chat_id, text, chat_session)
    elif skill == "downloader":
        args = result.get("args", {})
        dl_type = args.get("type", "auto")
        dest = args.get("destination", "") or os.environ.get("DOWNLOAD_DIR", "~/Downloads")
        tg_state.user_sessions[chat_id] = {
            "flow": "download_confirm",
            "dl_args": args,
            "history": history,
        }
        await update.message.reply_text(
            "*Download bestaetigen*\n\n"
            f"Anfrage : _{text[:200]}_\n"
            f"Typ     : `{dl_type}`\n"
            f"Ziel    : `{dest}`\n\n"
            "Herunterladen?",
            parse_mode="Markdown",
            reply_markup=kb_yesno("dl_confirm_yes", "dl_confirm_no"),
        )
    else:
        args = result.get("args", {})
        await update.message.reply_text(
            f"Starte: *{skill}*\n`{json.dumps(args, ensure_ascii=False)[:200]}`",
            parse_mode="Markdown",
        )
        threading.Thread(target=_run_skill_thread, args=(skill, args, chat_id), daemon=True).start()
