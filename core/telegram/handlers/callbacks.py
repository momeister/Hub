"""
core/telegram/handlers/callbacks.py — Callback query handler
=============================================================
"""

from __future__ import annotations

import threading

from telegram import Update
from telegram.ext import ContextTypes

from core.telegram import state as tg_state
from core.telegram.builder import run_builder
from core.telegram.handlers.image import build_image_params, start_image_thread
from core.telegram.handlers.voice import handle_voice_callback
from core.telegram.keyboards import (
    kb_image_confirm,
    kb_image_size,
    kb_image_amount,
    kb_project_list,
    kb_voice_profiles_select,
    kb_opmode,
    kb_tests,
    kb_internet,
    kb_optimizer_iterations,
    kb_optimizer_model,
    kb_optimizer_confirm,
)
from core.telegram.keyboards import kb_yesno
from core.telegram.settings import is_allowed
from core.utils import send_telegram


def _stop_voicebox_thread(chat_id: int):
    try:
        from skills.audio.skill import stop_voicebox
        ok = stop_voicebox()
        if ok:
            send_telegram("Voicebox stopped.")
        else:
            send_telegram("Voicebox was not running.")
    except Exception as exc:
        send_telegram(f"[ERROR] Stop Voicebox: `{str(exc)[:200]}`")


def _start_voicebox_thread(chat_id: int):
    try:
        from skills.audio.skill import _ensure_voicebox_running
        ok = _ensure_voicebox_running(timeout=60)
        if ok:
            send_telegram("Voicebox started and reachable.")
        else:
            send_telegram("Failed to start Voicebox within 60s.")
    except Exception as exc:
        send_telegram(f"[ERROR] Start Voicebox: `{str(exc)[:200]}`")


def _stop_comfyui_thread(chat_id: int):
    try:
        import skills.comfyui.skill as comfyui_skill
        if comfyui_skill._comfyui_process is not None and comfyui_skill._comfyui_process.poll() is None:
            comfyui_skill._comfyui_process.terminate()
            try:
                comfyui_skill._comfyui_process.wait(timeout=10)
            except Exception:
                comfyui_skill._comfyui_process.kill()
            comfyui_skill._comfyui_process = None
            send_telegram("ComfyUI stopped.")
        else:
            send_telegram("ComfyUI was not running.")
    except Exception as exc:
        send_telegram(f"[ERROR] Stop ComfyUI: `{str(exc)[:200]}`")


def _start_comfyui_thread(chat_id: int):
    try:
        from skills.comfyui.skill import _ensure_comfyui_running
        ok = _ensure_comfyui_running(timeout=120)
        if ok:
            send_telegram("ComfyUI started and reachable.")
        else:
            send_telegram("Failed to start ComfyUI within 120s.")
    except Exception as exc:
        send_telegram(f"[ERROR] Start ComfyUI: `{str(exc)[:200]}`")


async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    data = q.data
    session = tg_state.user_sessions.get(chat_id, {})

    if not is_allowed(chat_id):
        return

    if data == "chat_exit":
        tg_state.user_sessions.pop(chat_id, None)
        await q.message.reply_text("Chat beendet.")
        return

    if data == "svc_noop":
        return

    if data == "svc_refresh":
        from core.telegram.keyboards import kb_services
        from core.services_status import get_services_status
        from core.telegram.settings import SERVICES_CFG
        services = get_services_status(SERVICES_CFG)
        lines = ["*Services*\n"]
        for svc in services:
            status = "Online" if svc["online"] else "Offline"
            lines.append(f"  {svc['name']:10s}: {status} ({svc['port']})")
        await q.message.edit_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=kb_services(),
        )
        return

    if data == "svc_stop_voicebox":
        await q.message.edit_text("Stopping Voicebox...")
        threading.Thread(target=_stop_voicebox_thread, args=(chat_id,), daemon=True).start()
        return

    if data == "svc_start_voicebox":
        await q.message.edit_text("Starting Voicebox...")
        threading.Thread(target=_start_voicebox_thread, args=(chat_id,), daemon=True).start()
        return

    if data == "svc_stop_comfyui":
        await q.message.edit_text("Stopping ComfyUI...")
        threading.Thread(target=_stop_comfyui_thread, args=(chat_id,), daemon=True).start()
        return

    if data == "svc_start_comfyui":
        await q.message.edit_text("Starting ComfyUI...")
        threading.Thread(target=_start_comfyui_thread, args=(chat_id,), daemon=True).start()
        return

    if data.startswith("mode_"):
        mode = data.split("_")[1]
        session["mode"] = mode
        tg_state.user_sessions[chat_id] = session
        if mode == "5":
            session["awaiting"] = "custom_manager"
            await q.message.reply_text("Manager-Modell (z.B. llama3:70b):")
        else:
            session["awaiting"] = "opmode"
            await q.message.reply_text("*Neues oder bestehendes Projekt?*", parse_mode="Markdown", reply_markup=kb_opmode())
        return

    if data.startswith("opmode_"):
        op = data.split("_")[1]
        session["opmode"] = op
        tg_state.user_sessions[chat_id] = session
        if op == "2":
            session["awaiting"] = "edit_project_select"
            await q.message.reply_text(
                "*Projekt zum Bearbeiten waehlen:*",
                parse_mode="Markdown",
                reply_markup=kb_project_list(),
            )
        else:
            session["awaiting"] = "goal"
            await q.message.reply_text("*Was soll gebaut werden?*\n_Beschreibe moeglichst genau: Sprache, Features, etc._", parse_mode="Markdown")
        return

    if data.startswith("editproject_"):
        project_name = data[len("editproject_"):]
        if project_name == "none":
            tg_state.user_sessions.pop(chat_id, None)
            await q.message.reply_text("Keine Projekte vorhanden. Erstelle zuerst ein Projekt mit /build")
            return
        session["opmode"] = "2"
        session["project_name"] = project_name
        session["awaiting"] = "edit_goal"
        tg_state.user_sessions[chat_id] = session
        await q.message.reply_text(
            f"*Projekt:* `{project_name}`\n\n"
            "Was soll geaendert werden?\n"
            "_z.B. Fix the login bug, Add dark mode, Improve error messages..._",
            parse_mode="Markdown",
        )
        return

    if data.startswith("scope_"):
        session["scope"] = data.split("_")[1]
        tg_state.user_sessions[chat_id] = session
        await q.message.reply_text("*Tests generieren?*", parse_mode="Markdown", reply_markup=kb_tests())
        return

    if data.startswith("tests_"):
        session["tests"] = data.split("_")[1]
        tg_state.user_sessions[chat_id] = session
        await q.message.reply_text("*Internet-Zugriff?*", parse_mode="Markdown", reply_markup=kb_internet())
        return

    if data.startswith("internet_"):
        session["internet"] = data.split("_")[1]
        tg_state.user_sessions[chat_id] = session
        mn = {"1": "FAST", "2": "AVERAGE", "3": "GOD MODE", "4": "UNCENSORED", "5": "Custom"}
        sl = {"1": "Auto", "2": "Kompakt", "3": "Voll"}
        internet_txt = "Ja" if session.get("internet", "n") == "y" else "Nein"
        await q.message.reply_text(
            "*Zusammenfassung*\n\n"
            f"Modus    : {mn.get(session.get('mode', '2'))}\n"
            f"Scope    : {sl.get(session.get('scope', '1'))}\n"
            f"Tests    : {'Ja' if session.get('tests', '1') == '2' else 'Nein'}\n"
            f"Internet : {internet_txt}\n"
            f"Auftrag  : _{session.get('goal', '')[:200]}_\n\nStarten?",
            parse_mode="Markdown",
            reply_markup=kb_yesno("confirm_yes", "confirm_no"),
        )
        return

    if data == "confirm_yes":
        if tg_state.active_build:
            await q.message.reply_text("Build laeuft. /builder")
            return
        answers = dict(session)
        tg_state.user_sessions[chat_id] = {}
        threading.Thread(target=run_builder, args=(chat_id, answers), daemon=True).start()
        return

    if data == "confirm_no":
        tg_state.user_sessions.pop(chat_id, None)
        await q.message.reply_text("Abgebrochen.")
        return

    if data == "techapprove_yes":
        signal_path = getattr(tg_state, 'build_signal_path', None)
        if signal_path and tg_state.active_build_proc is not None:
            try:
                with open(signal_path, "w") as f:
                    f.write("approved\n")
            except OSError as exc:
                await q.message.reply_text(f"Fehler: `{exc}`")
                return
            await q.message.reply_text("*Tech Stack genehmigt* — Build wird fortgesetzt...", parse_mode="Markdown")
        else:
            await q.message.reply_text("Kein aktiver Build zum Genehmigen.")
        return

    if data == "techapprove_no":
        signal_path = getattr(tg_state, 'build_signal_path', None)
        if signal_path and tg_state.active_build_proc is not None:
            try:
                with open(signal_path, "w") as f:
                    f.write("cancelled\n")
            except OSError:
                pass
            try:
                tg_state.active_build_proc.terminate()
            except OSError:
                pass
            await q.message.reply_text("*Build abgebrochen.* Tech Stack abgelehnt.", parse_mode="Markdown")
        else:
            await q.message.reply_text("Kein aktiver Build zum Abbrechen.")
        return

    if data == "build_cancel":
        signal_path = getattr(tg_state, 'build_signal_path', None)
        if tg_state.active_build_proc is not None:
            if signal_path:
                try:
                    with open(signal_path, "w") as f:
                        f.write("cancelled\n")
                except OSError:
                    pass
            try:
                tg_state.active_build_proc.terminate()
            except OSError:
                pass
            await q.message.reply_text("*Build abgebrochen.*", parse_mode="Markdown")
        else:
            await q.message.reply_text("Kein aktiver Build.")
        return

    if data == "dl_confirm_yes":
        dl_args = session.get("dl_args", {})
        tg_state.user_sessions.pop(chat_id, None)
        await q.message.reply_text("*Download gestartet...*", parse_mode="Markdown")
        threading.Thread(
            target=_run_skill_thread_wrapper,
            args=("downloader", dl_args, chat_id),
            daemon=True,
        ).start()
        return

    if data == "dl_confirm_no":
        tg_state.user_sessions.pop(chat_id, None)
        await q.message.reply_text("Download abgebrochen.")
        return

    if data.startswith("imgmodel_"):
        model = data[len("imgmodel_"):]
        if model == "none":
            await q.message.reply_text(
                "No workflow files found.\n"
                "Export your workflow from ComfyUI web UI (Save API Format) "
                "and save to skills/comfyui/workflows/"
            )
            tg_state.user_sessions.pop(chat_id, None)
            return
        session["img_model"] = model
        session["awaiting"] = "img_size"
        tg_state.user_sessions[chat_id] = session
        await q.message.reply_text(
            f"Model: *{model}*\n\nSelect size:",
            parse_mode="Markdown",
            reply_markup=kb_image_size(),
        )
        return

    if data.startswith("imgsize_"):
        parts = data[len("imgsize_"):].split("_")
        if len(parts) == 2:
            session["img_width"] = int(parts[0])
            session["img_height"] = int(parts[1])
        session["awaiting"] = "img_prompt"
        tg_state.user_sessions[chat_id] = session
        ref_hint = "\nYou can also send a photo as reference image." if not session.get("img_reference") else ""
        await q.message.reply_text(
            f"Size: *{session.get('img_width', 1024)}x{session.get('img_height', 1024)}*\n\n"
            f"Type your prompt (what to generate).{ref_hint}",
            parse_mode="Markdown",
        )
        return

    if data.startswith("imgamount_"):
        amount = int(data[len("imgamount_"):])
        session["img_amount"] = amount
        session["awaiting"] = "img_confirm"
        tg_state.user_sessions[chat_id] = session
        ref_txt = "Yes" if session.get("img_reference") else "No"
        await q.message.reply_text(
            "*Image Summary*\n\n"
            f"Model     : `{session.get('img_model', 'auto')}`\n"
            f"Size      : {session.get('img_width', 1024)}x{session.get('img_height', 1024)}\n"
            f"Amount    : {amount}\n"
            f"Prompt    : _{session.get('img_prompt', '')[:200]}_\n"
            f"Reference : {ref_txt}\n\n"
            "Generate?",
            parse_mode="Markdown",
            reply_markup=kb_image_confirm(),
        )
        return

    if data == "imgconfirm_yes":
        if not tg_state.dispatcher_alive.is_set():
            await q.message.reply_text("GPU is busy. Try again later.")
            return
        params = build_image_params(session)
        tg_state.user_sessions.pop(chat_id, None)
        await q.message.reply_text("*Generating...*", parse_mode="Markdown")
        start_image_thread(chat_id, params)
        return

    if data == "imgconfirm_no":
        ref = session.get("img_reference", "")
        if ref:
            try:
                import os
                os.remove(ref)
            except OSError:
                pass
        tg_state.user_sessions.pop(chat_id, None)
        await q.message.reply_text("Image generation cancelled.")
        return

    # ── Self-Optimizer Callbacks ────────────────────────────────────────────

    if data.startswith("optmode_"):
        opt_mode = data.split("_")[1]  # "task" oder "auto"
        session["flow"] = "optimizer"
        session["opt_mode"] = opt_mode
        tg_state.user_sessions[chat_id] = session
        if opt_mode == "task":
            session["awaiting"] = "opt_task"
            await q.message.reply_text(
                "*Was soll optimiert werden?*\n"
                "_Beschreibe das Feature, den Fix oder die Verbesserung._",
                parse_mode="Markdown",
            )
        else:
            await q.message.reply_text(
                "*Wie viele Iterationen?*",
                parse_mode="Markdown",
                reply_markup=kb_optimizer_iterations(),
            )
        return

    if data.startswith("optiter_"):
        iters = int(data.split("_")[1])
        session["opt_iterations"] = iters
        tg_state.user_sessions[chat_id] = session
        await q.message.reply_text(
            "*Modell-Qualitaet:*",
            parse_mode="Markdown",
            reply_markup=kb_optimizer_model(),
        )
        return

    if data.startswith("optmodel_"):
        session["opt_model_mode"] = data.split("_")[1]
        tg_state.user_sessions[chat_id] = session

        mode_labels = {"task": "Spezifische Aufgabe", "auto": "KI-Entscheidung"}
        iter_val = session.get("opt_iterations", 1)
        iter_label = str(iter_val) if iter_val > 0 else "Endlos"
        model_labels = {"1": "FAST", "2": "AVERAGE", "3": "GOD MODE"}

        await q.message.reply_text(
            "*Optimizer Zusammenfassung*\n\n"
            f"Modus      : {mode_labels.get(session.get('opt_mode', 'task'), '?')}\n"
            f"Aufgabe    : _{session.get('opt_task', 'KI-Entscheidung')[:200]}_\n"
            f"Iterationen: {iter_label}\n"
            f"Qualitaet  : {model_labels.get(session.get('opt_model_mode', '2'), '?')}\n\n"
            "Starten?",
            parse_mode="Markdown",
            reply_markup=kb_optimizer_confirm(),
        )
        return

    if data == "optconfirm_yes":
        if tg_state.optimizer_active or tg_state.active_build:
            await q.message.reply_text("Eine andere Aufgabe laeuft bereits.")
            return

        answers = {
            "mode": session.get("opt_mode", "task"),
            "task": session.get("opt_task", ""),
            "iterations": session.get("opt_iterations", 1),
            "model_mode": session.get("opt_model_mode", "2"),
        }
        tg_state.user_sessions.pop(chat_id, None)
        tg_state.optimizer_active = chat_id
        tg_state.dispatcher_alive.clear()

        threading.Thread(
            target=_run_optimizer_thread_wrapper,
            args=(chat_id, answers),
            daemon=True,
        ).start()
        return

    if data == "optconfirm_no":
        tg_state.user_sessions.pop(chat_id, None)
        await q.message.reply_text("Optimizer abgebrochen.")
        return

    if data == "optmerge_yes":
        engine = tg_state.optimizer_engine
        if engine and hasattr(engine, "approve_merge"):
            engine.approve_merge()
            await q.message.reply_text(
                "*Merge genehmigt* -- wird angewandt...",
                parse_mode="Markdown",
            )
        return

    if data == "optmerge_no":
        engine = tg_state.optimizer_engine
        if engine and hasattr(engine, "reject_merge"):
            engine.reject_merge()
            await q.message.reply_text(
                "*Merge abgelehnt* -- Rollback...",
                parse_mode="Markdown",
            )
        return

    if data == "optapproval_yes":
        from skills.self_optimizer.approval import ApprovalManager
        from core.config import ROOT_DIR
        am = ApprovalManager(str(ROOT_DIR), None)
        am.write_approval_signal(True)
        await q.message.reply_text("*Anfrage genehmigt.*", parse_mode="Markdown")
        return

    if data == "optapproval_no":
        from skills.self_optimizer.approval import ApprovalManager
        from core.config import ROOT_DIR
        am = ApprovalManager(str(ROOT_DIR), None)
        am.write_approval_signal(False)
        await q.message.reply_text("*Anfrage abgelehnt.*", parse_mode="Markdown")
        return

    if data == "optctl_stop":
        engine = tg_state.optimizer_engine
        if engine and hasattr(engine, "stop"):
            engine.stop()
            await q.message.reply_text(
                "*Optimizer stoppt...*", parse_mode="Markdown"
            )
        return

    if data == "optctl_pause":
        engine = tg_state.optimizer_engine
        if engine and hasattr(engine, "stop"):
            engine.stop()
            await q.message.reply_text(
                "*Optimizer pausiert.* Neu starten mit /optimize",
                parse_mode="Markdown",
            )
        return

    if data == "optctl_status":
        state = tg_state.optimizer_state
        await q.message.reply_text(
            f"*Optimizer:* `{state.get('state', 'idle')}`\n"
            f"Iteration: {state.get('iteration', 0)}/{state.get('iterations_target', '?')}\n"
            f"Agent: {state.get('current_agent', '--')}\n"
            f"Fehler: {state.get('errors', 0)}",
            parse_mode="Markdown",
        )
        return

    # ── Voice Callbacks ───────────────────────────────────────────────────

    handled = await handle_voice_callback(update, context, data, session)
    if handled:
        return

    if data.startswith("vprofile_"):
        from core.telegram.handlers.voice import handle_voice_callback
        await handle_voice_callback(update, context, data, session)
        return

    if data.startswith("vdelete_yes_"):
        from core.telegram.handlers.voice import handle_voice_callback
        await handle_voice_callback(update, context, data, session)
        return

    if data in {"vsample_more", "vsample_done"}:
        from core.telegram.handlers.voice import handle_voice_callback
        await handle_voice_callback(update, context, data, session)
        return


def _run_skill_thread_wrapper(skill, args, chat_id):
    from core.telegram.handlers.dispatcher import _run_skill_thread
    _run_skill_thread(skill, args, chat_id)


def _run_optimizer_thread_wrapper(chat_id: int, answers: dict):
    from core.telegram.handlers.optimize import _run_optimizer_thread
    _run_optimizer_thread(chat_id, answers)
