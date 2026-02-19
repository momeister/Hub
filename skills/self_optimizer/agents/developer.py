"""
skills/self_optimizer/agents/developer.py -- Developer Agent
=============================================================
Analysiert die Codebase, plant Aenderungen, generiert Code.
Nutzt Reasoning-Modell fuer Analyse/Planung, Coding-Modell fuer Implementierung.
"""

from __future__ import annotations

import json
import logging
import os
import re

from core.utils import clean_json_output, strip_code_fences, write_file
from core.dual_logger import BuildLogger
from skills.self_optimizer.agents.base import BaseAgent
from skills.self_optimizer.config import OptimizerConfig

log = logging.getLogger("ai-hub.optimizer.developer")


class DeveloperAgent(BaseAgent):
    AGENT_NAME = "developer"

    def __init__(
        self,
        project_dir: str,
        coding_model: str,
        reasoning_model: str,
        config: OptimizerConfig,
        blog: BuildLogger,
    ):
        super().__init__(project_dir, config, blog)
        self.coding_model = coding_model
        self.reasoning_model = reasoning_model

    # ------------------------------------------------------------------
    # Codebase-Analyse
    # ------------------------------------------------------------------

    def build_codebase_summary(self) -> str:
        """Zusammenfassung der Codebase fuer KI-Analyse erstellen."""
        files = self.list_project_files()
        summaries = []
        total_chars = 0
        max_chars = 50000

        for fpath in files:
            if total_chars > max_chars:
                summaries.append(
                    f"\n... und {len(files) - len(summaries)} weitere Dateien"
                )
                break
            content = self.read_project_file(fpath)
            preview = content[:500] if len(content) > 500 else content
            summaries.append(f"=== {fpath} ({len(content)} chars) ===\n{preview}")
            total_chars += len(preview)

        return "\n\n".join(summaries)

    # ------------------------------------------------------------------
    # Planung
    # ------------------------------------------------------------------

    def plan(self, task: str) -> dict:
        """
        Code-Aenderungen fuer eine Aufgabe planen.

        Returns:
        {
            "description": str,
            "files_to_modify": [
                {"path": str, "action": "create|modify|delete", "purpose": str}
            ],
            "approach": str,
            "risks": [str],
        }
        """
        self.blog.phase(
            "developer_plan",
            f"Developer plant: {task[:80]}",
            model=self.reasoning_model,
        )

        files = self.list_project_files()
        file_list = "\n".join(f"  {f}" for f in files[:60])

        # Schluessel-Dateien fuer Kontext lesen
        key_files_content = ""
        key_files = [
            "main.py", "core/config.py", "core/dispatcher.py",
            "requirements.txt",
        ]
        for kf in key_files:
            content = self.read_project_file(kf)
            if content:
                key_files_content += f"\n=== {kf} ===\n{content[:2000]}\n"

        prompt = f"""You are a senior developer planning code changes for this project.

TASK: {task}

PROJECT FILES:
{file_list}

KEY FILE CONTENTS:
{key_files_content}

PROTECTED FILES (do NOT modify):
  skills/self_optimizer/** (self-optimizer code)
  .env (secrets)
  .git/** (version control)

Plan the implementation. Output ONLY this JSON:
{{
  "description": "<1-2 sentence summary of the change>",
  "files_to_modify": [
    {{"path": "<relative path>", "action": "create|modify|delete", "purpose": "<why>"}}
  ],
  "approach": "<implementation strategy in 2-3 sentences>",
  "risks": ["<potential risk 1>", "<potential risk 2>"]
}}"""

        response = self.llm(
            model=self.reasoning_model,
            prompt=prompt,
            system="Expert software architect. Output only valid JSON.",
            max_tokens=4096,
            temperature=0.1,
        )

        try:
            plan = json.loads(clean_json_output(response))
            # Geschuetzte Dateien herausfiltern
            plan["files_to_modify"] = [
                f for f in plan.get("files_to_modify", [])
                if not self.is_protected(f.get("path", ""))
            ]
            self.blog.info(
                f"Plan: {plan.get('description', '?')}, "
                f"{len(plan.get('files_to_modify', []))} Dateien"
            )
            return plan
        except (json.JSONDecodeError, ValueError) as exc:
            self.blog.warning(f"Plan-Parsing fehlgeschlagen: {exc}")
            return {
                "description": task,
                "files_to_modify": [],
                "approach": "Direkte Implementierung",
                "risks": [],
            }

    # ------------------------------------------------------------------
    # Implementierung
    # ------------------------------------------------------------------

    def develop(self, plan: dict) -> dict:
        """
        Plan ausfuehren: Code-Dateien generieren/modifizieren.

        Returns:
        {
            "files": [{"path": str, "action": str, "content": str}],
            "description": str,
        }
        """
        self.blog.phase(
            "developer_code",
            f"Developer implementiert: {plan.get('description', '?')[:60]}",
            model=self.coding_model,
        )

        changes = []
        files_to_modify = plan.get("files_to_modify", [])

        for file_spec in files_to_modify:
            path = file_spec["path"]
            action = file_spec.get("action", "modify")
            purpose = file_spec.get("purpose", "")

            # Geschuetzte Dateien ueberspringen
            if self.is_protected(path):
                self.blog.warning(f"Ueberspringe geschuetzte Datei: {path}")
                continue

            if action == "delete":
                changes.append({"path": path, "action": "delete", "content": ""})
                continue

            # Bestehenden Inhalt lesen falls Modifikation
            existing = ""
            if action == "modify":
                existing = self.read_project_file(path)

            # Kontext aus verwandten Dateien zusammentragen
            related_context = self._gather_related_context(path, plan)

            if action == "modify" and existing:
                prompt = f"""Modify this file to implement the required change.

TASK: {plan.get('description', '')}
PURPOSE FOR THIS FILE: {purpose}
APPROACH: {plan.get('approach', '')}

CURRENT FILE ({path}):
{existing}

RELATED FILES:
{related_context}

RULES:
1. Output the COMPLETE modified file
2. Do NOT break existing functionality
3. Follow the existing code style exactly
4. No new external dependencies without noting them
5. NEVER include API keys, tokens, or secrets
6. Output ONLY code, no markdown fences, no explanation"""
            else:
                prompt = f"""Create a new file for this project.

TASK: {plan.get('description', '')}
FILE PATH: {path}
PURPOSE: {purpose}
APPROACH: {plan.get('approach', '')}

RELATED FILES:
{related_context}

RULES:
1. Follow the existing project's code style
2. Include proper imports
3. Include docstrings
4. NEVER include API keys, tokens, or secrets
5. Output ONLY code, no markdown fences, no explanation"""

            code = self.llm(
                model=self.coding_model,
                prompt=prompt,
                system="Expert Python developer. Output ONLY code.",
                max_tokens=16384,
                temperature=0.05,
            )
            code = strip_code_fences(code)

            # Auf Festplatte schreiben
            full_path = os.path.join(self.project_dir, path)
            write_file(full_path, code)

            changes.append({"path": path, "action": action, "content": code})
            self.blog.info(f"Developer schrieb: {path} ({len(code)} chars)")

        return {
            "files": changes,
            "description": plan.get("description", ""),
        }

    # ------------------------------------------------------------------
    # Hilfsfunktionen
    # ------------------------------------------------------------------

    def _gather_related_context(self, target_path: str, plan: dict) -> str:
        """Inhalt verwandter Dateien fuer besseren Kontext zusammentragen."""
        related = []

        # Andere Dateien aus dem Plan lesen
        for f in plan.get("files_to_modify", [])[:5]:
            if f["path"] != target_path:
                content = self.read_project_file(f["path"])
                if content:
                    related.append(f"=== {f['path']} ===\n{content[:3000]}")

        # Imports aus bestehender Datei folgen
        existing = self.read_project_file(target_path)
        if existing:
            imports = re.findall(r"from\s+([\w.]+)\s+import", existing)
            for imp in imports[:5]:
                imp_path = imp.replace(".", "/") + ".py"
                content = self.read_project_file(imp_path)
                if content and imp_path != target_path:
                    related.append(f"=== {imp_path} ===\n{content[:2000]}")

        return "\n\n".join(related[:5]) if related else "(keine verwandten Dateien)"
