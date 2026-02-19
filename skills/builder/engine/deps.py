"""
skills/builder/engine/deps.py - Dependency installation
========================================================
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from skills.builder.engine.context import blog


def install_deps(project_dir: str, language: str) -> tuple[bool, str]:
    """Install project dependencies before compile checks and sandbox tests."""
    blog.phase("install_deps", f"Installing {language} dependencies")

    installers = {
        "python": _install_python_deps,
        "javascript": _install_node_deps,
        "typescript": _install_node_deps,
        "rust": _install_rust_deps,
        "go": _install_go_deps,
    }

    installer = installers.get(language)
    if not installer:
        blog.info(f"No dependency installer for {language}")
        return True, "No installer available"

    try:
        return installer(project_dir)
    except Exception as exc:
        blog.warning(f"Dependency installation error: {exc}")
        return False, str(exc)


def get_venv_python(project_dir: str) -> str:
    """Return the Python executable inside the project venv, creating it if needed."""
    venv_dir = os.path.join(project_dir, ".venv")
    if os.name == "nt":
        venv_python = os.path.join(venv_dir, "Scripts", "python.exe")
    else:
        venv_python = os.path.join(venv_dir, "bin", "python")

    if not os.path.exists(venv_python):
        blog.info("Creating virtual environment for project...")
        try:
            subprocess.run(
                [sys.executable, "-m", "venv", venv_dir],
                capture_output=True, text=True, timeout=60, cwd=project_dir,
            )
        except Exception as exc:
            blog.warning(f"Could not create venv: {exc}, falling back to system python")
            return sys.executable

    if os.path.exists(venv_python):
        return venv_python
    return sys.executable


def _install_python_deps(project_dir: str) -> tuple[bool, str]:
    req_file = os.path.join(project_dir, "requirements.txt")
    if not os.path.exists(req_file):
        blog.info("No requirements.txt found, skipping pip install")
        return True, "No requirements.txt"

    venv_python = get_venv_python(project_dir)
    blog.info(f"Using Python: {venv_python}")

    try:
        result = subprocess.run(
            [
                venv_python,
                "-m",
                "pip",
                "install",
                "-r",
                "requirements.txt",
                "--quiet",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=project_dir,
        )
        if result.returncode == 0:
            blog.verify(True, "pip_install", "Dependencies installed (venv)")
            return True, "pip install OK"
        msg = result.stderr[:500]
        blog.error(f"pip install failed: {msg}", severity="deps")
        return False, msg
    except subprocess.TimeoutExpired:
        blog.error("pip install timed out (120s)", severity="deps")
        return False, "pip install timeout"


def _install_node_deps(project_dir: str) -> tuple[bool, str]:
    pkg_file = os.path.join(project_dir, "package.json")
    if not os.path.exists(pkg_file):
        blog.info("No package.json found, skipping npm install")
        return True, "No package.json"

    try:
        result = subprocess.run(
            ["npm", "install", "--quiet"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=project_dir,
        )
        if result.returncode == 0:
            blog.verify(True, "npm_install", "Dependencies installed")
            return True, "npm install OK"
        msg = result.stderr[:500]
        blog.error(f"npm install failed: {msg}", severity="deps")
        return False, msg
    except subprocess.TimeoutExpired:
        blog.error("npm install timed out (120s)", severity="deps")
        return False, "npm install timeout"
    except FileNotFoundError:
        blog.warning("npm not found, skipping")
        return True, "npm not available"


def _install_rust_deps(project_dir: str) -> tuple[bool, str]:
    cargo_file = os.path.join(project_dir, "Cargo.toml")
    if not os.path.exists(cargo_file):
        return True, "No Cargo.toml"
    if not shutil.which("cargo"):
        return True, "cargo not available"

    try:
        result = subprocess.run(
            ["cargo", "build", "--message-format=short"],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=project_dir,
        )
        if result.returncode == 0:
            blog.verify(True, "cargo_build", "Dependencies compiled")
            return True, "cargo build OK"
        msg = (result.stdout + result.stderr)[:500]
        blog.error(f"cargo build failed: {msg}", severity="deps")
        return False, msg
    except subprocess.TimeoutExpired:
        blog.error("cargo build timed out (300s)", severity="deps")
        return False, "cargo build timeout"


def _install_go_deps(project_dir: str) -> tuple[bool, str]:
    gomod_file = os.path.join(project_dir, "go.mod")
    if not os.path.exists(gomod_file):
        return True, "No go.mod"
    if not shutil.which("go"):
        return True, "go not available"

    try:
        result = subprocess.run(
            ["go", "mod", "tidy"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=project_dir,
        )
        if result.returncode != 0:
            msg = result.stderr[:500]
            blog.error(f"go mod tidy failed: {msg}", severity="deps")
            return False, msg

        result2 = subprocess.run(
            ["go", "build", "./..."],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=project_dir,
        )
        if result2.returncode == 0:
            blog.verify(True, "go_build", "Dependencies compiled")
            return True, "go build OK"
        msg = result2.stderr[:500]
        blog.error(f"go build failed: {msg}", severity="deps")
        return False, msg
    except subprocess.TimeoutExpired:
        blog.error("go build timed out (120s)", severity="deps")
        return False, "go build timeout"
