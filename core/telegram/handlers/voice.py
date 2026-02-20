"""
core/telegram/handlers/voice.py — Voicebox TTS/STT handlers
===========================================================
"""

from __future__ import annotations

import os
import threading

from telegram import Update
from telegram.ext import ContextTypes

from core.telegram.keyboards import (
    kb_voice_menu,
    kb_voice_language,
    kb_voice_profile_lang,
    kb_voice_profiles_select,
    kb_voice_delete_confirm,
)
from core.telegram.settings import VOICEBOX_URL, is_allowed
from core.telegram import state as tg_state
from core.utils import send_telegram


def _send_voice_file(chat_id: int, voice_path: str):
    import urllib.request as _ur
    from core.telegram.settings import TELEGRAM_TOKEN

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendVoice"
    boundary = "----AiHubVoiceBoundary"
    with open(voice_path, "rb") as f:
        file_data = f.read()
    filename = os.path.basename(voice_path)
    parts = [
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
        f"{chat_id}\r\n"
    ]
    voice_header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="voice"; filename="{filename}"\r\n'
        "Content-Type: audio/ogg\r\n\r\n"
    )
    body = "".join(parts).encode() + voice_header.encode() + file_data + f"\r\n--{boundary}--\r\n".encode()
    try:
        req = _ur.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
        with _ur.urlopen(req, timeout=30):
            pass
    except Exception as exc:
        send_telegram(f"[ERROR] Voice send failed: `{exc}`")


def _transcribe_voice_thread(chat_id: int, voice_path: str):
    try:
        from skills.audio.skill import transcribe
        result = transcribe(voice_path)
        if result["success"]:
            text = result["text"]
            lang = result.get("language", "?")
            dur = result.get("duration", "?")
            msg = f"*Transcription* ({lang}, {dur}s)\n\n{text[:3500]}"
            if len(text) > 3500:
                msg += f"\n\n...({len(text)} total chars)"
            send_telegram(msg)
        else:
            send_telegram(f"[ERROR] Transcription: {result.get('error', '?')}")
    except Exception as exc:
        send_telegram(f"[ERROR] Voice transcription: `{str(exc)[:300]}`")
    finally:
        try:
            os.remove(voice_path)
        except OSError:
            pass


async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_allowed(chat_id):
        return

    session = tg_state.user_sessions.get(chat_id, {})

    await update.message.reply_chat_action("typing")

    try:
        import tempfile as _tempfile
        voice = update.message.voice or update.message.audio
        if not voice:
            return
        file = await context.bot.get_file(voice.file_id)
        tmp = _tempfile.NamedTemporaryFile(delete=False, suffix=".ogg", prefix="voice_")
        await file.download_to_drive(tmp.name)
        tmp.close()
        saved_path = tmp.name
    except Exception as exc:
        await update.message.reply_text(f"Failed to download voice: {exc}")
        return

    if session.get("flow") == "voice" and session.get("awaiting") == "voice_sample":
        profile_id = session.get("profile_id", "")
        if profile_id:
            await update.message.reply_text("Uploading sample to Voicebox...")
            threading.Thread(
                target=_voice_upload_sample_thread,
                args=(chat_id, profile_id, saved_path),
                daemon=True,
            ).start()
            return

    threading.Thread(
        target=_transcribe_voice_thread,
        args=(chat_id, saved_path),
        daemon=True,
    ).start()


async def cmd_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_allowed(chat_id):
        return

    args_text = " ".join(context.args).strip() if context.args else ""

    if not args_text:
        prefs = tg_state.voice_prefs.get(chat_id, {})
        current_profile = prefs.get("profile_name", "None")
        current_lang = prefs.get("language", "en")
        await update.message.reply_text(
            "*Voice Settings (Voicebox)*\n\n"
            f"Profile  : `{current_profile}`\n"
            f"Language : `{current_lang}`\n"
            f"Server   : `{VOICEBOX_URL}`\n\n"
            "Choose an option:",
            parse_mode="Markdown",
            reply_markup=kb_voice_menu(),
        )
        return

    sub = args_text.lower()
    if sub == "profiles":
        await update.message.reply_chat_action("typing")
        threading.Thread(
            target=_voice_list_profiles_thread,
            args=(chat_id,),
            daemon=True,
        ).start()
    elif sub == "status":
        await update.message.reply_chat_action("typing")
        threading.Thread(
            target=_voice_status_thread,
            args=(chat_id,),
            daemon=True,
        ).start()
    else:
        prefs = tg_state.voice_prefs.get(chat_id, {})
        profile_id = prefs.get("profile_id", "")
        language = prefs.get("language", "en")
        await update.message.reply_text(
            "*Generating voice...*\n"
            f"Profile: `{prefs.get('profile_name', 'default')}`\n"
            f"Language: `{language}`",
            parse_mode="Markdown",
        )
        threading.Thread(
            target=_voice_tts_thread,
            args=(chat_id, args_text, profile_id, language),
            daemon=True,
        ).start()


def _voice_list_profiles_thread(chat_id: int):
    try:
        from skills.audio.skill import list_profiles
        result = list_profiles()
        if result["success"]:
            profiles = result["profiles"]
            if not profiles:
                send_telegram("*Voice Profiles*\nNo profiles found. Use /voice to create one.")
                return
            lines = [f"*Voice Profiles ({len(profiles)}):*\n"]
            for p in profiles:
                pid = p.get("id", "?")
                name = p.get("name", "?")
                lang = p.get("language", "?")
                lines.append(f"  `{pid}` -- {name} ({lang})")
            send_telegram("\n".join(lines))
        else:
            send_telegram(f"[ERROR] {result.get('error', '?')}")
    except Exception as exc:
        send_telegram(f"[ERROR] Voice profiles: `{str(exc)[:300]}`")


def _voice_status_thread(chat_id: int):
    try:
        from skills.audio.skill import get_voicebox_status
        status = get_voicebox_status()
        healthy = "Online" if status["healthy"] else "Offline"
        models_info = ""
        models = status.get("models")
        if models and isinstance(models, dict):
            models_info = "\n\n*Models:*\n" + "\n".join(
                f"  `{k}`: {v}" for k, v in models.items()
            )
        elif models and isinstance(models, list):
            models_info = "\n\n*Models:*\n" + "\n".join(
                f"  `{m.get('name', '?')}`: {'loaded' if m.get('loaded') else 'available'}"
                for m in models
            )
        send_telegram(f"*Voicebox*\nStatus: {healthy}\nURL: `{VOICEBOX_URL}`{models_info}")
    except Exception as exc:
        send_telegram(f"[ERROR] Voicebox status: `{str(exc)[:300]}`")


def _voice_create_profile_thread(chat_id: int, name: str, language: str):
    try:
        from skills.audio.skill import create_profile
        result = create_profile(name, language)
        if result["success"]:
            profile = result["profile"]
            pid = profile.get("id", "?")
            send_telegram(
                "[SUCCESS] *Profile created*\n"
                f"Name : `{name}`\n"
                f"Lang : `{language}`\n"
                f"ID   : `{pid}`\n\n"
                "Now send voice messages to add samples."
            )
            tg_state.user_sessions[chat_id] = {
                "flow": "voice",
                "awaiting": "voice_sample",
                "profile_id": pid,
                "profile_name": name,
                "samples_count": 0,
            }
        else:
            send_telegram(f"[ERROR] Create profile: {result.get('error', '?')}")
    except Exception as exc:
        send_telegram(f"[ERROR] Create profile: `{str(exc)[:300]}`")


def _voice_upload_sample_thread(chat_id: int, profile_id: str, audio_path: str):
    try:
        from skills.audio.skill import upload_sample
        result = upload_sample(profile_id, audio_path)
        if result["success"]:
            session = tg_state.user_sessions.get(chat_id, {})
            count = session.get("samples_count", 0) + 1
            session["samples_count"] = count
            tg_state.user_sessions[chat_id] = session

            send_telegram(
                f"[SUCCESS] Sample #{count} uploaded to profile `{profile_id}`",
                reply_markup={
                    "inline_keyboard": [[
                        {"text": "Upload Another", "callback_data": "vsample_more"},
                        {"text": "Done", "callback_data": "vsample_done"},
                    ]]
                },
            )
        else:
            send_telegram(f"[ERROR] Sample upload: {result.get('error', '?')}")
    except Exception as exc:
        send_telegram(f"[ERROR] Sample upload: `{str(exc)[:300]}`")
    finally:
        try:
            os.remove(audio_path)
        except OSError:
            pass


def _voice_delete_profile_thread(chat_id: int, profile_id: str):
    try:
        from skills.audio.skill import delete_profile
        result = delete_profile(profile_id)
        if result["success"]:
            prefs = tg_state.voice_prefs.get(chat_id, {})
            if prefs.get("profile_id") == profile_id:
                tg_state.voice_prefs.pop(chat_id, None)
            send_telegram(f"[SUCCESS] Profile `{profile_id}` deleted.")
        else:
            send_telegram(f"[ERROR] Delete: {result.get('error', '?')}")
    except Exception as exc:
        send_telegram(f"[ERROR] Delete profile: `{str(exc)[:300]}`")


def _voice_tts_thread(chat_id: int, text: str, profile_id: str = None, language: str = "en"):
    try:
        from skills.audio.skill import speak
        result = speak(text, language=language, profile_id=profile_id or None)
        if result["success"]:
            _send_voice_file(chat_id, result["path"])
            try:
                os.remove(result["path"])
            except OSError:
                pass
        else:
            send_telegram(f"[ERROR] TTS: {result.get('error', '?')}")
    except Exception as exc:
        send_telegram(f"[ERROR] TTS: `{str(exc)[:300]}`")


async def handle_voice_wizard_text(update: Update, chat_id: int, text: str, session: dict):
    if session.get("flow") == "voice" and session.get("awaiting") == "voice_profile_name":
        session["profile_name"] = text
        session["awaiting"] = "voice_profile_lang"
        tg_state.user_sessions[chat_id] = session
        await update.message.reply_text(
            f"*Profile Name:* `{text}`\n\nSelect language for this profile:",
            parse_mode="Markdown",
            reply_markup=kb_voice_profile_lang(),
        )
        return True

    if session.get("flow") == "voice" and session.get("awaiting") == "voice_generate_text":
        prefs = tg_state.voice_prefs.get(chat_id, {})
        profile_id = prefs.get("profile_id", "")
        language = prefs.get("language", "en")
        tg_state.user_sessions.pop(chat_id, None)
        await update.message.reply_text(
            "*Generating voice...*\n"
            f"Profile: `{prefs.get('profile_name', 'default')}`\n"
            f"Language: `{language}`",
            parse_mode="Markdown",
        )
        threading.Thread(
            target=_voice_tts_thread,
            args=(chat_id, text, profile_id, language),
            daemon=True,
        ).start()
        return True

    return False


async def handle_voice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str, session: dict):
    chat_id = update.effective_chat.id
    # Callback queries nutzen update.callback_query.message, nicht update.message
    q = update.callback_query
    message = q.message if q else update.message

    if data == "voice_generate":
        session["flow"] = "voice"
        session["awaiting"] = "voice_generate_text"
        tg_state.user_sessions[chat_id] = session
        prefs = tg_state.voice_prefs.get(chat_id, {})
        await message.reply_text(
            "*Generate Voice*\n\n"
            f"Profile: `{prefs.get('profile_name', 'default')}`\n"
            f"Language: `{prefs.get('language', 'en')}`\n\n"
            "Type the text you want to convert to speech:",
            parse_mode="Markdown",
        )
        return True

    if data == "voice_list":
        threading.Thread(target=_voice_list_profiles_thread, args=(chat_id,), daemon=True).start()
        return True

    if data == "voice_create":
        session["flow"] = "voice"
        session["awaiting"] = "voice_profile_name"
        tg_state.user_sessions[chat_id] = session
        await message.reply_text(
            "*Create Voice Profile*\n\nEnter a name for the new profile:",
            parse_mode="Markdown",
        )
        return True

    if data == "voice_select":
        try:
            from skills.audio.skill import list_profiles
            result = list_profiles()
            profiles = result.get("profiles", []) if result["success"] else []
            session["flow"] = "voice"
            session["awaiting"] = "voice_profile_select"
            tg_state.user_sessions[chat_id] = session
            await message.reply_text(
                "*Select Voice Profile for TTS:*",
                parse_mode="Markdown",
                reply_markup=kb_voice_profiles_select(profiles),
            )
        except Exception as exc:
            await message.reply_text(f"Error loading profiles: {exc}")
        return True

    if data == "voice_delete":
        try:
            from skills.audio.skill import list_profiles
            result = list_profiles()
            profiles = result.get("profiles", []) if result["success"] else []
            session["flow"] = "voice"
            session["awaiting"] = "voice_profile_delete"
            tg_state.user_sessions[chat_id] = session
            await message.reply_text(
                "*Delete Voice Profile:*\nSelect profile to delete:",
                parse_mode="Markdown",
                reply_markup=kb_voice_profiles_select(profiles),
            )
        except Exception as exc:
            await message.reply_text(f"Error loading profiles: {exc}")
        return True

    if data == "voice_lang":
        await message.reply_text(
            "*Select TTS Language:*",
            parse_mode="Markdown",
            reply_markup=kb_voice_language(),
        )
        return True

    if data == "voice_status":
        threading.Thread(target=_voice_status_thread, args=(chat_id,), daemon=True).start()
        return True

    if data == "voice_cancel":
        tg_state.user_sessions.pop(chat_id, None)
        await message.reply_text("Voice operation cancelled.")
        return True

    if data.startswith("vlang_"):
        lang = data[len("vlang_"):]
        if chat_id not in tg_state.voice_prefs:
            tg_state.voice_prefs[chat_id] = {}
        tg_state.voice_prefs[chat_id]["language"] = lang
        await message.reply_text(
            f"*TTS Language set to:* `{lang}`",
            parse_mode="Markdown",
        )
        return True

    if data.startswith("vplang_"):
        lang = data[len("vplang_"):]
        name = session.get("profile_name", "Unnamed")
        tg_state.user_sessions.pop(chat_id, None)
        await message.reply_text(f"*Creating profile:* `{name}` ({lang})...", parse_mode="Markdown")
        threading.Thread(
            target=_voice_create_profile_thread,
            args=(chat_id, name, lang),
            daemon=True,
        ).start()
        return True

    if data.startswith("vprofile_"):
        pid = data[len("vprofile_"):]
        if pid == "none":
            await message.reply_text("No profiles available. Create one first with /voice.")
            tg_state.user_sessions.pop(chat_id, None)
            return True

        awaiting = session.get("awaiting", "")

        if awaiting == "voice_profile_select":
            if chat_id not in tg_state.voice_prefs:
                tg_state.voice_prefs[chat_id] = {}
            tg_state.voice_prefs[chat_id]["profile_id"] = pid
            tg_state.voice_prefs[chat_id]["profile_name"] = pid
            tg_state.user_sessions.pop(chat_id, None)
            await message.reply_text(
                f"*Voice profile selected:* `{pid}`\n"
                "All TTS will use this profile.",
                parse_mode="Markdown",
            )
            return True

        if awaiting == "voice_profile_delete":
            session["delete_profile_id"] = pid
            tg_state.user_sessions[chat_id] = session
            await message.reply_text(
                f"*Really delete profile* `{pid}`?",
                parse_mode="Markdown",
                reply_markup=kb_voice_delete_confirm(pid, pid),
            )
            return True

    if data.startswith("vdelete_yes_"):
        pid = data[len("vdelete_yes_"):]
        tg_state.user_sessions.pop(chat_id, None)
        threading.Thread(
            target=_voice_delete_profile_thread,
            args=(chat_id, pid),
            daemon=True,
        ).start()
        return True

    if data == "vsample_more":
        await message.reply_text("Send another voice message as sample.")
        return True

    if data == "vsample_done":
        count = session.get("samples_count", 0)
        profile_name = session.get("profile_name", "?")
        profile_id = session.get("profile_id", "?")
        tg_state.user_sessions.pop(chat_id, None)

        if chat_id not in tg_state.voice_prefs:
            tg_state.voice_prefs[chat_id] = {}
        tg_state.voice_prefs[chat_id]["profile_id"] = profile_id
        tg_state.voice_prefs[chat_id]["profile_name"] = profile_name

        await message.reply_text(
            "*Profile complete!*\n\n"
            f"Name    : `{profile_name}`\n"
            f"Samples : {count}\n"
            f"ID      : `{profile_id}`\n\n"
            "This profile is now active for TTS.",
            parse_mode="Markdown",
        )
        return True

    return False
