"""
skills/self_optimizer/agents/reviewer.py -- Reviewer Agent
===========================================================
Prueft Test-Ergebnisse + Code-Qualitaet, trifft Entscheidung:
MERGE / RETRY / REJECT.
"""

from __future__ import annotations

import enum
import json
import logging

from core.utils import clean_json_output
from core.dual_logger import BuildLogger
from skills.self_optimizer.agents.base import BaseAgent
from skills.self_optimizer.config import OptimizerConfig

log = logging.getLogger("ai-hub.optimizer.reviewer")


class ReviewDecision(enum.Enum):
    MERGE = "merge"
    REJECT = "reject"
    RETRY = "retry"


class ReviewerAgent(BaseAgent):
    AGENT_NAME = "reviewer"

    def __init__(
        self,
        project_dir: str,
        reasoning_model: str,
        config: OptimizerConfig,
        blog: BuildLogger,
    ):
        super().__init__(project_dir, config, blog)
        self.reasoning_model = reasoning_model

    def review(self, task: str, changes: dict, test_result: dict) -> dict:
        """
        Code-Aenderungen und Test-Ergebnisse pruefen.

        Returns:
        {
            "decision": ReviewDecision,
            "reason": str,
            "quality_score": int,  # 1-10
            "security_issues": [str],
            "suggestions": [str],
        }
        """
        self.blog.phase(
            "reviewer_review",
            "Reviewer: Analyse der Aenderungen + Testergebnisse",
            model=self.reasoning_model,
        )

        # Review-Kontext aufbauen
        files_summary = []
        for change in changes.get("files", []):
            path = change["path"]
            action = change["action"]
            content = change.get("content", "")
            preview = content[:3000] if content else "(geloescht)"
            files_summary.append(f"=== {path} ({action}) ===\n{preview}")

        files_context = "\n\n".join(files_summary)

        prompt = f"""You are a senior code reviewer with security expertise.
Review the following code changes and test results.

TASK: {task}

CHANGES:
{files_context}

TEST RESULTS:
  Success: {test_result.get('success', False)}
  Exit code: {test_result.get('exit_code', -1)}
  Output: {test_result.get('output', '')[:2000]}
  Errors: {test_result.get('errors', [])}

Review criteria:
1. CORRECTNESS: Does the code implement the task correctly?
2. SAFETY: No file system access outside project dir, no network calls, no secrets exposure
3. QUALITY: Code style, error handling, edge cases
4. TESTS: Did tests pass? Are test results meaningful?
5. SECURITY: No command injection, no path traversal, no data leaks, no API keys in code

DECISION RULES:
- MERGE: Tests pass, code is correct, no security issues
- RETRY: Minor issues that could be fixed with another attempt
- REJECT: Fundamental problems, security issues, or wrong approach

Output ONLY this JSON:
{{
  "decision": "merge" | "reject" | "retry",
  "reason": "<1-2 sentence explanation>",
  "quality_score": <1-10>,
  "security_issues": ["<issue 1>", ...],
  "suggestions": ["<suggestion 1>", ...]
}}"""

        response = self.llm(
            model=self.reasoning_model,
            prompt=prompt,
            system="Expert code reviewer with security focus. Output only JSON.",
            max_tokens=2048,
            temperature=0.1,
        )

        try:
            parsed = json.loads(clean_json_output(response))
            decision_str = parsed.get("decision", "reject").lower()
            decision = {
                "merge": ReviewDecision.MERGE,
                "retry": ReviewDecision.RETRY,
                "reject": ReviewDecision.REJECT,
            }.get(decision_str, ReviewDecision.REJECT)

            result = {
                "decision": decision,
                "reason": parsed.get("reason", "Kein Grund angegeben"),
                "quality_score": parsed.get("quality_score", 0),
                "security_issues": parsed.get("security_issues", []),
                "suggestions": parsed.get("suggestions", []),
            }

            # Erzwinge REJECT bei Security-Issues
            if result["security_issues"]:
                self.blog.warning(
                    f"Security-Issues gefunden: {result['security_issues']}"
                )
                result["decision"] = ReviewDecision.REJECT
                result["reason"] = (
                    f"Security: {'; '.join(result['security_issues'][:3])}"
                )

            # Erzwinge RETRY falls Tests fehlgeschlagen aber MERGE empfohlen
            if (
                not test_result.get("success", False)
                and decision == ReviewDecision.MERGE
            ):
                self.blog.warning(
                    "Reviewer empfiehlt MERGE aber Tests fehlgeschlagen "
                    "-- Override zu RETRY"
                )
                result["decision"] = ReviewDecision.RETRY
                result["reason"] = "Tests fehlgeschlagen trotz Reviewer-Zustimmung"

            self.blog.info(
                f"Review: {result['decision'].value} "
                f"(Score={result['quality_score']}, "
                f"Grund={result['reason'][:80]})"
            )
            return result

        except (json.JSONDecodeError, ValueError) as exc:
            self.blog.warning(f"Review-Parsing fehlgeschlagen: {exc}")
            return {
                "decision": ReviewDecision.RETRY,
                "reason": f"Review-Antwort nicht parsbar: {exc}",
                "quality_score": 0,
                "security_issues": [],
                "suggestions": [],
            }
