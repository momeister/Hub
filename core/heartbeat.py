"""
core/heartbeat.py — Proaktives Heartbeat/Cron-System
=====================================================
HeartbeatScheduler auf Basis von APScheduler.
Stellt Health-Checks, Session-Summaries und Temp-Cleanup bereit.
Skills koennen eigene Cron-Jobs registrieren.
"""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import shutil
import time
from datetime import datetime
from typing import Callable, Optional

from core.config import ROOT_DIR
from core.utils import send_telegram, info, warn, err

log = logging.getLogger("ai-hub.heartbeat")


class HeartbeatScheduler:
    """
    Cron-basierter Scheduler fuer wiederkehrende Aufgaben.
    Nutzt APScheduler AsyncIOScheduler.
    """

    def __init__(self):
        self._scheduler = None
        self._running = False
        self._custom_jobs: list[dict] = []

    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Scheduler starten und Built-in Jobs registrieren."""
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.cron import CronTrigger
            from apscheduler.triggers.interval import IntervalTrigger
        except ImportError:
            warn("APScheduler nicht installiert — Heartbeat deaktiviert")
            warn("Installiere mit: pip install apscheduler>=3.10")
            return

        self._scheduler = AsyncIOScheduler(timezone="Europe/Berlin")

        # Built-in Job 1: Taegliche Session-Zusammenfassung um 09:00
        self._scheduler.add_job(
            self._daily_summary,
            CronTrigger(hour=9, minute=0),
            id="daily_summary",
            name="Taegliche Zusammenfassung",
            replace_existing=True,
        )

        # Built-in Job 2: Alle 30 Minuten Health-Check
        self._scheduler.add_job(
            self._health_check,
            IntervalTrigger(minutes=30),
            id="health_check",
            name="Service Health Check",
            replace_existing=True,
        )

        # Built-in Job 3: Jede Stunde Temp-Cleanup
        self._scheduler.add_job(
            self._temp_cleanup,
            IntervalTrigger(hours=1),
            id="temp_cleanup",
            name="Temp Cleanup",
            replace_existing=True,
        )

        # Skill-Cron-Jobs laden
        self._load_skill_cron_jobs()

        self._scheduler.start()
        self._running = True
        info("HeartbeatScheduler gestartet (3 Built-in Jobs)")

    async def stop(self) -> None:
        """Scheduler sauber herunterfahren."""
        if self._scheduler and self._running:
            self._scheduler.shutdown(wait=False)
            self._running = False
            info("HeartbeatScheduler gestoppt")

    def register_cron(
        self,
        skill_name: str,
        cron_expr: str,
        callback: Callable,
    ) -> None:
        """
        Skill-spezifischen Cron-Job registrieren.

        Args:
            skill_name: Name des Skills
            cron_expr: Cron-Ausdruck (z.B. '0 3 * * *')
            callback: Async oder sync Callable
        """
        if not self._scheduler:
            warn(f"Scheduler nicht aktiv — Job '{skill_name}' nicht registriert")
            return

        try:
            from apscheduler.triggers.cron import CronTrigger

            parts = cron_expr.strip().split()
            if len(parts) != 5:
                warn(f"Ungueltiger Cron-Ausdruck fuer {skill_name}: {cron_expr}")
                return

            trigger = CronTrigger(
                minute=parts[0],
                hour=parts[1],
                day=parts[2],
                month=parts[3],
                day_of_week=parts[4],
            )

            job_id = f"skill_{skill_name}"
            self._scheduler.add_job(
                callback,
                trigger,
                id=job_id,
                name=f"Skill: {skill_name}",
                replace_existing=True,
            )
            info(f"Cron-Job registriert: {skill_name} ({cron_expr})")

        except Exception as exc:
            err(f"Cron-Job Registrierung fehlgeschlagen fuer {skill_name}: {exc}")

    # ------------------------------------------------------------------
    # Skill Cron-Jobs aus skill.json laden
    # ------------------------------------------------------------------

    def _load_skill_cron_jobs(self) -> None:
        """Cron-Jobs aus skill.json Dateien laden."""
        import json

        skills_dir = ROOT_DIR / "skills"
        if not skills_dir.exists():
            return

        for skill_json in skills_dir.rglob("skill.json"):
            try:
                with open(skill_json, "r", encoding="utf-8") as f:
                    manifest = json.load(f)

                cron_jobs = manifest.get("cron_jobs", [])
                skill_name = manifest.get("name", skill_json.parent.name)

                for job_def in cron_jobs:
                    job_name = job_def.get("name", "unknown")
                    cron_expr = job_def.get("cron", "")
                    action = job_def.get("action", "")

                    if not cron_expr or not action:
                        continue

                    # Callback erstellt das Skill-Modul und ruft die Action auf
                    self.register_cron(
                        skill_name=f"{skill_name}_{job_name}",
                        cron_expr=cron_expr,
                        callback=self._make_skill_callback(skill_name, action),
                    )

            except Exception as exc:
                log.debug(f"Skill cron load error ({skill_json}): {exc}")

    def _make_skill_callback(self, skill_name: str, action: str) -> Callable:
        """Factory fuer Skill-Cron-Callbacks."""
        async def _callback():
            try:
                import importlib
                mod = importlib.import_module(f"skills.{skill_name}.skill")
                fn = getattr(mod, action, None)
                if fn:
                    if asyncio.iscoroutinefunction(fn):
                        await fn()
                    else:
                        fn()
                    log.info(f"Skill-Cron ausgefuehrt: {skill_name}.{action}")
                else:
                    log.warning(f"Action '{action}' nicht in skills.{skill_name}.skill")
            except Exception as exc:
                log.warning(f"Skill-Cron Fehler ({skill_name}.{action}): {exc}")

        return _callback

    # ------------------------------------------------------------------
    # Built-in Jobs
    # ------------------------------------------------------------------

    async def _daily_summary(self) -> None:
        """Taegliche Zusammenfassung der gestrigen Sessions via Telegram."""
        try:
            from core.memory import get_memory
            memory = get_memory()

            yesterday = datetime.now().strftime("%Y-%m-%d")
            recent_sessions = [
                s for s in memory._sessions
                if s.get("date", "").startswith(yesterday)
            ]

            if not recent_sessions:
                return  # Keine Sessions gestern → keine Nachricht

            lines = [f"*Taegliche Zusammenfassung ({yesterday})*\n"]
            for s in recent_sessions[-5:]:
                lines.append(f"- {s.get('summary', '?')[:200]}")

            recent_projects = [
                p for p in memory._projects
                if p.get("date", "").startswith(yesterday)
            ]
            if recent_projects:
                lines.append(f"\n*Projekte:*")
                for p in recent_projects:
                    lines.append(
                        f"- {p['name']} ({p.get('language', '?')}): "
                        f"{p.get('summary', '')[:100]}"
                    )

            send_telegram("\n".join(lines))
            log.info("Taegliche Zusammenfassung gesendet")

        except Exception as exc:
            log.warning(f"Taegliche Zusammenfassung fehlgeschlagen: {exc}")

    async def _health_check(self) -> None:
        """Ollama und ComfyUI Health-Check, Warnung bei Ausfall."""
        try:
            from core.services_status import check_ollama, check_comfyui
            from core.config import get_service_config

            services = get_service_config()
            warnings = []

            if not check_ollama(services.ollama_base_url):
                warnings.append("Ollama ist nicht erreichbar!")

            if not check_comfyui(services.comfyui_url):
                # ComfyUI ist optional — nur warnen wenn es vorher lief
                log.debug("ComfyUI nicht erreichbar (optional)")

            if warnings:
                send_telegram(
                    "*Service-Warnung*\n\n" + "\n".join(f"- {w}" for w in warnings)
                )
                log.warning(f"Health-Check Warnungen: {warnings}")

        except Exception as exc:
            log.warning(f"Health-Check fehlgeschlagen: {exc}")

    async def _temp_cleanup(self) -> None:
        """tmpclaude-* Verzeichnisse bereinigen die aelter als 1 Stunde sind."""
        try:
            root = str(ROOT_DIR)
            now = time.time()
            max_age = 3600  # 1 Stunde

            cleaned = 0
            for entry in os.listdir(root):
                if not entry.startswith("tmpclaude"):
                    continue

                full_path = os.path.join(root, entry)
                if not os.path.isdir(full_path):
                    continue

                try:
                    mtime = os.path.getmtime(full_path)
                    if (now - mtime) > max_age:
                        shutil.rmtree(full_path, ignore_errors=True)
                        cleaned += 1
                except OSError:
                    continue

            if cleaned > 0:
                log.info(f"Temp-Cleanup: {cleaned} tmpclaude-Verzeichnis(se) entfernt")

        except Exception as exc:
            log.warning(f"Temp-Cleanup fehlgeschlagen: {exc}")

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        """Scheduler-Status zurueckgeben."""
        if not self._scheduler or not self._running:
            return {"active": False, "jobs": []}

        jobs = []
        for job in self._scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run": str(job.next_run_time) if job.next_run_time else "?",
            })

        return {"active": True, "jobs": jobs}


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: Optional[HeartbeatScheduler] = None


def get_heartbeat() -> HeartbeatScheduler:
    """Globale HeartbeatScheduler-Instanz (Singleton)."""
    global _instance
    if _instance is None:
        _instance = HeartbeatScheduler()
    return _instance
