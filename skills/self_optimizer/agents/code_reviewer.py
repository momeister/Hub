"""
skills/self_optimizer/agents/code_reviewer.py -- Code-Review Agent
==================================================================
Reiner Qualitaets-Review ohne Test-Ausfuehrung.
Analysiert Code inhaltlich: Logik, Edge-Cases, Konsistenz zwischen Dateien,
Korrektheit der Implementierung bezogen auf die urspruengliche Aufgabe.
Laeuft NACH dem Tester, VOR dem finalen Reviewer.
"""

from __future__ import annotations

import json
import logging

from core.utils import clean_json_output
from core.dual_logger import BuildLogger
from skills.self_optimizer.agents.base import BaseAgent
from skills.self_optimizer.config import OptimizerConfig

log = logging.getLogger("ai-hub.optimizer.code_reviewer")


class CodeReviewerAgent(BaseAgent):
    """
    Reiner Qualitaets-Review ohne Test-Ausfuehrung.
    Analysiert Code inhaltlich: Logik, Edge-Cases, Konsistenz zwischen Dateien,
    Korrektheit der Implementierung bezogen auf die urspruengliche Aufgabe.
    Laeuft NACH dem Tester, VOR dem finalen Reviewer.
    """

    AGENT_NAME = "code_reviewer"

    def __init__(
        self,
        project_dir: str,
        reasoning_model: str,
        config: OptimizerConfig,
        blog: BuildLogger,
    ):
        super().__init__(project_dir, config, blog)
        self.reasoning_model = reasoning_model

    def review_code(self, task: str, changes: dict) -> dict:
        """
        Inhaltliche Code-Qualitaetspruefung.

        Returns:
        {
            "passed": bool,
            "quality_score": int,  # 1-10
            "issues": [{"file": str, "line_hint": str, "severity": str, "description": str}],
            "cross_file_issues": [str],
            "summary": str,
        }
        """
        self.blog.phase(
            "code_review",
            "Code-Reviewer: Inhaltliche Qualitätsprüfung",
            model=self.reasoning_model,
        )

        # Alle geaenderten Dateien vollstaendig laden
        files_context = []
        for change in changes.get("files", []):
            path = change["path"]
            content = change.get("content", "")
            if content and change.get("action") != "delete":
                files_context.append(f"=== {path} ===\n{content}")

        all_code = "\n\n".join(files_context)

        prompt = f"""Du bist ein erfahrener Senior-Developer der Code inhaltlich reviewed.

AUFGABE DIE IMPLEMENTIERT WERDEN SOLLTE:
{task}

GEÄNDERTER CODE:
{all_code}

Prüfe folgende Aspekte:
1. KORREKTHEIT: Implementiert der Code was die Aufgabe verlangt?
2. LOGIK-FEHLER: Gibt es Off-by-one, falsche Bedingungen, fehlende Returns?
3. EDGE-CASES: Werden None, leere Listen, negative Zahlen etc. behandelt?
4. CROSS-FILE: Falls mehrere Dateien geändert wurden — passen die Interfaces zusammen?
   Stimmen Funktionsnamen, Parameter-Typen und Rückgabeformate überein?
5. KONSISTENZ: Folgt der Code dem Stil des restlichen Projekts?

Sei konkret. Verweise auf spezifische Funktionen oder Zeilen.

Output NUR dieses JSON:
{{
  "passed": true/false,
  "quality_score": 1-10,
  "issues": [
    {{
      "file": "<dateiname>",
      "line_hint": "<funktionsname oder ungefähre Zeilenbeschreibung>",
      "severity": "critical|warning|info",
      "description": "<was genau ist das Problem>"
    }}
  ],
  "cross_file_issues": ["<problem zwischen datei A und B>"],
  "summary": "<2-3 Sätze Gesamteinschätzung>"
}}

passed=false wenn es mindestens ein critical-Issue gibt."""

        response = self.llm(
            model=self.reasoning_model,
            prompt=prompt,
            system="Erfahrener Code-Reviewer. Output nur valides JSON.",
            max_tokens=4096,
            temperature=0.1,
        )

        try:
            parsed = json.loads(clean_json_output(response))

            # Logging
            score = parsed.get("quality_score", 0)
            issues = parsed.get("issues", [])
            critical = [i for i in issues if i.get("severity") == "critical"]
            self.blog.info(
                f"Code-Review: Score={score}/10, "
                f"{len(issues)} Issues ({len(critical)} kritisch)"
            )
            for issue in critical[:3]:
                self.blog.warning(
                    f"  KRITISCH [{issue.get('file', '')}] "
                    f"{issue.get('line_hint', '')}: {issue.get('description', '')}"
                )

            return parsed
        except Exception as exc:
            self.blog.warning(f"Code-Review Parsing fehlgeschlagen: {exc}")
            return {
                "passed": True,
                "quality_score": 5,
                "issues": [],
                "cross_file_issues": [],
                "summary": f"Review konnte nicht geparst werden: {exc}",
            }
