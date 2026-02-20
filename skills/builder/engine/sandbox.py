"""
skills/builder/engine/sandbox.py - Sandbox tests
===============================================
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from skills.builder.engine.context import blog, read_file
from skills.builder.engine.deps import get_venv_python


def sandbox_test(output_dir: str, language: str) -> tuple[bool, str]:
    entry_commands = {
        "python": _find_python_entry(output_dir),
        "rust": ["cargo", "run"],
        "go": ["go", "run", "."],
        "javascript": _find_js_entry(output_dir),
        "typescript": _find_ts_entry(output_dir),
        "html": None,
    }

    # ── Always validate HTML references first (regardless of language) ──
    # This catches missing <script src> / <link href> files even when
    # the project also has a server component.
    html_issues: list[str] = []
    if _is_browser_project(output_dir):
        html_ok, html_msg = _validate_browser_project(output_dir)
        if not html_ok:
            html_issues.append(html_msg)
            blog.warning(f"Browser validation issues: {html_msg}")
        else:
            blog.verify(True, "browser_refs", html_msg)

    cmd = entry_commands.get(language)
    if not cmd:
        if _is_browser_project(output_dir):
            blog.phase("sandbox_test", f"Validating browser project ({language})")
            if html_issues:
                return False, html_issues[0]
            return True, "Browser project OK"
        return True, f"No sandbox test available for {language}"

    blog.phase("sandbox_test", f"Testing generated project ({language})")

    ports_before = _get_listening_ports()

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=output_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        deadline = time.time() + 30
        port_check_time = time.time() + 5
        new_port = None

        while time.time() < deadline:
            retcode = proc.poll()
            if retcode is not None:
                remaining = proc.stdout.read() if proc.stdout else ""
                output = remaining[:2000]

                if retcode == 0:
                    if html_issues:
                        return False, html_issues[0]
                    blog.verify(True, "sandbox", "Project ran successfully")
                    return True, output
                blog.error(f"Sandbox exit code {retcode}", severity="sandbox")
                return False, output

            if time.time() >= port_check_time and new_port is None:
                ports_after = _get_listening_ports()
                new_ports = ports_after - ports_before
                if new_ports:
                    new_port = min(new_ports)
                    blog.info(f"Port {new_port} detected, testing HTTP...")
                    http_ok, http_msg = _test_http_port(new_port)
                    if http_ok:
                        blog.verify(True, "sandbox", f"Web server on port {new_port}: {http_msg}")
                    else:
                        blog.info(f"Port {new_port} open but not HTTP: {http_msg}")
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    # Server started, but if HTML refs are broken the project won't work
                    if html_issues:
                        return False, html_issues[0]
                    return True, f"Server listening on port {new_port}"

                port_check_time = time.time() + 2

            time.sleep(0.5)

        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

        if html_issues:
            return False, html_issues[0]
        blog.verify(True, "sandbox", "Process ran 30s without crashing (likely OK)")
        return True, "Process ran for 30s without crashing (long-running app)"

    except FileNotFoundError as exc:
        blog.warning(f"Sandbox: command not found: {exc}")
        if html_issues:
            return False, html_issues[0]
        return True, f"Cannot test: {exc}"
    except Exception as exc:
        blog.error(f"Sandbox error: {exc}", severity="sandbox")
        return False, str(exc)


def _get_listening_ports() -> set[int]:
    try:
        import psutil
        ports = set()
        for conn in psutil.net_connections(kind="tcp"):
            if conn.status == "LISTEN":
                ports.add(conn.laddr.port)
        return ports
    except (ImportError, PermissionError, psutil.AccessDenied):
        return set()


def _test_http_port(port: int, timeout: float = 3.0) -> tuple[bool, str]:
    import urllib.request
    import urllib.error

    try:
        url = f"http://localhost:{port}/"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return True, f"HTTP {exc.code}"
    except Exception as exc:
        return False, str(exc)


def _is_browser_project(output_dir: str) -> bool:
    if os.path.exists(os.path.join(output_dir, "index.html")):
        return True
    # Also check common subdirs
    for sub in ("public", "static", "www", "dist"):
        if os.path.exists(os.path.join(output_dir, sub, "index.html")):
            return True
    return False


def _validate_browser_project(output_dir: str) -> tuple[bool, str]:
    # Find all HTML files (root + common subdirs)
    html_files: list[str] = []
    for root, _dirs, files in os.walk(output_dir):
        # Skip node_modules and .venv
        rel_root = os.path.relpath(root, output_dir)
        if any(skip in rel_root for skip in ("node_modules", ".venv", "__pycache__")):
            continue
        for f in files:
            if f.endswith((".html", ".htm")):
                html_files.append(os.path.join(root, f))

    if not html_files:
        return False, "No HTML files found"

    issues = []
    import re as _re

    for html_path in html_files:
        html = read_file(html_path)
        if not html.strip():
            issues.append(f"{os.path.relpath(html_path, output_dir)} is empty")
            continue

        html_dir = os.path.dirname(html_path)
        rel_html = os.path.relpath(html_path, output_dir)

        # Check script references
        script_srcs = _re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html, _re.IGNORECASE)
        for src in script_srcs:
            if src.startswith(("http://", "https://", "//", "data:")):
                continue
            js_path = os.path.join(html_dir, src)
            if not os.path.exists(js_path):
                issues.append(f"[{rel_html}] Referenced script missing: {src}")

        # Check CSS references
        css_hrefs = _re.findall(r'<link[^>]+href=["\']([^"\']+\.css)["\']', html, _re.IGNORECASE)
        for href in css_hrefs:
            if href.startswith(("http://", "https://", "//", "data:")):
                continue
            css_path = os.path.join(html_dir, href)
            if not os.path.exists(css_path):
                issues.append(f"[{rel_html}] Referenced stylesheet missing: {href}")

    if issues:
        return False, "Browser project issues: " + "; ".join(issues)

    js_files = [f for f in os.listdir(output_dir) if f.endswith(".js")]
    total_html = len(html_files)
    return True, f"Browser project OK ({total_html} HTML file(s), {len(js_files)} root JS files)"


def _find_python_entry(output_dir: str) -> list[str]:
    # Use venv Python if available so we have access to installed deps
    venv_python = get_venv_python(output_dir)
    for name in ["main.py", "app.py", "__main__.py", "src/main.py", "server.py", "run.py"]:
        if os.path.exists(os.path.join(output_dir, name)):
            return [venv_python, name]
    # Fallback: find any top-level .py file that isn't a test
    for f in sorted(os.listdir(output_dir)):
        if f.endswith(".py") and not f.startswith("test") and f != "setup.py":
            return [venv_python, f]
    return [venv_python, "main.py"]


def _find_js_entry(output_dir: str) -> list[str] | None:
    has_html = os.path.exists(os.path.join(output_dir, "index.html"))

    if has_html:
        server_patterns = ("server.js", "server.ts", "app.js")
        has_server = any(os.path.exists(os.path.join(output_dir, s)) for s in server_patterns)
        if not has_server:
            return None

    pkg_json = os.path.join(output_dir, "package.json")
    if os.path.exists(pkg_json):
        try:
            pkg = json.loads(read_file(pkg_json))
            main = pkg.get("main", "index.js")
            if not os.path.exists(os.path.join(output_dir, main)):
                for name in ["index.js", "main.js", "app.js", "server.js", "src/index.js"]:
                    if os.path.exists(os.path.join(output_dir, name)):
                        return ["node", name]
                return None
            return ["node", main]
        except Exception:
            pass
    for name in ["index.js", "main.js", "app.js", "server.js", "src/index.js"]:
        if os.path.exists(os.path.join(output_dir, name)):
            return ["node", name]
    return None


def _find_ts_entry(output_dir: str) -> list[str]:
    for name in ["index.ts", "main.ts", "src/index.ts", "app.ts"]:
        if os.path.exists(os.path.join(output_dir, name)):
            return ["npx", "ts-node", name]
    return ["npx", "ts-node", "index.ts"]
