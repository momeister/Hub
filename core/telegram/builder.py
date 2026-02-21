"""
core/telegram/builder.py — Builder integration
===============================================
Runs the builder Docker container and parses JSONL events.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from core.telegram import state as tg_state
from core.telegram.settings import BUILDER_IMAGE, OUTPUT_BASE, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
from core.utils import send_telegram, info, warn, err


def _esc(text: str) -> str:
    """Escape Telegram Markdown v1 special characters in dynamic text."""
    if not text:
        return text
    for ch in ('_', '*', '`', '['):
        text = text.replace(ch, '\\' + ch)
    return text


def _sanitize(val: str) -> str:
    """Strip newlines from stdin values to prevent line-count desync."""
    return val.replace("\r", " ").replace("\n", " ").strip()


def _build_stdin(answers: dict) -> str:
    lines = []
    mode = answers.get("mode", "2")
    lines.append(mode)
    if mode == "5":
        lines += [
            _sanitize(answers.get("custom_manager", "")),
            _sanitize(answers.get("custom_coder", "")),
            _sanitize(answers.get("custom_ctx", "")),
        ]
    lines.append(answers.get("internet", "n"))
    op = answers.get("opmode", "1")
    lines.append(op)
    if op == "2":
        lines += [_sanitize(answers.get("project_name", "")), _sanitize(answers.get("goal", ""))]
    else:
        lines += [_sanitize(answers.get("goal", "")), answers.get("scope", "1"), answers.get("tests", "1")]
    return "\n".join(lines) + "\n"


def _parse_builder_line(line: str) -> dict:
    line = line.strip()
    if not line:
        return {"type": "ignore"}
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {"type": "log", "level": "debug", "message": line}


def run_builder(chat_id: int, answers: dict) -> None:
    tg_state.active_build = chat_id
    tg_state.active_build_proc = None

    # Set up file-based approval signal path (host side of Docker volume)
    output_base_resolved = str(Path(OUTPUT_BASE).resolve())
    tg_state.build_signal_path = str(Path(output_base_resolved) / ".build_approval")
    # Clean up stale signal from previous runs
    try:
        if os.path.exists(tg_state.build_signal_path):
            os.remove(tg_state.build_signal_path)
    except OSError:
        pass
    tg_state.dispatcher_alive.clear()

    # Model configs: (manager, coder)
    # Models run sequentially (one at a time) to fit in VRAM
    mode_models = {
        "1": ("deepseek-r1:8b", "qwen2.5-coder:7b"),
        "2": ("deepseek-r1:32b", "qwen2.5-coder:14b"),
        "3": ("gpt-oss:120b", "qwen3-coder-next"),
        "4": ("huihui_ai/qwen3-coder-next-abliterated", "huihui_ai/qwen3-coder-next-abliterated"),
        "5": (answers.get("custom_manager", "?"), answers.get("custom_coder", "?")),
    }
    mgr, cdr = mode_models.get(answers.get("mode", "2"), ("?", "?"))

    tg_state.build_state = {
        "phase": "Startet...",
        "detailed_phase": "Initializing agent pipeline",
        "files_done": 0,
        "files_total": 0,
        "current_file": "",
        "language": "",
        "errors": [],
        "started_at": time.time(),
        "coder_model": cdr,
        "manager_model": mgr,
        "active_model": mgr,
        "current_action": "Starting agent pipeline: Planner -> Retriever -> Coder -> Executor -> Critic",
    }
    # Reset event log and set quiet mode by default
    tg_state.build_events = []
    tg_state.build_verbose = False

    mode_labels = {"1": "FAST", "2": "AVERAGE", "3": "GOD MODE", "4": "UNCENSORED", "5": "Custom"}
    scope_labels = {"1": "Auto", "2": "Kompakt", "3": "Voll"}
    internet_label = "Ja" if answers.get("internet", "n") == "y" else "Nein"

    is_edit = answers.get("opmode") == "2"
    if is_edit:
        send_telegram(
            f"[EDIT] *Edit-Modus gestartet*\n"
            f"Projekt  : `{answers.get('project_name', '?')}`\n"
            f"Modus    : {mode_labels.get(answers.get('mode', '2'))}\n"
            f"Aenderung: _{answers.get('goal', '')[:200]}_\n"
            f"Manager  : `{mgr}`\n"
            f"Coder    : `{cdr}`\n\n"
            f"_Quiet Mode aktiv. /builder fuer Status._",
            reply_markup={
                "inline_keyboard": [[
                    {"text": "Verbose an", "callback_data": "build_verbose_on"},
                    {"text": "Builder Status", "callback_data": "build_status_query"},
                ]]
            },
        )
    else:
        send_telegram(
            f"[BUILD] *Builder gestartet*\n"
            f"Modus    : {mode_labels.get(answers.get('mode', '2'))}\n"
            f"Agents   : Planner -> Retriever -> Coder -> Executor -> Critic\n"
            f"Scope    : {scope_labels.get(answers.get('scope', '1'))}\n"
            f"Tests    : {'Ja' if answers.get('tests', '1') == '2' else 'Nein'}\n"
            f"Internet : {internet_label}\n"
            f"Auftrag  : _{answers.get('goal', '')[:200]}_\n"
            f"Manager  : `{mgr}`\n"
            f"Coder    : `{cdr}`\n\n"
            f"_Quiet Mode aktiv. /builder fuer Status._",
            reply_markup={
                "inline_keyboard": [[
                    {"text": "Verbose an", "callback_data": "build_verbose_on"},
                    {"text": "Builder Status", "callback_data": "build_status_query"},
                ]]
            },
        )

    stdin_data = _build_stdin(answers)
    output_dir = str(Path(OUTPUT_BASE).resolve()).replace("\\", "/")
    cmd = [
        "docker", "run", "--rm", "-i", "--add-host=host.docker.internal:host-gateway",
        "-v", f"{output_dir}:/app/output",
        "-e", "TRIGGERED_BY=telegram",
        "-e", f"TELEGRAM_TOKEN={TELEGRAM_TOKEN}",
        "-e", f"TELEGRAM_CHAT_ID={TELEGRAM_CHAT_ID}",
        BUILDER_IMAGE,
    ]

    files_written = []
    errors_found = []
    timeout_count = 0

    # Helper: send only if verbose mode is on; always store event
    def _tg_if_verbose(msg: str, **kwargs):
        """Send Telegram message only in verbose mode, always log to build_events."""
        tg_state.build_events.append({"msg": msg, "ts": time.time()})
        if tg_state.build_verbose:
            send_telegram(msg, **kwargs)

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        tg_state.active_build_proc = proc
        proc.stdin.write(stdin_data)
        proc.stdin.flush()

        for raw_line in proc.stdout:
            line = raw_line.rstrip()
            parsed = _parse_builder_line(line)
            ptype = parsed.get("type", "ignore")

            if ptype != "ignore":
                level = parsed.get("level", "info")
                msg = parsed.get("detail", parsed.get("message", parsed.get("path", "")))
                print(f"[BUILDER] [{ptype.upper()}] {msg}")

            if ptype == "phase":
                detail = parsed.get("detail", "")
                model = parsed.get("model", "")
                tg_state.build_state["phase"] = detail
                tg_state.build_state["detailed_phase"] = detail
                if model:
                    tg_state.build_state["active_model"] = model
                tg_state.build_state["current_action"] = detail
                _tg_if_verbose(f"[PHASE] *{_esc(detail)}*" + (f"\nModel: `{model}`" if model else ""))

            elif ptype == "tech":
                lang = parsed.get("language", "?")
                fw = parsed.get("framework", "")
                why = parsed.get("why", "")
                tg_state.build_state["language"] = lang
                tech_msg = f"[INFO] *Tech Stack*\nLanguage: `{lang}`"
                if fw:
                    tech_msg += f"\nFramework: `{fw}`"
                if why:
                    tech_msg += f"\n_{_esc(why[:200])}_"
                # Tech stack is always sent (essential)
                send_telegram(tech_msg)

            elif ptype == "plan":
                total = parsed.get("files_total", 0)
                files = parsed.get("files", [])
                complexity = parsed.get("complexity", "")
                tg_state.build_state["files_total"] = total
                # Reset file counter on each new plan (new subproject starts fresh)
                tg_state.build_state["files_done"] = 0
                tg_state.build_state["current_action"] = f"Planning {total} files"
                file_list = "\n".join(f"  `{f}`" for f in files[:15])
                # Plan is always sent (essential)
                send_telegram(
                    f"[INFO] *Project Plan*\n"
                    f"Files: {total} | Complexity: {complexity}\n"
                    f"{file_list}"
                )

            elif ptype == "approval_needed":
                lang = parsed.get("language", "?")
                fw = parsed.get("framework", "")
                why = parsed.get("why", "")
                ftotal = parsed.get("files_total", 0)
                flist = parsed.get("files", [])
                complexity = parsed.get("complexity", "")
                arch_decs = parsed.get("architecture_decisions", [])

                tg_state.build_state["current_action"] = "Waiting for user approval"
                tg_state.build_state["language"] = lang

                file_list_str = "\n".join(f"  `{f}`" for f in flist[:15])
                if len(flist) > 15:
                    file_list_str += f"\n  ... +{len(flist) - 15} more"

                tech_msg = "*Tech Stack Vorschlag*\n\n"
                tech_msg += f"Language   : `{lang}`\n"
                if fw:
                    tech_msg += f"Framework  : `{fw}`\n"
                tech_msg += f"Files      : {ftotal} | Complexity: {_esc(complexity)}\n"
                if why:
                    tech_msg += f"\n_{_esc(why[:300])}_\n"
                tech_msg += f"\n*Files:*\n{file_list_str}"
                if arch_decs:
                    tech_msg += "\n\n*Decisions:*\n" + "\n".join(f"  - {_esc(d)}" for d in arch_decs[:5])
                tech_msg += "\n\n*Tech Stack und Plan genehmigen?*"

                # Approval is always sent (essential)
                send_telegram(
                    tech_msg,
                    reply_markup={
                        "inline_keyboard": [[
                            {"text": "Genehmigen", "callback_data": "techapprove_yes"},
                            {"text": "Abbrechen", "callback_data": "techapprove_no"},
                        ]]
                    },
                )

            elif ptype == "file_start":
                path = parsed.get("path", "")
                tg_state.build_state["current_file"] = path
                tg_state.build_state["current_action"] = f"Writing {path}"

            elif ptype == "file_done":
                path = parsed.get("path", "")
                chars = parsed.get("chars", 0)
                attempt = parsed.get("attempt", 1)
                tg_state.build_state["files_done"] += 1
                files_written.append(path)
                done_display = min(tg_state.build_state['files_done'], tg_state.build_state['files_total'])
                progress = f"{done_display}/{tg_state.build_state['files_total']}"
                tg_state.build_state["current_action"] = f"Completed {path}"
                _tg_if_verbose(
                    f"[SUCCESS] {progress} -- `{path}`\n"
                    f"   {chars:,} chars" + (f" (attempt {attempt})" if attempt > 1 else "")
                )

            elif ptype == "error":
                msg = parsed.get("message", "")
                severity = parsed.get("severity", "error")
                file = parsed.get("file", "")
                errors_found.append(msg)
                tg_state.build_state["errors"].append(msg)
                tg_state.build_state["current_action"] = f"Error: {msg[:60]}"
                # Send critical errors always (max 5), others only in verbose
                if severity in ("fatal", "sandbox") and len(errors_found) <= 5:
                    file_info = f"\nFile: `{file}`" if file else ""
                    send_telegram(f"[ERROR] [{severity}] `{msg[:300]}`{file_info}")
                else:
                    file_info = f"\nFile: `{file}`" if file else ""
                    _tg_if_verbose(f"[ERROR] [{severity}] `{msg[:300]}`{file_info}")

            elif ptype == "repair":
                file = parsed.get("file", "")
                attempt = parsed.get("attempt", 1)
                max_a = parsed.get("max_attempts", 3)
                tg_state.build_state["current_action"] = f"Repairing {file} ({attempt}/{max_a})"
                _tg_if_verbose(f"[INFO] *Repair* `{file}` ({attempt}/{max_a})")

            elif ptype == "verify":
                tool = parsed.get("tool", "")
                success = parsed.get("success", False)
                msg = parsed.get("message", "")
                marker = "SUCCESS" if success else "ERROR"
                tg_state.build_state["current_action"] = f"{tool}: {'passed' if success else 'failed'}"
                _tg_if_verbose(f"[{marker}] *{_esc(tool)}*" + (f" -- {_esc(msg)}" if msg else ""))

            elif ptype == "timeout":
                timeout_count += 1
                file = parsed.get("file", "?")
                model = parsed.get("model", "?")
                tg_state.build_state["current_action"] = f"LLM Timeout (attempt {timeout_count})"
                # Timeouts are always sent (they indicate long waits)
                send_telegram(
                    f"[ERROR] *LLM Timeout* (#{timeout_count})\n"
                    f"File: `{file}` | Model: `{model}`\n"
                    f"_GOD MODE models can take 10-30 min per file._"
                )

            elif ptype == "polish_suggestion":
                pfile = parsed.get("file", "?")
                pwhat = parsed.get("what", "?")
                pwhy = parsed.get("why", "")
                pprio = parsed.get("priority", 1)
                tg_state.build_state["current_action"] = f"Polish: {pwhat[:40]}"
                why_str = f"\n_{_esc(pwhy[:150])}_" if pwhy else ""
                _tg_if_verbose(
                    f"[INFO] *UX Polish Vorschlag* (#{pprio})\n"
                    f"`{pfile}`\n"
                    f"{_esc(pwhat)}{why_str}"
                )

            elif ptype == "polish_applied":
                pfile = parsed.get("file", "?")
                pwhat = parsed.get("what", "?")
                pchars = parsed.get("chars", 0)
                tg_state.build_state["current_action"] = f"Applied polish: {pfile}"
                _tg_if_verbose(
                    f"[SUCCESS] *UX Polish angewendet*\n"
                    f"`{pfile}` -- {_esc(pwhat)}\n"
                    f"   {pchars:,} chars"
                )

            elif ptype == "complete":
                build_success = parsed.get("success", False)
                build_files = parsed.get("files_written", 0)
                build_elapsed = parsed.get("elapsed_sec", 0)
                build_output = parsed.get("output_dir", "")
                tg_state.build_state["current_action"] = "Build complete"
                tg_state.build_state["phase"] = "complete"
                if build_output:
                    tg_state.build_state["output_dir"] = build_output

            elif ptype == "log":
                level = parsed.get("level", "info")
                msg = parsed.get("message", "")
                if level == "warning":
                    tg_state.build_state["current_action"] = f"Warning: {msg[:60]}"

        proc.wait()
        elapsed = int(time.time() - tg_state.build_state["started_at"])
        mins, secs = divmod(elapsed, 60)

        # Detect if this was a cancelled/rejected build
        was_cancelled = (
            tg_state.build_state.get("phase", "").startswith("Abgebrochen")
            or (not files_written and tg_state.build_state.get("current_action") == "Waiting for user approval")
        )

        # Don't send a completion message if the build was already cancelled
        # via techapprove_no (the callback handler already informed the user)
        if was_cancelled:
            pass  # User already notified by techapprove_no handler
        elif proc.returncode == 0 and files_written:
            file_list = "\n".join(f"  `{_esc(f)}`" for f in files_written[-25:]) if files_written else "  --"
            err_section = ""
            if errors_found:
                err_section = (
                    f"\n\n*Warnings ({len(errors_found)}):*\n"
                    + "\n".join(f"  {_esc(e[:80])}" for e in errors_found[:5])
                )
            out_dir = tg_state.build_state.get("output_dir", "output/")
            send_telegram(
                f"*Build erfolgreich abgeschlossen!*\n\n"
                f"Duration : {mins}m {secs}s\n"
                f"Files    : {len(files_written)}\n"
                f"Language : {tg_state.build_state['language'] or '?'}\n"
                f"Output   : `{_esc(out_dir)}`\n\n"
                f"*Generated Files:*\n{file_list}{err_section}\n\n"
                f"Use /run to execute the project\n"
                f"Or open project\\_start.bat in the output folder"
            )
        else:
            err_list = (
                "\n".join(f"  `{_esc(e[:100])}`" for e in errors_found[:8]) if errors_found else "  --"
            )
            send_telegram(
                f"*Build fehlgeschlagen* (exit {proc.returncode})\n\n"
                f"Duration : {mins}m {secs}s\n"
                f"Phase    : {_esc(tg_state.build_state.get('detailed_phase', '')[:80])}\n"
                f"Files    : {tg_state.build_state['files_done']}/{tg_state.build_state['files_total']}\n\n"
                f"*Errors:*\n{err_list}\n\n"
                f"/status for system info"
            )
    except Exception as exc:
        err(f"Builder error: {exc}")
        send_telegram(f"[ERROR] *Critical error*\n`{str(exc)[:300]}`\n\nCheck Docker: `docker ps`")
    finally:
        tg_state.active_build = None
        tg_state.active_build_proc = None
        # Clean up approval signal file
        try:
            sp = getattr(tg_state, 'build_signal_path', None)
            if sp and os.path.exists(sp):
                os.remove(sp)
        except OSError:
            pass
        tg_state.build_signal_path = None
        tg_state.build_state["phase"] = "Fertig"
        tg_state.dispatcher_alive.set()
        info("Dispatcher active again")
