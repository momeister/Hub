"""
skills/self_optimizer/docker_sandbox.py -- Docker-Sandbox
==========================================================
Isolierte Code-Ausfuehrung mit:
- network_mode: none (kein Internet)
- user: 1000:1000 (kein Root)
- read-only Dateisystem + tmpfs fuer /tmp
- Memory/CPU/PID-Limits
"""

from __future__ import annotations

import logging
import os
import subprocess

from core.dual_logger import BuildLogger
from skills.self_optimizer.config import OptimizerConfig

log = logging.getLogger("ai-hub.optimizer.sandbox")

OPTIMIZER_SANDBOX_IMAGE = "ai-hub-optimizer-sandbox"


class OptimizerSandbox:
    def __init__(self, config: OptimizerConfig, blog: BuildLogger):
        self.config = config
        self.blog = blog

    # ------------------------------------------------------------------
    # Docker Container ausfuehren
    # ------------------------------------------------------------------

    def _docker_run(
        self,
        project_dir: str,
        command: str,
        timeout: int = 60,
        allow_network: bool = False,
    ) -> tuple[int, str]:
        """
        Befehl in Docker mit allen Sicherheitsbeschraenkungen ausfuehren.
        Returns (exit_code, combined_output).
        """
        # Windows-Pfade fuer Docker konvertieren
        docker_dir = project_dir.replace("\\", "/")

        cmd = [
            "docker", "run", "--rm",
            "--user", "1000:1000",
            f"--memory={self.config.container_memory_limit}",
            f"--cpus={self.config.container_cpu_limit}",
            "--pids-limit=256",
            "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=100m",
            "-v", f"{docker_dir}:/app/code:ro",  # READ-ONLY Mount
            "-w", "/app/code",
        ]

        if not allow_network:
            cmd.append("--network=none")

        cmd.extend([OPTIMIZER_SANDBOX_IMAGE, "sh", "-c", command])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = (result.stdout + result.stderr)[:8000]
            return result.returncode, output

        except subprocess.TimeoutExpired:
            return -1, f"Ausfuehrung abgebrochen nach {timeout}s Timeout"
        except FileNotFoundError:
            return -1, "Docker nicht installiert oder nicht im PATH"
        except Exception as e:
            return -1, f"Docker-Fehler: {e}"

    # ------------------------------------------------------------------
    # Test-Methoden
    # ------------------------------------------------------------------

    def run_import_check(
        self,
        project_dir: str,
        changed_files: list[str],
    ) -> tuple[bool, str]:
        """Pruefen ob alle geaenderten Python-Dateien importierbar sind."""
        if not changed_files:
            return True, "Keine Python-Dateien zu pruefen"

        # Syntax-Pruefung via py_compile
        syntax_cmds = [
            f"python -m py_compile {f} 2>&1" for f in changed_files
        ]
        command = " && ".join(syntax_cmds)
        exit_code, output = self._docker_run(
            project_dir, command, timeout=30,
        )

        return exit_code == 0, output

    def run_tests(self, project_dir: str) -> tuple[bool, str]:
        """Vorhandene Tests im Projekt ausfuehren."""
        test_command = (
            "if [ -f pytest.ini ] || [ -f setup.cfg ] || [ -d tests ]; then "
            "  python -m pytest --tb=short -q 2>&1; "
            "elif ls test_*.py 2>/dev/null; then "
            "  python -m pytest test_*.py --tb=short -q 2>&1; "
            "elif [ -f main.py ]; then "
            "  python -c 'import main' 2>&1 && echo 'Import OK'; "
            "else "
            "  echo 'Keine Tests gefunden - nur Import-Check'; "
            "fi"
        )

        exit_code, output = self._docker_run(
            project_dir,
            test_command,
            timeout=self.config.test_timeout,
        )

        return exit_code == 0, output

    # ------------------------------------------------------------------
    # Image-Management
    # ------------------------------------------------------------------

    def ensure_image(self) -> bool:
        """Sandbox Docker-Image pruefen und bei Bedarf bauen."""
        # Pruefen ob Image existiert
        check = subprocess.run(
            ["docker", "image", "inspect", OPTIMIZER_SANDBOX_IMAGE],
            capture_output=True,
            text=True,
        )
        if check.returncode == 0:
            self.blog.info(
                f"Sandbox-Image '{OPTIMIZER_SANDBOX_IMAGE}' vorhanden"
            )
            return True

        return self.build_image()

    def build_image(self) -> bool:
        """Sandbox Docker-Image bauen."""
        dockerfile_path = os.path.join(
            os.path.dirname(__file__), "Dockerfile"
        )
        if not os.path.exists(dockerfile_path):
            self.blog.warning(
                f"Sandbox-Dockerfile nicht gefunden: {dockerfile_path}"
            )
            return False

        context_dir = os.path.dirname(dockerfile_path)
        self.blog.phase(
            "docker_build",
            f"Baue Sandbox-Image '{OPTIMIZER_SANDBOX_IMAGE}'...",
        )

        cmd = [
            "docker", "build",
            "-t", OPTIMIZER_SANDBOX_IMAGE,
            "-f", dockerfile_path,
            context_dir,
        ]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                self.blog.info(
                    f"Sandbox-Image '{OPTIMIZER_SANDBOX_IMAGE}' erfolgreich gebaut"
                )
                return True
            self.blog.error(
                f"Sandbox-Build fehlgeschlagen: {result.stderr[:500]}",
                severity="docker",
            )
            return False
        except Exception as e:
            self.blog.error(f"Docker-Build-Fehler: {e}", severity="docker")
            return False
