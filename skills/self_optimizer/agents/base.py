"""
skills/self_optimizer/agents/base.py -- Basis-Agent-Klasse
===========================================================
Gemeinsame Funktionen: Timeout, Fehlerbehandlung, LLM-Wrapper,
Dateisystem-Zugriff und strukturiertes Logging.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from core.llm_client import call as llm_call
from core.dual_logger import BuildLogger
from skills.self_optimizer.config import OptimizerConfig

log = logging.getLogger("ai-hub.optimizer.agent")


class AgentTimeout(Exception):
    """Wird geworfen wenn ein Agent sein Zeitlimit ueberschreitet."""
    pass


class BaseAgent:
    """
    Basisklasse fuer alle Optimizer-Agenten.

    Bietet:
    - Cancellation via threading.Event
    - LLM-Aufruf-Wrapper mit Cancel-Check
    - Projekt-Dateisystem-Zugriff (read, list)
    - Strukturiertes Logging via BuildLogger
    """

    AGENT_NAME: str = "base"

    def __init__(
        self,
        project_dir: str,
        config: OptimizerConfig,
        blog: BuildLogger,
    ):
        self.project_dir = project_dir
        self.config = config
        self.blog = blog
        self._cancel_event = threading.Event()

    def llm(
        self,
        model: str,
        prompt: str,
        system: str = "You are a helpful assistant.",
        max_tokens: int = 8192,
        temperature: float = 0.1,
    ) -> str:
        """LLM-Aufruf mit Cancel-Check."""
        if self._cancel_event.is_set():
            raise AgentTimeout(f"{self.AGENT_NAME} wurde abgebrochen")
        return llm_call(
            model=model,
            prompt=prompt,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    def read_project_file(self, relative_path: str) -> str:
        """Datei aus dem Projektverzeichnis lesen."""
        full_path = os.path.join(self.project_dir, relative_path)
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except (OSError, IOError):
            return ""

    def list_project_files(
        self,
        extensions: tuple = (".py", ".json", ".yaml", ".yml", ".toml"),
    ) -> list[str]:
        """Alle relevanten Projektdateien auflisten."""
        result = []
        exclude_dirs = {
            ".git", "__pycache__", "node_modules", ".venv", "venv", "output",
        }
        for root, dirs, files in os.walk(self.project_dir):
            dirs[:] = [
                d for d in dirs
                if d not in exclude_dirs and not d.startswith("tmpclaude")
            ]
            for f in files:
                if f.startswith("tmpclaude"):
                    continue
                if any(f.endswith(ext) for ext in extensions):
                    rel = os.path.relpath(os.path.join(root, f), self.project_dir)
                    result.append(rel.replace("\\", "/"))
        return sorted(result)

    def is_protected(self, path: str) -> bool:
        """Pruefen ob Pfad geschuetzt ist (nicht modifiziert werden darf)."""
        normalized = path.replace("\\", "/")
        for protected in self.config.protected_paths:
            if normalized.startswith(protected) or normalized == protected:
                return True
        return False

    def cancel(self) -> None:
        """Agent abbrechen."""
        self._cancel_event.set()
