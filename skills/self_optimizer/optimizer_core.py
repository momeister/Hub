"""
skills/self_optimizer/optimizer_core.py -- Optimierungs-Engine
==============================================================
State Machine die den Developer -> Tester -> Reviewer Loop steuert
mit Git-basiertem Branching und Docker-isoliertem Testen.
"""

from __future__ import annotations

import enum
import json
import logging
import time
import threading
from dataclasses import dataclass, field
from typing import Optional, Callable

from core.llm_client import call as llm_call
from core.dual_logger import BuildLogger
from core.utils import clean_json_output

from skills.self_optimizer.agents.developer import DeveloperAgent
from skills.self_optimizer.agents.tester import TesterAgent
from skills.self_optimizer.agents.reviewer import ReviewerAgent, ReviewDecision
from skills.self_optimizer.agents.code_reviewer import CodeReviewerAgent
from skills.self_optimizer.git_manager import GitManager
from skills.self_optimizer.docker_sandbox import OptimizerSandbox
from skills.self_optimizer.approval import ApprovalManager
from skills.self_optimizer.config import OptimizerConfig
from skills.self_optimizer.memory_hook import record_optimization, get_optimization_context

log = logging.getLogger("ai-hub.self_optimizer")


class OptimizerState(enum.Enum):
    IDLE = "idle"
    INITIALIZING = "initializing"
    ANALYZING = "analyzing"
    PLANNING = "planning"
    DEVELOPING = "developing"
    TESTING = "testing"
    REVIEWING = "reviewing"
    AWAITING_MERGE = "awaiting_merge"
    MERGING = "merging"
    ROLLING_BACK = "rolling_back"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class OptimizationRun:
    """Zustand einer einzelnen Optimierungs-Ausfuehrung."""
    run_id: str
    mode: str = "task"
    task_description: str = ""
    iterations_target: int = 1        # 0 = unendlich
    iterations_done: int = 0
    state: OptimizerState = OptimizerState.IDLE
    consecutive_errors: int = 0
    total_errors: int = 0
    retry_count: int = 0
    current_agent: str = ""
    last_change_description: str = ""
    last_test_output: str = ""
    last_review_decision: str = ""
    last_review_reason: str = ""
    started_at: Optional[float] = None
    version_before: str = ""
    files_changed: list = field(default_factory=list)
    merge_approved: Optional[bool] = None
    stop_requested: bool = False


class OptimizationEngine:
    """
    Haupt-Engine die den Developer -> Tester -> Reviewer Loop antreibt.
    Laeuft in eigenem Thread, kommuniziert via Events und Telegram.
    """

    def __init__(
        self,
        project_dir: str,
        config: OptimizerConfig,
        blog: BuildLogger,
        notify_callback: Optional[Callable] = None,
    ):
        self.project_dir = project_dir
        self.config = config
        self.blog = blog
        self.notify = notify_callback
        self.run: Optional[OptimizationRun] = None
        self._stop_event = threading.Event()
        self._merge_event = threading.Event()

        # Sub-Komponenten
        self.git = GitManager(project_dir, blog)
        self.sandbox = OptimizerSandbox(config, blog)
        self.approval = ApprovalManager(project_dir, blog)

        # Agenten (pro Lauf mit passenden Modellen erstellt)
        self.developer: Optional[DeveloperAgent] = None
        self.tester: Optional[TesterAgent] = None
        self.reviewer: Optional[ReviewerAgent] = None
        self.code_reviewer: Optional[CodeReviewerAgent] = None

    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------

    def start(
        self,
        mode: str,
        task: str,
        iterations: int,
        reasoning_model: str,
        coding_model: str,
    ) -> OptimizationRun:
        """Optimierungs-Lauf starten. Gibt Run-Objekt zum Tracking zurueck."""
        run_id = f"opt_{int(time.time())}"
        self.run = OptimizationRun(
            run_id=run_id,
            mode=mode,
            task_description=task,
            iterations_target=iterations,
            started_at=time.time(),
        )
        self._stop_event.clear()
        self._merge_event.clear()

        # Agenten erstellen
        self.developer = DeveloperAgent(
            project_dir=self.project_dir,
            coding_model=coding_model,
            reasoning_model=reasoning_model,
            config=self.config,
            blog=self.blog,
        )
        self.tester = TesterAgent(
            project_dir=self.project_dir,
            sandbox=self.sandbox,
            config=self.config,
            blog=self.blog,
        )
        self.reviewer = ReviewerAgent(
            project_dir=self.project_dir,
            reasoning_model=reasoning_model,
            config=self.config,
            blog=self.blog,
        )
        self.code_reviewer = CodeReviewerAgent(
            project_dir=self.project_dir,
            reasoning_model=reasoning_model,
            config=self.config,
            blog=self.blog,
        )

        return self.run

    def stop(self) -> None:
        """Loop nach aktuellem Agenten stoppen."""
        if self.run:
            self.run.stop_requested = True
        self._stop_event.set()
        self._merge_event.set()

    def approve_merge(self) -> None:
        """Vom Telegram-Callback aufgerufen: User genehmigt Merge."""
        if self.run:
            self.run.merge_approved = True
        self._merge_event.set()

    def reject_merge(self) -> None:
        """Vom Telegram-Callback aufgerufen: User lehnt Merge ab."""
        if self.run:
            self.run.merge_approved = False
        self._merge_event.set()

    # ------------------------------------------------------------------
    # Haupt-Loop
    # ------------------------------------------------------------------

    def execute_loop(self) -> None:
        """Haupt-Optimierungs-Loop. Laeuft in Background-Thread."""
        run = self.run
        if not run:
            return

        try:
            self._transition(OptimizerState.INITIALIZING)

            # Git initialisieren
            self.git.ensure_initialized()
            self.git.ensure_on_experimental()

            # Docker-Image pruefen
            self.sandbox.ensure_image()

            while not self._should_stop():
                # Hard Cap pruefen
                if run.iterations_done >= self.config.max_iterations:
                    self.blog.info(
                        f"Hard Cap erreicht ({self.config.max_iterations} Iterationen)"
                    )
                    break

                run.iterations_done += 1
                run.merge_approved = None  # Reset fuer diese Iteration
                self.blog.info(
                    f"=== Optimierungs-Iteration {run.iterations_done} ==="
                )
                self._update_telegram_state()

                # PHASE 1: Analyse / Planung
                if run.mode == "auto":
                    self._transition(OptimizerState.ANALYZING)
                    run.current_agent = "analyzer"
                    self._update_telegram_state()
                    task = self._analyze_codebase()
                    if not task:
                        self.blog.info("Keine Verbesserungen gefunden. Stoppe.")
                        self._notify(
                            "*Optimizer:* Keine weiteren Verbesserungen gefunden."
                        )
                        break
                    run.task_description = task
                else:
                    task = run.task_description

                self._transition(OptimizerState.PLANNING)
                run.current_agent = "developer"
                self._update_telegram_state()
                plan = self.developer.plan(task)
                run.last_change_description = plan.get("description", task)

                self._notify(
                    f"*Optimizer -- Iteration {run.iterations_done}*\n\n"
                    f"Versuche: _{run.last_change_description[:300]}_"
                )

                if not plan.get("files_to_modify"):
                    self.blog.warning("Keine Dateien zum Modifizieren im Plan")
                    continue

                if self._should_stop():
                    break

                # PHASE 2: Entwicklung
                self._transition(OptimizerState.DEVELOPING)
                self._update_telegram_state()
                changes = self.developer.develop(plan)
                run.files_changed = [
                    f.get("path", "") for f in changes.get("files", [])
                ]

                # Auf experimental committen
                self.git.commit_changes(
                    message=f"[optimizer] {run.last_change_description[:72]}",
                    files=changes.get("files", []),
                )

                if self._should_stop():
                    break

                # PHASE 3: Testen
                self._transition(OptimizerState.TESTING)
                run.current_agent = "tester"
                self._update_telegram_state()
                test_result = self.tester.test(changes)
                run.last_test_output = test_result.get("output", "")

                if self._should_stop():
                    break

                # PHASE 3b: Code-Review (inhaltliche Qualitätsprüfung)
                if not self._should_stop():
                    code_review = self.code_reviewer.review_code(
                        task=task,
                        changes=changes,
                    )
                    code_review_passed = code_review.get("passed", True)
                    code_review_score = code_review.get("quality_score", 5)
                    code_review_summary = code_review.get("summary", "")

                    self._notify(
                        f"*Optimizer — Code-Review*\n\n"
                        f"Score: {code_review_score}/10\n"
                        f"_{code_review_summary[:300]}_"
                    )

                    # Bei kritischen Issues direkt RETRY ohne den finalen Reviewer zu belasten
                    if not code_review_passed:
                        run.consecutive_errors += 1
                        run.total_errors += 1
                        issues_text = "\n".join(
                            f"- [{i.get('severity', '')}] {i.get('file', '')}: {i.get('description', '')}"
                            for i in code_review.get("issues", [])
                            if i.get("severity") == "critical"
                        )
                        self._transition(OptimizerState.ROLLING_BACK)
                        self.git.rollback_experimental()
                        run.task_description = (
                            f"{task}\n\n[CODE-REVIEW FEHLGESCHLAGEN]\n"
                            f"Kritische Issues:\n{issues_text}\n"
                            f"Cross-File: {', '.join(code_review.get('cross_file_issues', []))}"
                        )
                        continue  # Retry mit Feedback

                    # Code-Review-Ergebnis dem finalen Reviewer mitgeben
                    test_result["code_review_score"] = code_review_score
                    test_result["code_review_summary"] = code_review_summary

                # PHASE 4: Review
                self._transition(OptimizerState.REVIEWING)
                run.current_agent = "reviewer"
                self._update_telegram_state()
                review = self.reviewer.review(
                    task=task,
                    changes=changes,
                    test_result=test_result,
                )
                decision = review["decision"]
                run.last_review_decision = decision.value
                run.last_review_reason = review.get("reason", "")
                self._update_telegram_state()

                # -- Entscheidung verarbeiten --

                if decision == ReviewDecision.MERGE:
                    run.consecutive_errors = 0

                    # Download/Internet-Anfragen pruefen
                    pending = self.approval.check_pending_requests()
                    if pending:
                        self._notify_approval_needed(pending)
                        approved = self.approval.wait_for_approval(
                            timeout=self.config.approval_timeout,
                        )
                        if not approved:
                            self.blog.info("Genehmigung abgelehnt oder Timeout")
                            self._transition(OptimizerState.ROLLING_BACK)
                            self.git.rollback_experimental()
                            continue

                    # Merge-Genehmigung via Telegram anfordern
                    self._transition(OptimizerState.AWAITING_MERGE)
                    self._update_telegram_state()

                    diff_summary = self.git.get_diff_summary()
                    self._notify_merge_request(run, diff_summary, review)

                    self._merge_event.clear()
                    self._merge_event.wait(
                        timeout=self.config.merge_approval_timeout
                    )

                    if run.merge_approved is True:
                        self._transition(OptimizerState.MERGING)
                        self._update_telegram_state()
                        version = self.git.merge_to_stable(
                            tag_prefix="v",
                            message=run.last_change_description,
                        )
                        run.version_before = version

                        # Optimierung in Memory schreiben
                        record_optimization(
                            files_changed=run.files_changed,
                            description=run.last_change_description,
                            tests_passed=test_result.get("success", False),
                            decision="merge",
                            quality_score=review.get("quality_score", 0),
                        )

                        self._notify(
                            f"*Optimizer: Gemergt!*\n\n"
                            f"Version: `{version}`\n"
                            f"Aenderung: _{run.last_change_description[:200]}_"
                        )
                    elif run.merge_approved is False:
                        self._transition(OptimizerState.ROLLING_BACK)
                        self.git.rollback_experimental()
                        self._notify("*Optimizer:* Merge abgelehnt, Rollback.")
                    else:
                        # Timeout
                        self.blog.warning("Merge-Genehmigung Timeout")
                        self._transition(OptimizerState.ROLLING_BACK)
                        self.git.rollback_experimental()
                        self._notify(
                            "*Optimizer:* Merge-Timeout, automatischer Rollback."
                        )

                elif decision == ReviewDecision.RETRY:
                    run.consecutive_errors += 1
                    run.total_errors += 1
                    run.retry_count += 1

                    if run.consecutive_errors >= self.config.max_consecutive_errors:
                        self.blog.error(
                            f"Max aufeinanderfolgende Fehler "
                            f"({self.config.max_consecutive_errors}) erreicht",
                            severity="abort",
                        )
                        self._transition(OptimizerState.ROLLING_BACK)
                        self.git.rollback_experimental()
                        self._notify(
                            f"*Optimizer:* Max Fehler erreicht "
                            f"({self.config.max_consecutive_errors}). Gestoppt.\n"
                            f"Grund: _{run.last_review_reason[:200]}_"
                        )
                        break

                    self.blog.info(
                        f"Retry ({run.retry_count}) mit Feedback: "
                        f"{run.last_review_reason[:100]}"
                    )
                    self._transition(OptimizerState.ROLLING_BACK)
                    self.git.rollback_experimental()

                    # Fehler-Feedback in Aufgabe einfuegen
                    run.task_description = (
                        f"{task}\n\n[VORHERIGER VERSUCH FEHLGESCHLAGEN]\n"
                        f"Grund: {run.last_review_reason}\n"
                        f"Test-Output: {run.last_test_output[:1000]}"
                    )
                    continue  # Gleiche Iteration erneut

                elif decision == ReviewDecision.REJECT:
                    run.consecutive_errors += 1
                    run.total_errors += 1
                    self._transition(OptimizerState.ROLLING_BACK)
                    self.git.rollback_experimental()
                    self._notify(
                        f"*Optimizer:* Aenderung abgelehnt.\n"
                        f"Grund: _{run.last_review_reason[:200]}_"
                    )

                # Iterations-Limit pruefen
                if (
                    run.iterations_target > 0
                    and run.iterations_done >= run.iterations_target
                ):
                    self.blog.info(
                        f"Ziel-Iterationen erreicht ({run.iterations_target})"
                    )
                    break

            self._transition(OptimizerState.STOPPED)
            self._emit_complete(run)

        except Exception as exc:
            self.blog.error(
                f"Optimizer fataler Fehler: {exc}", severity="fatal"
            )
            self._transition(OptimizerState.ERROR)
            try:
                self.git.rollback_experimental()
            except Exception:
                pass
            self._notify(f"*Optimizer FEHLER:* `{str(exc)[:300]}`")
            raise
        finally:
            try:
                self.git.ensure_on_stable()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Analyse (Auto-Modus)
    # ------------------------------------------------------------------

    def _analyze_codebase(self) -> Optional[str]:
        """Reasoning-Modell analysiert Codebase und schlaegt Verbesserung vor."""
        analysis_context = self.developer.build_codebase_summary()

        # Bisherige Optimierungen als Kontext laden
        opt_history = get_optimization_context()
        history_block = ""
        if opt_history:
            history_block = f"\n\nPREVIOUS OPTIMIZATIONS (do NOT repeat these):\n{opt_history}\n"

        prompt = f"""You are a senior software architect analyzing a codebase for improvements.

CODEBASE SUMMARY:
{analysis_context}
{history_block}
Identify the single highest-impact improvement that can be made.
Consider: bug fixes, performance issues, missing error handling,
code quality, missing features, security issues, new useful experiments.

IMPORTANT: Do NOT suggest changes to files in skills/self_optimizer/ (that's the optimizer itself).
IMPORTANT: Do NOT suggest changes that have already been done (see previous optimizations above).

Choose ONE of these categories:
1. IMPROVE existing code (bugfix, refactor, performance, security)
2. NEW FEATURE for the existing project
3. NEW EXPERIMENT (a standalone script or mini-project in a new directory)

Output ONLY this JSON:
{{
  "task": "<1-2 sentence description of what to implement/fix>",
  "category": "bugfix|feature|refactor|performance|security|experiment",
  "files_affected": ["file1.py", "file2.py"],
  "priority": 1-5,
  "rationale": "<why this matters>"
}}

If the codebase looks solid and no improvements are needed, output:
{{"task": null}}"""

        response = self.developer.llm(
            model=self.developer.reasoning_model,
            prompt=prompt,
            system="Expert code analyst. Output only valid JSON.",
            max_tokens=2048,
            temperature=0.3,
        )

        try:
            parsed = json.loads(clean_json_output(response))
            task = parsed.get("task")
            if task:
                category = parsed.get("category", "?")
                rationale = parsed.get("rationale", "")
                self.blog.info(f"Auto-Aufgabe: [{category}] {task}")
                self._notify(
                    f"*Optimizer Auto-Analyse:*\n"
                    f"Kategorie: `{category}`\n"
                    f"Aufgabe: _{task[:200]}_\n"
                    f"Grund: _{rationale[:200]}_"
                )
                return task
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Interne Hilfsfunktionen
    # ------------------------------------------------------------------

    def _should_stop(self) -> bool:
        return self._stop_event.is_set() or (
            self.run and self.run.stop_requested
        )

    def _transition(self, new_state: OptimizerState) -> None:
        if self.run:
            old = self.run.state
            self.run.state = new_state
            self.blog.phase(
                f"optimizer_{new_state.value}",
                f"Optimizer: {old.value} -> {new_state.value}",
            )

    def _update_telegram_state(self) -> None:
        """Telegram-State-Dict aktualisieren."""
        from core.telegram import state as tg_state

        if not self.run:
            return
        tg_state.optimizer_state.update({
            "state": self.run.state.value,
            "mode": self.run.mode,
            "task": self.run.task_description[:200],
            "iteration": self.run.iterations_done,
            "iterations_target": self.run.iterations_target,
            "current_agent": self.run.current_agent,
            "last_change": self.run.last_change_description[:200],
            "last_decision": self.run.last_review_decision,
            "errors": self.run.total_errors,
            "started_at": self.run.started_at,
            "version": self.run.version_before,
        })

    def _notify(self, message: str) -> None:
        """Telegram-Nachricht senden."""
        if self.notify:
            try:
                self.notify(message)
            except Exception:
                pass

    def _notify_merge_request(
        self, run: OptimizationRun, diff_summary: str, review: dict,
    ) -> None:
        """Merge-Request via Telegram mit Approve/Reject Buttons senden."""
        from core.utils import send_telegram

        score = review.get("quality_score", 0)
        reason = review.get("reason", "")
        suggestions = review.get("suggestions", [])
        sugg_text = "\n".join(f"  - {s}" for s in suggestions[:3])

        message = (
            f"*Optimizer: Merge-Anfrage*\n\n"
            f"Aenderung: _{run.last_change_description[:200]}_\n"
            f"Qualitaet: {score}/10\n"
            f"Reviewer: _{reason[:200]}_\n"
        )
        if sugg_text:
            message += f"\nVorschlaege:\n{sugg_text}\n"
        if diff_summary:
            message += f"\nDateien:\n```\n{diff_summary[:500]}\n```"

        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "Merge genehmigen", "callback_data": "optmerge_yes"},
                    {"text": "Ablehnen", "callback_data": "optmerge_no"},
                ]
            ]
        }
        send_telegram(message, reply_markup=reply_markup)

    def _notify_approval_needed(self, pending: list[dict]) -> None:
        """Download/Internet-Genehmigung via Telegram anfordern."""
        from core.utils import send_telegram

        for req in pending:
            content = req.get("content", "")[:800]
            rtype = req["type"]
            label = "Download" if rtype == "download" else "Internet-Zugriff"

            message = (
                f"*Optimizer: {label}-Anfrage*\n\n"
                f"```\n{content}\n```\n\n"
                f"Genehmigen?"
            )
            reply_markup = {
                "inline_keyboard": [
                    [
                        {"text": "Genehmigen", "callback_data": "optapproval_yes"},
                        {"text": "Ablehnen", "callback_data": "optapproval_no"},
                    ]
                ]
            }
            send_telegram(message, reply_markup=reply_markup)

    def _emit_complete(self, run: OptimizationRun) -> None:
        """Abschluss-Nachricht senden."""
        elapsed = int(time.time() - (run.started_at or time.time()))
        mins, secs = divmod(elapsed, 60)

        self._notify(
            f"*Optimizer abgeschlossen*\n\n"
            f"Iterationen: {run.iterations_done}\n"
            f"Fehler: {run.total_errors}\n"
            f"Dauer: {mins}m {secs}s\n"
            f"Version: `{run.version_before or 'keine Aenderung'}`"
        )

        self.blog.complete(
            success=run.total_errors == 0,
            files_written=run.iterations_done,
            elapsed_sec=elapsed,
        )

    # ------------------------------------------------------------------
    # History & Suggest (Memory-basiert)
    # ------------------------------------------------------------------

    def get_optimization_history(self) -> list[dict]:
        """Bisherige Optimierungen aus MEMORY.md lesen."""
        try:
            from core.memory import get_memory
            memory = get_memory()
            return memory.get_optimization_history()
        except Exception:
            return []

    def suggest_next_target(self) -> str:
        """
        Basierend auf der History den naechsten sinnvollen
        Optimierungskandidaten vorschlagen.
        """
        history = self.get_optimization_history()

        # Dateien sammeln die bereits optimiert wurden
        optimized_files = set()
        failed_files = set()
        for entry in history:
            f = entry.get("file", "")
            if entry.get("tests_passed"):
                optimized_files.add(f)
            else:
                failed_files.add(f)

        # Codebase-Dateien lesen
        try:
            all_files = self.developer.get_project_files() if self.developer else []
        except Exception:
            all_files = []

        # Dateien die noch nicht optimiert wurden bevorzugen
        candidates = [f for f in all_files if f not in optimized_files]
        # Fehlgeschlagene mit neuem Ansatz versuchen
        retry_candidates = [f for f in failed_files if f in all_files]

        if candidates:
            suggestion = f"Optimize {candidates[0]} — not yet analyzed"
        elif retry_candidates:
            suggestion = f"Retry optimization of {retry_candidates[0]} with different approach"
        else:
            suggestion = "All known files already optimized. Run auto-mode for deeper analysis."

        return suggestion
