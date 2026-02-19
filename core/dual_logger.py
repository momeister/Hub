"""
core/dual_logger.py — Structured Build Logger
================================================
Emits JSON lines to stdout (for Docker -> gateway parsing)
and optionally prints human-readable output to the console.

Protocol: each stdout line is a JSON object with a "type" field:
  phase, tech, plan, approval_needed, file_start, file_done, error,
  repair, verify, timeout, complete, log
"""

import json
import sys
import time
from typing import Optional


class BuildLogger:
    """
    Dual-purpose logger for the builder process.

    Inside Docker (jsonl_mode=True):
        - Emits one JSON object per line to stdout
        - Gateway parses these directly via json.loads()

    On host / local dev (jsonl_mode=False):
        - Prints human-readable [TYPE] prefixed output
    """

    def __init__(self, jsonl_mode: bool = False):
        self._jsonl = jsonl_mode

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _emit(self, event: dict):
        """Emit a structured event."""
        event["ts"] = int(time.time())
        if self._jsonl:
            # JSON line — gateway parses this
            print(json.dumps(event, ensure_ascii=False), flush=True)
        else:
            # Human-readable fallback for local dev
            etype = event.get("type", "log").upper()
            detail = event.get("detail", event.get("message", ""))
            if not detail:
                # Build a readable summary from event fields
                detail = " | ".join(
                    f"{k}={v}" for k, v in event.items()
                    if k not in ("type", "ts")
                )
            marker = {
                "PHASE": "\u2501",      # ━
                "ERROR": "\u2717",      # ✗
                "LOG": "\u2022",        # •
                "FILE_DONE": "\u2713",  # ✓
                "FILE_START": "\u25B6", # ▶
                "VERIFY": "\u2713",     # ✓
                "REPAIR": "\u2699",     # ⚙
                "TIMEOUT": "\u23F1",    # ⏱
                "COMPLETE": "\u2605",   # ★
                "TECH": "\u2139",       # ℹ
                "PLAN": "\u2630",       # ☰
                "APPROVAL_NEEDED": "\u2753",  # ❓
            }.get(etype, "\u2022")
            print(f"  {marker} [{etype}] {detail}", flush=True)

    # ------------------------------------------------------------------
    # Phase Events
    # ------------------------------------------------------------------

    def phase(self, name: str, detail: str, model: str = ""):
        """Signal a major phase transition (tech_stack, planning, skeleton, fill_in, etc.)."""
        event = {"type": "phase", "name": name, "detail": detail}
        if model:
            event["model"] = model
        self._emit(event)

    # ------------------------------------------------------------------
    # Tech Stack
    # ------------------------------------------------------------------

    def tech(self, language: str, framework: str, why: str, is_multi: bool = False):
        """Report chosen tech stack."""
        self._emit({
            "type": "tech",
            "language": language,
            "framework": framework,
            "why": why,
            "is_multi_language": is_multi,
        })

    # ------------------------------------------------------------------
    # Plan
    # ------------------------------------------------------------------

    def plan(self, files_total: int, file_paths: list, complexity: str = ""):
        """Report file structure plan."""
        self._emit({
            "type": "plan",
            "files_total": files_total,
            "files": file_paths,
            "complexity": complexity,
        })

    # ------------------------------------------------------------------
    # File Progress
    # ------------------------------------------------------------------

    def file_start(self, path: str, index: int, total: int):
        """Signal start of file generation."""
        self._emit({
            "type": "file_start",
            "path": path,
            "index": index,
            "total": total,
        })

    def file_done(self, path: str, chars: int, attempt: int = 1):
        """Signal file generation complete."""
        self._emit({
            "type": "file_done",
            "path": path,
            "chars": chars,
            "attempt": attempt,
        })

    # ------------------------------------------------------------------
    # Errors and Repair
    # ------------------------------------------------------------------

    def error(self, message: str, file: str = "", severity: str = "error"):
        """Report an error."""
        event = {"type": "error", "severity": severity, "message": message}
        if file:
            event["file"] = file
        self._emit(event)

    def repair(self, file: str, attempt: int, max_attempts: int):
        """Signal a repair attempt."""
        self._emit({
            "type": "repair",
            "file": file,
            "attempt": attempt,
            "max_attempts": max_attempts,
        })

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def verify(self, success: bool, tool: str, message: str = ""):
        """Report compile/build verification result."""
        event = {"type": "verify", "success": success, "tool": tool}
        if message:
            event["message"] = message
        self._emit(event)

    def timeout(self, file: str, attempt: int, model: str):
        """Report an LLM timeout."""
        self._emit({
            "type": "timeout",
            "file": file,
            "attempt": attempt,
            "model": model,
        })

    # ------------------------------------------------------------------
    # Approval Request (pause build, wait for Telegram user approval)
    # ------------------------------------------------------------------

    def approval_needed(self, language: str, framework: str, why: str,
                        files_total: int, file_paths: list, complexity: str = "",
                        architecture_decisions: list = None):
        """Signal that the tech stack + plan needs user approval before continuing."""
        self._emit({
            "type": "approval_needed",
            "language": language,
            "framework": framework,
            "why": why,
            "files_total": files_total,
            "files": file_paths,
            "complexity": complexity,
            "architecture_decisions": architecture_decisions or [],
        })

    # ------------------------------------------------------------------
    # Human Polish Events
    # ------------------------------------------------------------------

    def polish_suggestion(self, file: str, what: str, why: str, priority: int = 1):
        """Report a UX polish suggestion."""
        self._emit({
            "type": "polish_suggestion",
            "file": file,
            "what": what,
            "why": why,
            "priority": priority,
        })

    def polish_applied(self, file: str, what: str, chars: int):
        """Report a UX polish improvement was applied."""
        self._emit({
            "type": "polish_applied",
            "file": file,
            "what": what,
            "chars": chars,
        })

    # ------------------------------------------------------------------
    # Completion
    # ------------------------------------------------------------------

    def complete(self, success: bool, files_written: int, elapsed_sec: int,
                 output_dir: str = ""):
        """Signal build completion."""
        event = {
            "type": "complete",
            "success": success,
            "files_written": files_written,
            "elapsed_sec": elapsed_sec,
        }
        if output_dir:
            event["output_dir"] = output_dir
        self._emit(event)

    # ------------------------------------------------------------------
    # General Logging
    # ------------------------------------------------------------------

    def info(self, message: str):
        """General info message."""
        self._emit({"type": "log", "level": "info", "message": message})

    def warning(self, message: str):
        """General warning message."""
        self._emit({"type": "log", "level": "warning", "message": message})

    def debug(self, message: str):
        """Debug message (only emitted in jsonl mode for gateway)."""
        if self._jsonl:
            self._emit({"type": "log", "level": "debug", "message": message})
