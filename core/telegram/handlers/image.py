"""
core/telegram/handlers/image.py — ComfyUI image generation
==========================================================
"""

from __future__ import annotations

import os
import re
import threading

from telegram import Update
from telegram.ext import ContextTypes

from core.telegram.keyboards import (
    kb_image_amount,
    kb_image_confirm,
    kb_image_model,
    kb_image_size,
)
from core.telegram import state as tg_state
from core.telegram.settings import is_allowed
from core.utils import send_telegram, send_telegram_photo, send_telegram_video, info


def _parse_image_oneline(text: str) -> dict:
    result = {"prompt": text}

    size_match = re.search(r"\b(\d{3,4})\s*[xX]\s*(\d{3,4})\b", text)
    if size_match:
        result["width"] = int(size_match.group(1))
        result["height"] = int(size_match.group(2))
        text = text[:size_match.start()] + text[size_match.end():]

    try:
        from skills.comfyui.skill import get_available_models
        models = get_available_models()
    except Exception:
        models = []

    for model in models:
        pattern = re.compile(r"\b" + re.escape(model) + r"\b", re.IGNORECASE)
        if pattern.search(text):
            result["model"] = model
            text = pattern.sub("", text)
            break

    text = re.sub(r"\s*,\s*,\s*", ", ", text)
    text = text.strip().strip(",").strip()
    result["prompt"] = text

    return result


def _run_image_thread(chat_id: int, params: dict):
    tg_state.dispatcher_alive.clear()
    try:
        from skills.comfyui.skill import run as comfyui_run

        video_exts = (".mp4", ".webm", ".avi", ".mov", ".mkv", ".gif")

        def on_progress(elapsed, status, pct=0):
            mins, secs = divmod(elapsed, 60)
            send_telegram(f"[INFO] *ComfyUI* -- {pct}% {status} ({mins}m {secs}s)")

        result = comfyui_run(
            prompt=params.get("prompt", ""),
            model=params.get("model", ""),
            negative=params.get("negative", ""),
            width=params.get("width", 1248),
            height=params.get("height", 821),
            steps=params.get("steps", 20),
            amount=params.get("amount", 1),
            reference_image_path=params.get("reference_image_path", ""),
            progress_callback=on_progress,
            output_dir=params.get("output_dir", ""),
        )

        if result.get("success"):
            output_paths = result.get("image_paths", [])
            send_telegram(f"[SUCCESS] {result.get('message', 'Done')}")

            for out_path in output_paths:
                try:
                    ext = os.path.splitext(out_path)[1].lower()
                    caption = params.get("prompt", "")[:200]
                    if ext in video_exts:
                        send_telegram_video(
                            out_path,
                            caption=caption,
                            chat_id=str(chat_id),
                        )
                    else:
                        send_telegram_photo(
                            out_path,
                            caption=caption,
                            chat_id=str(chat_id),
                        )
                except Exception as exc:
                    info(f"Failed to send output: {exc}")

            _ask_file_cleanup(chat_id, output_paths)

        else:
            error_msg = result.get("error", "Unknown error")
            send_telegram(f"[ERROR] Image generation failed: {error_msg}")

    except Exception as exc:
        send_telegram(f"[ERROR] ComfyUI error: `{str(exc)[:300]}`")
    finally:
        tg_state.dispatcher_alive.set()
        info("Dispatcher active again (image generation done)")


def _ask_file_cleanup(chat_id: int, output_paths: list[str]):
    import tempfile as _tf
    temp_dir = _tf.gettempdir()
    temp_files = [p for p in output_paths if p.startswith(temp_dir)]
    if not temp_files:
        return

    send_telegram(
        "[INFO] *File Cleanup*\n"
        f"{len(temp_files)} temp file(s) on disk.\n"
        "Reply `/cleanup delete` to remove or `/cleanup keep` to archive.",
    )


async def cmd_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_allowed(chat_id):
        return

    if not tg_state.dispatcher_alive.is_set():
        await update.message.reply_text(
            "GPU is busy (build or generation running).\n"
            "/builder or /status for info."
        )
        return

    args_text = " ".join(context.args) if context.args else ""

    if args_text:
        parsed = _parse_image_oneline(args_text)
        if not parsed.get("prompt"):
            await update.message.reply_text("Please provide a prompt.")
            return

        await update.message.reply_text(
            "*Generating...*\n"
            f"Prompt : _{parsed['prompt'][:150]}_\n"
            f"Model  : `{parsed.get('model', 'auto')}`\n"
            f"Size   : {parsed.get('width', 1024)}x{parsed.get('height', 1024)}",
            parse_mode="Markdown",
        )
        threading.Thread(
            target=_run_image_thread,
            args=(chat_id, parsed),
            daemon=True,
        ).start()
    else:
        tg_state.user_sessions[chat_id] = {"flow": "image", "awaiting": "img_model"}
        await update.message.reply_text(
            "*Image Generation*\n\nSelect model:",
            parse_mode="Markdown",
            reply_markup=kb_image_model(),
        )


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_allowed(chat_id):
        return

    session = tg_state.user_sessions.get(chat_id, {})

    try:
        import tempfile as _tempfile
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        tmp = _tempfile.NamedTemporaryFile(delete=False, suffix=".jpg", prefix="ref_")
        await file.download_to_drive(tmp.name)
        tmp.close()
        saved_path = tmp.name
    except Exception as exc:
        await update.message.reply_text(f"Failed to download photo: {exc}")
        return

    if session.get("flow") == "image":
        session["img_reference"] = saved_path
        tg_state.user_sessions[chat_id] = session
        await update.message.reply_text("Reference image updated.")
    else:
        if not tg_state.dispatcher_alive.is_set():
            await update.message.reply_text(
                "GPU is busy (build or generation running).\n"
                "/status for info."
            )
            try:
                os.remove(saved_path)
            except OSError:
                pass
            return

        tg_state.user_sessions[chat_id] = {
            "flow": "image",
            "awaiting": "img_model",
            "img_reference": saved_path,
        }
        await update.message.reply_text(
            "*Image Generation (img2img)*\n\n"
            "Reference image saved.\n"
            "Select model/workflow:",
            parse_mode="Markdown",
            reply_markup=kb_image_model(),
        )


async def handle_image_wizard_text(update: Update, chat_id: int, text: str, session: dict):
    if session.get("flow") == "image" and session.get("awaiting") == "img_prompt":
        session["img_prompt"] = text
        session["awaiting"] = "img_amount"
        tg_state.user_sessions[chat_id] = session
        await update.message.reply_text(
            "*How many images?*",
            parse_mode="Markdown",
            reply_markup=kb_image_amount(),
        )
        return True
    return False


def build_image_params(session: dict) -> dict:
    return {
        "prompt": session.get("img_prompt", ""),
        "model": session.get("img_model", ""),
        "width": session.get("img_width", 1024),
        "height": session.get("img_height", 1024),
        "amount": session.get("img_amount", 1),
        "reference_image_path": session.get("img_reference", ""),
    }


def start_image_thread(chat_id: int, params: dict):
    threading.Thread(
        target=_run_image_thread,
        args=(chat_id, params),
        daemon=True,
    ).start()
