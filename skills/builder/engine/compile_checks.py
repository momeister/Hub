"""
skills/builder/engine/compile_checks.py - Compile checks
========================================================
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from skills.builder.engine.context import write_file


def compile_check_rust(project_dir: str) -> tuple[bool, list[str]]:
    if not shutil.which("cargo"):
        return True, []
    result = subprocess.run(
        ["cargo", "check", "--message-format=short"],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=project_dir,
    )
    if result.returncode == 0:
        return True, []
    errors = []
    combined = result.stdout + result.stderr
    for line in combined.splitlines():
        if "error" in line:
            errors.append(line)
    return False, errors[:20]


def compile_check_go(project_dir: str) -> tuple[bool, list[str]]:
    if not shutil.which("go"):
        return True, []
    result = subprocess.run(
        ["go", "build", "./..."],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=project_dir,
    )
    if result.returncode == 0:
        return True, []
    return False, result.stderr.splitlines()[:20]


def compile_check_typescript(project_dir: str) -> tuple[bool, list[str]]:
    if not shutil.which("tsc"):
        return True, []
    tsconfig = Path(project_dir) / "tsconfig.json"
    if not tsconfig.exists():
        write_file(
            str(tsconfig),
            json.dumps(
                {
                    "compilerOptions": {
                        "target": "ES2020",
                        "module": "ESNext",
                        "strict": True,
                        "esModuleInterop": True,
                        "skipLibCheck": True,
                        "jsx": "react-jsx",
                    }
                },
                indent=2,
            ),
        )
    result = subprocess.run(
        ["tsc", "--noEmit"],
        capture_output=True,
        text=True,
        timeout=90,
        cwd=project_dir,
    )
    if result.returncode == 0:
        return True, []
    return False, result.stdout.splitlines()[:20]


def compile_check_javascript(project_dir: str) -> tuple[bool, list[str]]:
    """Check JavaScript files for syntax errors using ``node --check``.

    Falls back to tsc if available; otherwise uses node's built-in
    syntax checker which catches SyntaxErrors without executing code.
    """
    if shutil.which("tsc"):
        return compile_check_typescript(project_dir)

    node = shutil.which("node")
    if not node:
        return True, []

    errors: list[str] = []
    js_files = list(Path(project_dir).rglob("*.js"))
    # Skip node_modules
    js_files = [f for f in js_files if "node_modules" not in str(f)]

    for js_file in js_files[:50]:  # cap to avoid huge projects
        try:
            result = subprocess.run(
                [node, "--check", str(js_file)],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=project_dir,
            )
            if result.returncode != 0:
                combined = (result.stderr + result.stdout).strip()
                rel = str(js_file.relative_to(project_dir))
                for line in combined.splitlines()[:3]:
                    errors.append(f"{rel}: {line}")
        except Exception:
            pass

    return (len(errors) == 0), errors[:20]


def compile_check_python(project_dir: str) -> tuple[bool, list[str]]:
    if not shutil.which("mypy"):
        return True, []
    result = subprocess.run(
        ["mypy", project_dir, "--ignore-missing-imports", "--no-error-summary"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode == 0:
        return True, []
    errors = [l for l in result.stdout.splitlines() if "error:" in l][:20]
    return False, errors


def compile_check(project_dir: str, language: str) -> tuple[bool, list[str]]:
    checkers = {
        "rust": compile_check_rust,
        "go": compile_check_go,
        "typescript": compile_check_typescript,
        "javascript": compile_check_javascript,
        "python": compile_check_python,
    }
    checker = checkers.get(language)
    if checker:
        return checker(project_dir)
    return True, []


def html_smoke_test(output_dir: str) -> tuple[bool, str]:
    """
    Check HTML/JS projects with minimal validation:
    - Reads all .html files
    - Checks that all referenced <script src> and <link href> files exist
    - Checks that JavaScript files parse with node --check
    Returns (True, "") on success or (False, error description) on failure.
    """
    import glob
    import os
    import re

    errors = []
    html_files = glob.glob(os.path.join(output_dir, "**/*.html"), recursive=True) + \
                 glob.glob(os.path.join(output_dir, "*.html"))

    # Deduplicate
    html_files = list(dict.fromkeys(html_files))

    if not html_files:
        return True, ""  # No HTML, nothing to test

    for html_path in html_files:
        try:
            content = open(html_path, encoding="utf-8").read()
        except Exception:
            continue

        html_dir = os.path.dirname(html_path)

        # Check <script src="...">
        for src in re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', content, re.IGNORECASE):
            if src.startswith(("http://", "https://", "//", "data:")):
                continue
            abs_path = os.path.normpath(os.path.join(html_dir, src))
            if not os.path.exists(abs_path):
                errors.append(f"Missing script: {src} (referenced in {os.path.basename(html_path)})")

        # Check <link href="...css">
        for href in re.findall(r'<link[^>]+href=["\']([^"\']+\.css)["\']', content, re.IGNORECASE):
            if href.startswith(("http://", "https://", "//", "data:")):
                continue
            abs_path = os.path.normpath(os.path.join(html_dir, href))
            if not os.path.exists(abs_path):
                errors.append(f"Missing stylesheet: {href} (referenced in {os.path.basename(html_path)})")

    # Syntax check for all .js files with node --check
    if shutil.which("node"):
        js_files = glob.glob(os.path.join(output_dir, "**/*.js"), recursive=True) + \
                   glob.glob(os.path.join(output_dir, "*.js"))
        js_files = list(dict.fromkeys(js_files))
        for js_path in js_files[:10]:  # Max 10 files
            if "node_modules" in js_path:
                continue
            try:
                result = subprocess.run(
                    ["node", "--check", js_path],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode != 0:
                    rel = os.path.relpath(js_path, output_dir)
                    errors.append(f"JS syntax error in {rel}: {result.stderr.strip()[:200]}")
            except Exception:
                pass

    if errors:
        return False, "\n".join(errors)
    return True, ""
