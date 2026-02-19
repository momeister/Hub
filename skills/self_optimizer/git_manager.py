"""
skills/self_optimizer/git_manager.py -- Git-Operationen
========================================================
Init, Branch, Commit, Merge, Rollback, Tag.
Alle Operationen auf dem HOST-Dateisystem (nicht in Docker).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Optional

from core.dual_logger import BuildLogger

log = logging.getLogger("ai-hub.optimizer.git")


class GitManager:
    def __init__(self, project_dir: str, blog: BuildLogger):
        self.project_dir = project_dir
        self.blog = blog

    # ------------------------------------------------------------------
    # Intern
    # ------------------------------------------------------------------

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """Git-Befehl im Projektverzeichnis ausfuehren."""
        cmd = ["git", "-C", self.project_dir] + list(args)
        self.blog.info(f"git {' '.join(args)}")
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=check,
        )

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def is_initialized(self) -> bool:
        return os.path.isdir(os.path.join(self.project_dir, ".git"))

    def get_current_branch(self) -> str:
        result = self._run("branch", "--show-current", check=False)
        return result.stdout.strip()

    # ------------------------------------------------------------------
    # Initialisierung
    # ------------------------------------------------------------------

    def ensure_initialized(self) -> None:
        """Git-Repo mit stable + experimental Branches initialisieren."""
        if self.is_initialized():
            self.blog.info("Git bereits initialisiert")
            return

        self.blog.phase("git_init", "Git-Repository initialisieren")

        # .gitignore erstellen
        gitignore_path = os.path.join(self.project_dir, ".gitignore")
        if not os.path.exists(gitignore_path):
            gitignore_content = (
                "# Python\n"
                "__pycache__/\n"
                "*.pyc\n"
                "*.pyo\n"
                "*.egg-info/\n"
                "\n"
                "# Environment\n"
                ".env\n"
                "\n"
                "# Virtual Environments\n"
                ".venv/\n"
                "venv/\n"
                "\n"
                "# Output\n"
                "output/\n"
                "\n"
                "# Temp Files\n"
                "tmpclaude-*\n"
                "*.tmp\n"
                ".screenshot.png\n"
                "\n"
                "# Optimizer\n"
                "DOWNLOAD_REQUESTS.md\n"
                "INTERNET_REQUESTS.md\n"
                ".optimizer_approval_signal\n"
                ".optimizer_state.json\n"
            )
            with open(gitignore_path, "w", encoding="utf-8") as f:
                f.write(gitignore_content)

        self._run("init")
        self._run("checkout", "-b", "stable")
        self._run("add", "-A")
        self._run("commit", "-m", "Initial commit: AI HUB baseline", "--allow-empty")
        self._run("checkout", "-b", "experimental")
        self.blog.info("Git initialisiert: stable + experimental Branches")

    # ------------------------------------------------------------------
    # Branch-Management
    # ------------------------------------------------------------------

    def ensure_on_experimental(self) -> None:
        """Auf experimental Branch wechseln (erzeugen falls noetig)."""
        current = self.get_current_branch()
        if current == "experimental":
            return
        branches = self._run("branch", "--list", "experimental", check=False)
        if "experimental" in branches.stdout:
            self._run("checkout", "experimental")
        else:
            self._run("checkout", "-b", "experimental")

    def ensure_on_stable(self) -> None:
        """Auf stable Branch wechseln."""
        current = self.get_current_branch()
        if current != "stable":
            self._run("checkout", "stable")

    # ------------------------------------------------------------------
    # Commit
    # ------------------------------------------------------------------

    def commit_changes(self, message: str, files: list = None) -> str:
        """Aenderungen auf experimental committen. Gibt Commit-Hash zurueck."""
        self.ensure_on_experimental()

        if files:
            for f in files:
                path = f if isinstance(f, str) else f.get("path", "")
                action = "modify" if isinstance(f, str) else f.get("action", "modify")
                if action == "delete":
                    self._run("rm", "--cached", path, check=False)
                else:
                    self._run("add", path)
        else:
            self._run("add", "-A")

        result = self._run("commit", "-m", message, check=False)
        if result.returncode != 0:
            if "nothing to commit" in result.stdout:
                self.blog.info("Nichts zu committen")
                return ""
            self.blog.warning(f"Commit fehlgeschlagen: {result.stderr}")
            return ""

        hash_result = self._run("rev-parse", "--short", "HEAD", check=False)
        commit_hash = hash_result.stdout.strip()
        self.blog.info(f"Committed: {commit_hash} -- {message[:60]}")
        return commit_hash

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------

    def rollback_experimental(self) -> None:
        """Experimental Branch auf stable zuruecksetzen."""
        self.blog.phase("git_rollback", "Experimental Branch wird zurueckgesetzt")
        self.ensure_on_experimental()
        self._run("reset", "--hard", "stable")
        self.blog.info("Experimental auf stable zurueckgesetzt")

    # ------------------------------------------------------------------
    # Merge
    # ------------------------------------------------------------------

    def merge_to_stable(self, tag_prefix: str = "v", message: str = "") -> str:
        """Experimental in stable mergen und Version-Tag erstellen."""
        self.blog.phase("git_merge", "Merge: experimental -> stable")

        self._run("checkout", "stable")
        result = self._run(
            "merge", "experimental", "--no-ff",
            "-m", f"[optimizer] {message[:72]}",
            check=False,
        )

        if result.returncode != 0:
            self.blog.error(f"Merge fehlgeschlagen: {result.stderr}", severity="git")
            self._run("merge", "--abort", check=False)
            self._run("checkout", "experimental")
            raise RuntimeError(f"Merge fehlgeschlagen: {result.stderr}")

        # Version-Tag erstellen
        version = self._next_version(tag_prefix)
        self._run("tag", "-a", version, "-m", f"[optimizer] {message[:60]}")

        # Experimental auf stable zuruecksetzen
        self._run("checkout", "experimental")
        self._run("reset", "--hard", "stable")

        self.blog.info(f"Gemergt und getaggt als {version}")
        return version

    # ------------------------------------------------------------------
    # Versioning
    # ------------------------------------------------------------------

    def _next_version(self, prefix: str = "v") -> str:
        """Naechsten semantischen Version-Tag generieren."""
        result = self._run(
            "tag", "--list", f"{prefix}*",
            "--sort=-version:refname",
            check=False,
        )
        tags = result.stdout.strip().split("\n")
        tags = [t.strip() for t in tags if t.strip()]

        if not tags:
            return f"{prefix}0.1.0"

        latest = tags[0]
        match = re.match(rf"{re.escape(prefix)}(\d+)\.(\d+)\.(\d+)", latest)
        if match:
            major, minor, patch = (
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
            )
            return f"{prefix}{major}.{minor}.{patch + 1}"

        return f"{prefix}0.1.0"

    def get_current_version(self) -> str:
        """Aktuellen Version-Tag zurueckgeben."""
        result = self._run("describe", "--tags", "--abbrev=0", check=False)
        return result.stdout.strip() if result.returncode == 0 else "v0.0.0"

    def get_diff_summary(self) -> str:
        """Zusammenfassung der Aenderungen zwischen stable und experimental."""
        result = self._run("diff", "--stat", "stable..experimental", check=False)
        return result.stdout.strip()
