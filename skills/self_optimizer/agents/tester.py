"""
skills/self_optimizer/agents/tester.py -- Tester Agent
=======================================================
Fuehrt Code in Docker-Sandbox aus, prueft Syntax, Imports und Tests.
"""

from __future__ import annotations

import logging
import os
import py_compile

from core.dual_logger import BuildLogger
from skills.self_optimizer.agents.base import BaseAgent
from skills.self_optimizer.config import OptimizerConfig

log = logging.getLogger("ai-hub.optimizer.tester")


class TesterAgent(BaseAgent):
    AGENT_NAME = "tester"

    def __init__(
        self,
        project_dir: str,
        sandbox,  # OptimizerSandbox
        config: OptimizerConfig,
        blog: BuildLogger,
    ):
        super().__init__(project_dir, config, blog)
        self.sandbox = sandbox

    def test(self, changes: dict) -> dict:
        """
        Code-Aenderungen testen.

        Ablauf:
        1. Syntax-Check (py_compile, kein Docker noetig)
        2. Import-Check (Docker, network:none)
        3. Test-Ausfuehrung (Docker, network:none)

        Returns:
        {
            "success": bool,
            "output": str,
            "exit_code": int,
            "test_type": str,
            "errors": [str],
        }
        """
        self.blog.phase("tester_run", "Tester: Pruefung in Docker-Sandbox")

        results = {
            "success": True,
            "output": "",
            "exit_code": 0,
            "test_type": "multi",
            "errors": [],
        }

        # Schritt 1: Syntax-Check (kein Docker)
        syntax_ok, syntax_errors = self._syntax_check(changes)
        if not syntax_ok:
            results["success"] = False
            results["errors"].extend(syntax_errors)
            results["output"] = "Syntax-Fehler:\n" + "\n".join(syntax_errors)
            results["test_type"] = "syntax_check"
            self.blog.verify(
                False, "syntax_check",
                f"{len(syntax_errors)} Syntax-Fehler"
            )
            return results

        self.blog.verify(True, "syntax_check", "Alle Dateien bestehen Syntax-Check")

        # Schritt 2: Import-Check in Docker
        changed_py_files = [
            c["path"] for c in changes.get("files", [])
            if c["path"].endswith(".py") and c.get("action") != "delete"
        ]

        if changed_py_files and self.sandbox:
            import_ok, import_output = self.sandbox.run_import_check(
                self.project_dir,
                changed_files=changed_py_files,
            )
            if not import_ok:
                results["success"] = False
                results["errors"].append(
                    f"Import-Check fehlgeschlagen: {import_output[:500]}"
                )
                results["output"] = import_output
                results["test_type"] = "import_check"
                self.blog.verify(False, "import_check", import_output[:200])
                return results

            self.blog.verify(True, "import_check", "Import-Check bestanden")

        # Schritt 3: Tests ausfuehren falls vorhanden
        if self.sandbox:
            test_ok, test_output = self.sandbox.run_tests(self.project_dir)
            results["output"] = test_output

            if not test_ok:
                results["success"] = False
                results["errors"].append(
                    f"Tests fehlgeschlagen: {test_output[:500]}"
                )
                results["test_type"] = "unit_test"
                self.blog.verify(False, "unit_test", test_output[:200])
                return results

        self.blog.verify(True, "tester", "Alle Pruefungen bestanden")
        return results

    def _syntax_check(self, changes: dict) -> tuple[bool, list[str]]:
        """Python-Syntax der geaenderten Dateien pruefen (ohne Docker)."""
        errors = []
        for change in changes.get("files", []):
            path = change["path"]
            if not path.endswith(".py") or change.get("action") == "delete":
                continue
            full_path = os.path.join(self.project_dir, path)
            if not os.path.exists(full_path):
                continue
            try:
                # doraise=True wirft bei Syntax-Fehler,
                # cfile=os.devnull verhindert .pyc Erzeugung
                py_compile.compile(full_path, doraise=True, cfile=os.devnull)
            except py_compile.PyCompileError as exc:
                errors.append(f"{path}: {exc}")
        return len(errors) == 0, errors
