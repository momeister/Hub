"""
core/telegram/handlers/optimize.py -- Self-Optimizer Telegram-Handler
======================================================================
/optimize Command, Status-Anzeige und Background-Thread-Management.
"""

from __future__ import annotations

import threading
import time

from telegram import Update
from telegram.ext import ContextTypes

from core.config import ROOT_DIR
from core.dual_logger import BuildLogger
from core.scheduler import acquire_gpu, release_gpu
from core.telegram import state as tg_state
from core.telegram.keyboards import (
    kb_optimizer_mode,
    kb_optimizer_running,
)
from core.telegram.settings import is_allowed
from core.utils import send_telegram


# ------------------------------------------------------------------
# Background Thread
# ------------------------------------------------------------------

def _run_optimizer_thread(chat_id: int, answers: dict) -> None:
    """Background-Thread der den Optimierungs-Loop ausfuehrt."""
    from skills.self_optimizer.config import OptimizerConfig
    from skills.self_optimizer.optimizer_core import OptimizationEngine

    # GPU reservieren
    if not acquire_gpu("self_optimizer", timeout=60):
        send_telegram("*Optimizer:* GPU belegt. Versuche es spaeter.")
        tg_state.optimizer_active = None
        tg_state.optimizer_state["state"] = "idle"
        tg_state.dispatcher_alive.set()
        return

    config = OptimizerConfig.from_env()
    blog = BuildLogger(jsonl_mode=False)

    engine = OptimizationEngine(
        project_dir=str(ROOT_DIR),
        config=config,
        blog=blog,
        notify_callback=send_telegram,
    )
    tg_state.optimizer_engine = engine

    mode = answers.get("mode", "task")
    task = answers.get("task", "")
    iterations = int(answers.get("iterations", "1"))

    # Modell-Auswahl
    model_modes = {
        "1": ("deepseek-r1:8b", "qwen2.5-coder:7b"),
        "2": ("deepseek-r1:32b", "qwen2.5-coder:14b"),
        "3": ("gpt-oss:120b", "qwen3-coder-next"),
    }
    reasoning_model, coding_model = model_modes.get(
        answers.get("model_mode", "2"),
        ("deepseek-r1:32b", "qwen3-coder-next"),
    )

    run = engine.start(
        mode=mode,
        task=task,
        iterations=iterations,
        reasoning_model=reasoning_model,
        coding_model=coding_model,
    )

    tg_state.optimizer_state = {
        "state": "running",
        "mode": mode,
        "task": task[:200],
        "iteration": 0,
        "iterations_target": iterations,
        "current_agent": "",
        "last_change": "",
        "last_decision": "",
        "errors": 0,
        "started_at": time.time(),
        "version": "",
        "reasoning_model": reasoning_model,
        "coding_model": coding_model,
    }

    iter_label = str(iterations) if iterations > 0 else "Endlos"
    send_telegram(
        f"*Self-Optimizer gestartet*\n\n"
        f"Modus      : {'Auto' if mode == 'auto' else 'Task'}\n"
        f"Aufgabe    : _{task[:200] if task else 'KI-Entscheidung'}_\n"
        f"Iterationen: {iter_label}\n"
        f"Reasoning  : `{reasoning_model}`\n"
        f"Coding     : `{coding_model}`",
    )

    try:
        engine.execute_loop()
    except Exception as exc:
        send_telegram(f"*Optimizer Fehler:* `{str(exc)[:300]}`")
    finally:
        tg_state.optimizer_active = None
        tg_state.optimizer_engine = None
        tg_state.optimizer_state["state"] = "idle"
        release_gpu("self_optimizer")
        tg_state.dispatcher_alive.set()


# ------------------------------------------------------------------
# Telegram Command Handler
# ------------------------------------------------------------------

async def cmd_optimize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /optimize command."""
    chat_id = update.effective_chat.id
    if not is_allowed(chat_id):
        return

    args_text = " ".join(context.args).strip() if context.args else ""

    # /optimize stop
    if args_text.lower() == "stop":
        if tg_state.optimizer_engine:
            tg_state.optimizer_engine.stop()
            await update.message.reply_text(
                "*Optimizer stoppt...* (beendet aktuellen Agenten)",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text("Kein Optimizer aktiv.")
        return

    # /optimize status
    if args_text.lower() == "status":
        await _show_optimizer_status(update)
        return

    # Schon aktiv?
    if tg_state.optimizer_active:
        await update.message.reply_text(
            "Optimizer laeuft bereits.\n"
            "/optimize stop   -- Stoppen\n"
            "/optimize status -- Fortschritt"
        )
        return

    # Builder aktiv?
    if tg_state.active_build:
        await update.message.reply_text(
            "Builder laeuft. Warte bis er fertig ist."
        )
        return

    # Optimizer-Wizard starten
    tg_state.user_sessions[chat_id] = {"flow": "optimizer"}
    await update.message.reply_text(
        "*Self-Optimizer*\n\n"
        "Analysiert und verbessert die AI HUB Codebase.\n"
        "Alle Aenderungen auf experimentellem Git-Branch.\n\n"
        "Modus waehlen:",
        parse_mode="Markdown",
        reply_markup=kb_optimizer_mode(),
    )


async def _show_optimizer_status(update: Update) -> None:
    """Aktuellen Optimizer-Status anzeigen."""
    state = tg_state.optimizer_state
    if state["state"] == "idle":
        await update.message.reply_text(
            "Kein Optimizer aktiv.\nStarten mit /optimize"
        )
        return

    elapsed = int(time.time() - (state["started_at"] or time.time()))
    mins, secs = divmod(elapsed, 60)

    iter_target = state.get("iterations_target", "?")
    if iter_target == 0:
        iter_target = "Endlos"

    await update.message.reply_text(
        f"*Optimizer Status* -- {mins}m {secs}s\n\n"
        f"State      : `{state['state']}`\n"
        f"Modus      : {state.get('mode', '?')}\n"
        f"Iteration  : {state.get('iteration', 0)}/{iter_target}\n"
        f"Agent      : {state.get('current_agent', '--')}\n"
        f"Aenderung  : _{state.get('last_change', '--')[:100]}_\n"
        f"Entscheid  : {state.get('last_decision', '--')}\n"
        f"Fehler     : {state.get('errors', 0)}\n"
        f"Version    : {state.get('version', '--')}\n"
        f"Reasoning  : `{state.get('reasoning_model', '--')}`\n"
        f"Coding     : `{state.get('coding_model', '--')}`",
        parse_mode="Markdown",
        reply_markup=kb_optimizer_running() if state["state"] != "idle" else None,
    )
