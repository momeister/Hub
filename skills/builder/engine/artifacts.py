"""
skills/builder/engine/artifacts.py - README and start scripts
=============================================================
"""

from __future__ import annotations

import json
import os

from skills.builder.engine.context import blog, write_file


def generate_readme(
    goal: str,
    blueprint: dict,
    files: dict,
    output_dir: str,
    manager_model: str,
    coder_model: str,
) -> str:
    """Generate comprehensive README."""
    language = blueprint["language"]
    framework = blueprint.get("framework", "")
    why = blueprint.get("why", "Optimal for this use case")

    # Detect actual entry point for Python
    python_entry = "main.py"
    if language == "python" and files:
        file_paths = set(files.keys())
        for candidate in ["main.py", "app.py", "src/main.py", "__main__.py", "server.py", "run.py"]:
            if candidate in file_paths:
                python_entry = candidate
                break
        else:
            for fp in sorted(file_paths):
                if fp.endswith(".py") and "/" not in fp and not fp.startswith("test"):
                    python_entry = fp
                    break

    start_instructions = {
        "python": f"```bash\npython -m venv .venv\n.venv\\Scripts\\activate  # Windows\npip install -r requirements.txt\npython {python_entry}\n```",
        "rust": "```bash\ncargo build\ncargo run\n```",
        "go": "```bash\ngo mod tidy\ngo run .\n```",
        "typescript": "```bash\nnpm install\nnpm run dev\n```",
        "javascript": "```bash\nnpm install\nnpm start\n```",
        "html": "Open `index.html` in a web browser, or:\n```bash\npython -m http.server 8000\n```\nThen open: http://localhost:8000",
        "java": "```bash\nmvn spring-boot:run\n```",
        "csharp": "```bash\ndotnet build\ndotnet run\n```",
    }

    start_section = start_instructions.get(language, f"See {language} documentation for build instructions.")
    file_list = "\n".join(f"- `{path}`" for path in sorted(files.keys())[:30])

    arch_section = ""
    decisions = blueprint.get("architecture_decisions", [])
    if decisions:
        arch_lines = "\n".join(f"- **{d['decision']}**: {d['reasoning']}" for d in decisions)
        arch_section = f"\n## Architecture Decisions\n\n{arch_lines}\n"

    readme = f"""# {goal.split('.')[0].strip() if '.' in goal else goal[:80]}

> Built by AI Builder v3

## Tech Stack

**Language:** {language.title()}
**Framework:** {framework if framework else 'N/A'}

**Why this stack?**
{why}

## Start

{start_section}
{arch_section}## Project Structure

{file_list}

## Development

**Manager Model:** `{manager_model}`
**Coder Model:** `{coder_model}`
**Context Window:** 128k tokens

---

Built with AI Builder v3
"""

    readme_path = os.path.join(output_dir, "README.md")
    write_file(readme_path, readme)

    return readme


def generate_multi_language_readme(goal: str, subprojects: list[dict], output_dir: str) -> str:
    """Generate README for multi-language projects."""
    sub_instructions = []
    for i, sub in enumerate(subprojects, 1):
        name = sub["name"]
        lang = sub["language"]
        path = sub.get("path", name)

        cmds = {
            "rust": f"cd {path} && cargo run",
            "typescript": f"cd {path} && npm install && npm run dev",
            "javascript": f"cd {path} && npm install && npm start",
            "go": f"cd {path} && go run .",
            "python": f"cd {path} && pip install -r requirements.txt && python main.py",
        }
        cmd = cmds.get(lang, f"cd {path} && <start command>")

        sub_instructions.append(
            f"**Terminal {i} - {name.title()} ({lang.title()}):**\n```bash\n{cmd}\n```"
        )

    instructions = "\n\n".join(sub_instructions)
    sub_list = "\n".join(
        f"- **{sub['name'].title()}**: {sub['language'].title()} ({sub.get('framework', 'N/A')})"
        for sub in subprojects
    )

    readme = f"""# {goal[:80]}

> Multi-Language Project - Built by AI Builder v3

## Architecture

{sub_list}

## Start

{instructions}

---

Built with AI Builder v3
"""

    write_file(os.path.join(output_dir, "README.md"), readme)
    return readme


def generate_project_start_bat(
    blueprint: dict,
    output_dir: str,
    files: dict,
    subprojects: list[dict] | None = None,
) -> str:
    """Generate a project_start.bat for quick launch."""
    language = blueprint.get("language", "python")
    project_name = blueprint.get("project_name", "project")

    if subprojects:
        bat = _generate_multi_start_bat(project_name, subprojects)
    else:
        bat = _generate_single_start_bat(project_name, language, files)

    bat_path = os.path.join(output_dir, "project_start.bat")
    write_file(bat_path, bat)
    blog.info(f"Generated project_start.bat for {language} project")
    return bat_path


def _generate_single_start_bat(project_name: str, language: str, files: dict) -> str:
    title = project_name.replace("_", " ").title()

    file_paths = set(files.keys()) if files else set()

    if language == "python":
        entry = None
        for candidate in ["main.py", "app.py", "src/main.py", "__main__.py", "server.py", "run.py"]:
            if candidate in file_paths:
                entry = candidate
                break
        # If none of the standard names match, find any .py file that looks like an entry point
        if not entry:
            for fp in sorted(file_paths):
                if fp.endswith(".py") and "/" not in fp and not fp.startswith("test"):
                    entry = fp
                    break
        if not entry:
            entry = "main.py"
        return (
            f"@echo off\n"
            f"title {title}\n"
            f"echo === {title} ===\n"
            f"echo.\n"
            f"if not exist .venv (\n"
            f"    echo Creating virtual environment...\n"
            f"    python -m venv .venv\n"
            f")\n"
            f"call .venv\\Scripts\\activate.bat\n"
            f"if exist requirements.txt (\n"
            f"    echo Installing dependencies...\n"
            f"    pip install -r requirements.txt --quiet\n"
            f")\n"
            f"echo.\n"
            f"echo Starting {title}...\n"
            f"echo.\n"
            f"python {entry}\n"
            f"echo.\n"
            f"pause\n"
        )

    if language == "rust":
        return (
            f"@echo off\n"
            f"title {title}\n"
            f"echo === {title} ===\n"
            f"echo.\n"
            f"echo Building project (release mode)...\n"
            f"cargo build --release\n"
            f"if errorlevel 1 (\n"
            f"    echo.\n"
            f"    echo BUILD FAILED\n"
            f"    pause\n"
            f"    exit /b 1\n"
            f")\n"
            f"echo.\n"
            f"echo Starting {title}...\n"
            f"echo.\n"
            f"target\\release\\{project_name}.exe\n"
            f"echo.\n"
            f"pause\n"
        )

    if language == "go":
        return (
            f"@echo off\n"
            f"title {title}\n"
            f"echo === {title} ===\n"
            f"echo.\n"
            f"echo Resolving dependencies...\n"
            f"go mod tidy\n"
            f"echo Building project...\n"
            f"go build -o {project_name}.exe .\n"
            f"if errorlevel 1 (\n"
            f"    echo.\n"
            f"    echo BUILD FAILED\n"
            f"    pause\n"
            f"    exit /b 1\n"
            f")\n"
            f"echo.\n"
            f"echo Starting {title}...\n"
            f"echo.\n"
            f"{project_name}.exe\n"
            f"echo.\n"
            f"pause\n"
        )

    if language in ("javascript", "typescript"):
        start_cmd = "npm start"
        pkg_json_path = None
        for candidate in ["package.json"]:
            if candidate in file_paths:
                pkg_json_path = candidate
                break
        if pkg_json_path and pkg_json_path in files:
            try:
                pkg = json.loads(files[pkg_json_path])
                scripts = pkg.get("scripts", {})
                if "dev" in scripts:
                    start_cmd = "npm run dev"
                elif "start" in scripts:
                    start_cmd = "npm start"
            except Exception:
                pass

        return (
            f"@echo off\n"
            f"title {title}\n"
            f"echo === {title} ===\n"
            f"echo.\n"
            f"if not exist node_modules (\n"
            f"    echo Installing dependencies...\n"
            f"    npm install\n"
            f")\n"
            f"echo.\n"
            f"echo Starting {title}...\n"
            f"echo.\n"
            f"{start_cmd}\n"
            f"echo.\n"
            f"pause\n"
        )

    if language == "html":
        return (
            f"@echo off\n"
            f"title {title}\n"
            f"echo === {title} ===\n"
            f"echo.\n"
            f"echo Opening in browser...\n"
            f"start \"\" index.html\n"
            f"echo.\n"
            f"echo Project opened in your default browser.\n"
            f"pause\n"
        )

    if language in ("java", "kotlin"):
        return (
            f"@echo off\n"
            f"title {title}\n"
            f"echo === {title} ===\n"
            f"echo.\n"
            f"if exist pom.xml (\n"
            f"    echo Building with Maven...\n"
            f"    mvn package -q\n"
            f"    if errorlevel 1 (echo BUILD FAILED & pause & exit /b 1)\n"
            f"    java -jar target\\*.jar\n"
            f") else if exist build.gradle (\n"
            f"    echo Building with Gradle...\n"
            f"    gradle build -q\n"
            f"    if errorlevel 1 (echo BUILD FAILED & pause & exit /b 1)\n"
            f"    gradle run\n"
            f") else (\n"
            f"    echo No build tool found.\n"
            f")\n"
            f"echo.\n"
            f"pause\n"
        )

    if language == "csharp":
        return (
            f"@echo off\n"
            f"title {title}\n"
            f"echo === {title} ===\n"
            f"echo.\n"
            f"echo Building project...\n"
            f"dotnet build\n"
            f"if errorlevel 1 (echo BUILD FAILED & pause & exit /b 1)\n"
            f"echo.\n"
            f"echo Starting {title}...\n"
            f"echo.\n"
            f"dotnet run\n"
            f"echo.\n"
            f"pause\n"
        )

    return (
        f"@echo off\n"
        f"title {title}\n"
        f"echo === {title} ===\n"
        f"echo.\n"
        f"echo Language: {language}\n"
        f"echo Please check README.md for start instructions.\n"
        f"echo.\n"
        f"pause\n"
    )


def _generate_multi_start_bat(project_name: str, subprojects: list[dict]) -> str:
    title = project_name.replace("_", " ").title()

    lines = [
        "@echo off",
        f"title {title}",
        f"echo === {title} ===",
        "echo.",
    ]

    start_cmds = {
        "rust": "cargo run",
        "go": "go run .",
        "python": "pip install -r requirements.txt --quiet && python main.py",
        "javascript": "npm install && npm start",
        "typescript": "npm install && npm run dev",
    }

    for i, sub in enumerate(subprojects):
        name = sub.get("name", f"service_{i}")
        lang = sub.get("language", "unknown")
        cmd = start_cmds.get(lang, f"echo No start command for {lang}")
        label = name.title()
        delay = "timeout /t 3 >nul" if i > 0 else ""

        if delay:
            lines.append(delay)
        lines.append(f"echo Starting {label} ({lang})...")
        lines.append(f'start "{label}" cmd /k "cd {name} && {cmd}"')

    lines.extend([
        "",
        "echo.",
        f"echo All {len(subprojects)} services started in separate windows.",
        "echo Close this window when done.",
        "echo.",
        "pause",
    ])

    return "\n".join(lines) + "\n"
