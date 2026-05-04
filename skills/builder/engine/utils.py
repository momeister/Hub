"""
skills/builder/engine/utils.py - Small helpers
==============================================
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from skills.builder.engine.context import blog, strip_fences


def generate_project_name(goal: str) -> str:
    """Generate a clean project folder name from goal description."""
    goal = goal.lower()
    for phrase in [
        "bau mir", "erstelle", "schreib", "entwickle", "programmiere",
        "mach mir", "create", "build", "write", "develop",
    ]:
        goal = goal.replace(phrase, "")
    goal = re.sub(r"[^\w\s-]", "", goal)
    goal = re.sub(r"[-\s]+", "_", goal)
    goal = goal.strip("_")
    goal = goal[:50]
    if len(goal) < 3:
        goal = f"project_{int(time.time())}"
    return goal


def detect_language_from_ext(path: str) -> str:
    """Detect programming language from file extension."""
    ext = Path(path).suffix.lower()
    mapping = {
        ".rs": "rust", ".go": "go", ".py": "python",
        ".js": "javascript", ".jsx": "javascript",
        ".ts": "typescript", ".tsx": "typescript",
        ".java": "java", ".cs": "csharp",
        ".cpp": "cpp", ".c": "c",
        ".html": "html", ".css": "css",
        ".rb": "ruby", ".php": "php",
        ".swift": "swift", ".kt": "kotlin",
    }
    return mapping.get(ext, "unknown")


def sanitize_skeleton_paths(skeletons: dict, blueprint: dict) -> dict:
    """Strip subproject/project name prefix from skeleton paths.

    When generating skeletons for a subproject (e.g. 'backend'), the LLM
    often outputs paths like 'backend/main.py' instead of 'main.py'.
    Since the output directory is already the subproject folder, writing
    the prefixed path creates nested folders: output/backend/backend/main.py.

    This function strips the project_name prefix so files land in the
    correct directory.
    """
    project_name = blueprint.get("project_name", "")
    if not project_name:
        return skeletons

    prefix = project_name + "/"
    planned_paths = {f["path"] for f in blueprint.get("files", [])}

    sanitized = {}
    stripped_count = 0

    for path, content in skeletons.items():
        if path.startswith(prefix):
            stripped = path[len(prefix):]
            # If the unprefixed version already exists in skeletons, skip the prefixed duplicate
            if stripped in skeletons:
                stripped_count += 1
                continue
            sanitized[stripped] = content
            stripped_count += 1
            continue

        # Also catch double-nesting: backend/backend/main.py -> main.py
        double_prefix = prefix + prefix
        if path.startswith(double_prefix):
            stripped = path[len(double_prefix):]
            if stripped not in sanitized:
                sanitized[stripped] = content
                stripped_count += 1
                continue

        sanitized[path] = content

    if stripped_count:
        blog.warning(
            f"Sanitized skeleton paths: stripped '{prefix}' prefix from "
            f"{stripped_count} path(s) to prevent nested subproject folders"
        )

    return sanitized


def parse_multi_file_output(text: str) -> dict:
    """
    Parse LLM output with multiple files in format:
    === path/to/file.ext ===
    [content]
    === END ===

    Also handles variations:
    --- path/to/file.ext ---
    ```filename.ext

    Returns: {path: content}
    """
    files = {}

    # Primary format: === path === ... === END ===
    pattern = r"===\s*(.+?)\s*===\s*\n(.*?)\n===\s*(?:END|end)\s*==="
    matches = list(re.finditer(pattern, text, re.DOTALL))

    # Fallback 1: --- path --- ... --- END ---
    if not matches:
        pattern2 = r"---\s*(.+?)\s*---\s*\n(.*?)\n---\s*(?:END|end)\s*---"
        matches = list(re.finditer(pattern2, text, re.DOTALL))

    # Fallback 2: ```filename.ext ... ```
    if not matches:
        pattern3 = r"```([\w./\\-]+\.[\w]+)\s*\n(.*?)```"
        matches = list(re.finditer(pattern3, text, re.DOTALL))

    # Fallback 3: Split on === path === without explicit END markers
    if not matches:
        header_pattern = r"===\s*([\w./\\-]+\.[\w]+)\s*==="
        headers = list(re.finditer(header_pattern, text))
        if len(headers) >= 2:
            for i, header in enumerate(headers):
                path = header.group(1).strip()
                start = header.end()
                end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
                content = text[start:end].strip()
                # Remove trailing === END === if present
                content = re.sub(r"\n===\s*(?:END|end)\s*===\s*$", "", content).strip()
                if path and ("." in path or "/" in path):
                    files[path] = strip_fences(content)
            if files:
                return files

    for match in matches:
        path = match.group(1).strip()
        content = match.group(2).strip()
        if "." in path or "/" in path:
            files[path] = strip_fences(content)

    if not files:
        blog.warning("parse_multi_file_output: No files found in LLM output, attempting line-by-line parse")
        # Last resort: look for file path headers followed by code
        current_path = None
        current_lines = []
        for line in text.split("\n"):
            # Check if line looks like a file header
            header_match = re.match(r"^[=#-]+\s*([\w./\\-]+\.[\w]+)\s*[=#-]*$", line.strip())
            if header_match:
                if current_path and current_lines:
                    content = "\n".join(current_lines).strip()
                    files[current_path] = strip_fences(content)
                current_path = header_match.group(1).strip()
                current_lines = []
            elif current_path is not None:
                if line.strip().lower() in ("end", "=== end ===", "--- end ---"):
                    if current_lines:
                        content = "\n".join(current_lines).strip()
                        files[current_path] = strip_fences(content)
                    current_path = None
                    current_lines = []
                else:
                    current_lines.append(line)
        if current_path and current_lines:
            content = "\n".join(current_lines).strip()
            files[current_path] = strip_fences(content)

    if not files:
        blog.error("parse_multi_file_output: No files found in LLM output after all fallbacks!")

    return files
