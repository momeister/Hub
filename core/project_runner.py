"""
core/project_runner.py — Docker-Isolated Project Execution
============================================================
Detects project language and runs generated projects inside Docker.
Used by the /run Telegram command.
"""

import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger("ai-hub.runner")

# ============================================================================
# DOCKER IMAGES PER LANGUAGE
# ============================================================================

RUNNER_IMAGES = {
    "python": "python:3.12-slim",
    "rust": "rust:1.78-slim",
    "go": "golang:1.22-alpine",
    "javascript": "node:20-slim",
    "typescript": "node:20-slim",
    "html": "python:3.12-slim",
}

# ============================================================================
# RUN COMMANDS PER LANGUAGE
# ============================================================================

RUNNER_COMMANDS = {
    "python": "pip install -r requirements.txt 2>/dev/null; python main.py 2>&1",
    "rust": "cargo run --release 2>&1",
    "go": "go run . 2>&1",
    "javascript": "npm install --silent 2>/dev/null; npm start 2>&1 || node index.js 2>&1",
    "typescript": "npm install --silent 2>/dev/null; npx ts-node index.ts 2>&1",
    "html": "echo 'HTML project - open index.html in browser'; ls *.html 2>&1",
}


# ============================================================================
# LANGUAGE DETECTION
# ============================================================================

# Priority-ordered: first match wins
LANGUAGE_MARKERS = [
    ("Cargo.toml", "rust"),
    ("go.mod", "go"),
    ("tsconfig.json", "typescript"),
    ("package.json", "javascript"),  # after tsconfig check
    ("requirements.txt", "python"),
    ("main.py", "python"),
    ("app.py", "python"),
    ("index.html", "html"),
]


def detect_project_language(project_dir: str) -> str:
    """
    Auto-detect language from project files.
    Returns language name or 'unknown'.
    """
    for marker, lang in LANGUAGE_MARKERS:
        if os.path.exists(os.path.join(project_dir, marker)):
            # Special case: package.json with tsconfig = typescript
            if marker == "package.json" and \
               os.path.exists(os.path.join(project_dir, "tsconfig.json")):
                return "typescript"
            return lang
    return "unknown"


# ============================================================================
# PROJECT RUNNER
# ============================================================================

def run_project(
    project_dir: str,
    language: str = "auto",
    timeout: int = 60,
    use_docker: bool = True,
) -> dict:
    """
    Run a generated project.

    Args:
        project_dir: Absolute path to the project directory
        language: Language override, or "auto" to detect
        timeout: Max execution time in seconds
        use_docker: True = Docker isolation, False = native subprocess

    Returns:
        {
            "exit_code": int,
            "output": str,       # First ~50 lines of output
            "language": str,
            "method": "docker" | "native",
            "error": str | None,
        }
    """
    project_dir = os.path.abspath(project_dir)

    if not os.path.isdir(project_dir):
        return {
            "exit_code": -1,
            "output": "",
            "language": "unknown",
            "method": "none",
            "error": f"Directory not found: {project_dir}",
        }

    # Detect language
    if language == "auto":
        language = detect_project_language(project_dir)

    if language == "unknown":
        return {
            "exit_code": -1,
            "output": "",
            "language": "unknown",
            "method": "none",
            "error": "Could not detect project language. No recognized marker files found.",
        }

    log.info(f"Running {language} project: {project_dir}")

    if use_docker:
        return _run_in_docker(project_dir, language, timeout)
    else:
        return _run_native(project_dir, language, timeout)


def _run_in_docker(project_dir: str, language: str, timeout: int) -> dict:
    """Run project inside a Docker container."""
    image = RUNNER_IMAGES.get(language)
    run_cmd = RUNNER_COMMANDS.get(language)

    if not image or not run_cmd:
        return {
            "exit_code": -1,
            "output": "",
            "language": language,
            "method": "docker",
            "error": f"No Docker image configured for {language}",
        }

    # Convert Windows path to Docker-compatible mount
    docker_dir = project_dir.replace("\\", "/")

    cmd = [
        "docker", "run", "--rm",
        "--network=none",       # No internet access (security)
        "--memory=512m",        # Memory limit
        "--cpus=2",             # CPU limit
        "-v", f"{docker_dir}:/app",
        "-w", "/app",
        image,
        "sh", "-c", run_cmd,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        output = (result.stdout + result.stderr)[:4000]
        output_lines = output.split("\n")[:50]
        trimmed_output = "\n".join(output_lines)

        return {
            "exit_code": result.returncode,
            "output": trimmed_output,
            "language": language,
            "method": "docker",
            "error": None,
        }

    except subprocess.TimeoutExpired:
        return {
            "exit_code": 0,  # Not a crash
            "output": f"Process ran for {timeout}s (likely a server/long-running app)",
            "language": language,
            "method": "docker",
            "error": None,
        }
    except FileNotFoundError:
        return {
            "exit_code": -1,
            "output": "",
            "language": language,
            "method": "docker",
            "error": "Docker not found. Install Docker or use native mode.",
        }
    except Exception as e:
        return {
            "exit_code": -1,
            "output": "",
            "language": language,
            "method": "docker",
            "error": str(e),
        }


def _run_native(project_dir: str, language: str, timeout: int) -> dict:
    """Run project as a native subprocess (no Docker)."""
    native_cmds = {
        "python": _find_native_cmd_python,
        "rust": lambda d: ["cargo", "run"],
        "go": lambda d: ["go", "run", "."],
        "javascript": _find_native_cmd_js,
        "typescript": lambda d: ["npx", "ts-node", "index.ts"],
        "html": lambda d: None,
    }

    cmd_fn = native_cmds.get(language)
    if not cmd_fn:
        return {
            "exit_code": -1,
            "output": "",
            "language": language,
            "method": "native",
            "error": f"No native runner for {language}",
        }

    cmd = cmd_fn(project_dir)
    if cmd is None:
        return {
            "exit_code": 0,
            "output": "HTML project - open index.html in your browser",
            "language": language,
            "method": "native",
            "error": None,
        }

    try:
        result = subprocess.run(
            cmd,
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        output = (result.stdout + result.stderr)[:4000]
        output_lines = output.split("\n")[:50]

        return {
            "exit_code": result.returncode,
            "output": "\n".join(output_lines),
            "language": language,
            "method": "native",
            "error": None,
        }

    except subprocess.TimeoutExpired:
        return {
            "exit_code": 0,
            "output": f"Process ran for {timeout}s (likely a server/long-running app)",
            "language": language,
            "method": "native",
            "error": None,
        }
    except FileNotFoundError as e:
        return {
            "exit_code": -1,
            "output": "",
            "language": language,
            "method": "native",
            "error": f"Command not found: {e}",
        }
    except Exception as e:
        return {
            "exit_code": -1,
            "output": "",
            "language": language,
            "method": "native",
            "error": str(e),
        }


def _find_native_cmd_python(project_dir: str) -> list[str]:
    import sys
    for name in ["main.py", "app.py", "__main__.py"]:
        if os.path.exists(os.path.join(project_dir, name)):
            return [sys.executable, name]
    return [sys.executable, "main.py"]


def _find_native_cmd_js(project_dir: str) -> list[str]:
    pkg_path = os.path.join(project_dir, "package.json")
    if os.path.exists(pkg_path):
        try:
            import json
            with open(pkg_path) as f:
                pkg = json.load(f)
            main = pkg.get("main", "index.js")
            return ["node", main]
        except Exception:
            pass
    for name in ["index.js", "main.js", "app.js"]:
        if os.path.exists(os.path.join(project_dir, name)):
            return ["node", name]
    return ["node", "index.js"]


def list_projects(output_base: str) -> list[dict]:
    """
    List available projects in the output directory.
    Returns list of {name, language, path, modified}.
    """
    projects = []
    base = Path(output_base)

    if not base.exists():
        return projects

    for d in sorted(base.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not d.is_dir():
            continue

        lang = detect_project_language(str(d))
        projects.append({
            "name": d.name,
            "language": lang,
            "path": str(d),
            "modified": d.stat().st_mtime,
        })

    return projects


# ============================================================================
# SCREENSHOT CAPTURE (HTML/Web projects)
# ============================================================================

# Browser paths to try on Windows
_BROWSER_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def _find_browser() -> str | None:
    """Find a headless-capable browser (Edge or Chrome) on Windows."""
    for path in _BROWSER_PATHS:
        if os.path.exists(path):
            return path
    # Try PATH-based lookup
    import shutil
    for name in ["msedge", "chrome", "google-chrome", "chromium"]:
        found = shutil.which(name)
        if found:
            return found
    return None


def capture_screenshot(
    project_dir: str,
    language: str = "auto",
    width: int = 1280,
    height: int = 900,
) -> str | None:
    """
    Capture a screenshot of an HTML/web project using headless Edge/Chrome.

    Args:
        project_dir: Path to the project directory
        language: Detected language (screenshot only for 'html')
        width: Browser viewport width
        height: Browser viewport height

    Returns:
        Path to the screenshot PNG file, or None if failed.
    """
    if language == "auto":
        language = detect_project_language(project_dir)

    if language != "html":
        return None

    # Find the HTML entry point
    html_file = None
    for name in ["index.html", "game.html", "main.html", "app.html"]:
        candidate = os.path.join(project_dir, name)
        if os.path.exists(candidate):
            html_file = candidate
            break

    if not html_file:
        log.warning("No HTML file found for screenshot")
        return None

    browser = _find_browser()
    if not browser:
        log.warning("No headless browser found (Edge/Chrome) for screenshot")
        return None

    # Screenshot output path
    screenshot_path = os.path.join(project_dir, ".screenshot.png")

    # Convert to file:// URL
    file_url = Path(html_file).as_uri()

    cmd = [
        browser,
        "--headless=new",
        f"--screenshot={screenshot_path}",
        f"--window-size={width},{height}",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--hide-scrollbars",
        file_url,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if os.path.exists(screenshot_path) and os.path.getsize(screenshot_path) > 0:
            log.info(f"Screenshot captured: {screenshot_path}")
            return screenshot_path
        else:
            log.warning(f"Screenshot file not created. Browser exit={result.returncode}")
            return None
    except subprocess.TimeoutExpired:
        log.warning("Screenshot capture timed out")
        return None
    except Exception as e:
        log.warning(f"Screenshot capture failed: {e}")
        return None
