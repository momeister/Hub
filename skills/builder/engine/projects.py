"""
skills/builder/engine/projects.py - Project listing and edit mode
=================================================================
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from skills.builder.engine.compile_checks import compile_check
from skills.builder.engine.context import blog, llm_call, clean_json, strip_fences, write_file, DEFAULT_CTX_TOKENS, MAX_SANDBOX_RETRIES
from skills.builder.engine.sandbox import sandbox_test
from skills.builder.engine.self_correct import self_correct

_TEXT_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".scss", ".less",
    ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".md", ".txt",
    ".sh", ".bat", ".ps1", ".rs", ".go", ".java", ".c", ".cpp", ".h",
    ".hpp", ".cs", ".rb", ".php", ".sql", ".xml", ".svg", ".vue",
    ".svelte", ".astro", ".mjs", ".cjs", ".env", ".gitignore",
    ".dockerfile", ".conf", ".lock", ".gradle", ".kt", ".swift",
    ".r", ".lua", ".dart", ".zig", ".nim", ".ex", ".exs", ".erl",
}

_SKIP_DIRS = {
    "node_modules", "__pycache__", ".git", ".venv", "venv", "env",
    ".idea", ".vscode", "dist", "build", ".next", "target",
    ".tox", ".mypy_cache", ".pytest_cache", "egg-info",
}

_LANG_MAP = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".jsx": "javascript", ".tsx": "typescript", ".rs": "rust",
    ".go": "go", ".java": "java", ".c": "c", ".cpp": "cpp",
    ".cs": "csharp", ".rb": "ruby", ".php": "php", ".dart": "dart",
    ".swift": "swift", ".kt": "kotlin", ".lua": "lua", ".zig": "zig",
    ".html": "html/javascript", ".vue": "vue", ".svelte": "svelte",
}


def _read_project_files(project_dir: str, max_file_size: int = 50_000) -> dict[str, str]:
    files: dict[str, str] = {}
    base = Path(project_dir)

    for fpath in sorted(base.rglob("*")):
        if not fpath.is_file():
            continue

        parts = fpath.relative_to(base).parts
        if any(p in _SKIP_DIRS or p.startswith(".") for p in parts[:-1]):
            continue

        ext = fpath.suffix.lower()
        if ext not in _TEXT_EXTENSIONS and ext != "":
            continue

        try:
            size = fpath.stat().st_size
        except OSError:
            continue
        if size > max_file_size or size == 0:
            continue

        rel = str(fpath.relative_to(base)).replace("\\", "/")
        try:
            files[rel] = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

    return files


def _detect_language(files: dict[str, str]) -> str:
    ext_count: dict[str, int] = {}
    for fpath in files:
        ext = Path(fpath).suffix.lower()
        lang = _LANG_MAP.get(ext)
        if lang:
            ext_count[lang] = ext_count.get(lang, 0) + 1

    if not ext_count:
        return "unknown"
    return max(ext_count, key=ext_count.get)


def list_projects(output_base: str) -> list[dict]:
    base = Path(output_base)
    if not base.exists():
        return []

    projects = []
    for d in sorted(base.iterdir()):
        if not d.is_dir() or d.name.startswith(("_", ".")):
            continue
        all_files = [f for f in d.rglob("*") if f.is_file()]
        total_size = sum(f.stat().st_size for f in all_files if f.exists())

        exts = {}
        for f in all_files:
            ext = f.suffix.lower()
            lang = _LANG_MAP.get(ext)
            if lang:
                exts[lang] = exts.get(lang, 0) + 1
        primary_lang = max(exts, key=exts.get) if exts else "?"

        projects.append({
            "name": d.name,
            "language": primary_lang,
            "files_count": len(all_files),
            "size_kb": round(total_size / 1024, 1),
            "modified": d.stat().st_mtime,
        })

    projects.sort(key=lambda p: p["modified"], reverse=True)
    return projects


def edit_project(
    goal: str,
    project_dir: str,
    manager_model: str,
    coder_model: str,
    ctx_tokens: int = DEFAULT_CTX_TOKENS,
) -> None:
    start_time = time.time()

    blog.info("=" * 70)
    blog.info("AI BUILDER v3 - EDIT MODE")
    blog.info("=" * 70)
    blog.info(f"Edit goal : {goal}")
    blog.info(f"Project   : {project_dir}")
    blog.info(f"Manager   : {manager_model}")
    blog.info(f"Coder     : {coder_model}")

    blog.phase("read_project", "Reading existing project files")

    files = _read_project_files(project_dir)
    if not files:
        blog.error("No source files found in project directory", severity="fatal")
        blog.complete(success=False, files_written=0, elapsed_sec=0)
        return

    language = _detect_language(files)
    blog.info(f"Found {len(files)} files, language: {language}")

    file_listing = []
    for fpath, content in sorted(files.items()):
        lines = content.splitlines()
        file_listing.append(f"=== {fpath} ({len(lines)} lines) ===\n{content}")

    project_context = "\n\n".join(file_listing)
    if len(project_context) > 20000:
        file_listing_trimmed = []
        total = 0
        for fpath, content in sorted(files.items(), key=lambda x: len(x[1])):
            chunk = f"=== {fpath} ({len(content.splitlines())} lines) ===\n{content}"
            if total + len(chunk) > 20000:
                remaining = 20000 - total
                if remaining > 200:
                    file_listing_trimmed.append(chunk[:remaining] + "\n... (truncated)")
                break
            file_listing_trimmed.append(chunk)
            total += len(chunk)
        project_context = "\n\n".join(file_listing_trimmed)

    blog.phase("plan_edits", "Manager analyzing project and planning changes", model=manager_model)

    plan_prompt = f"""You are a senior developer. An existing {language} project needs modifications.

EDIT REQUEST: "{goal}"

CURRENT PROJECT FILES:
{project_context}

Analyze the project and create a precise edit plan. For each file that needs changes,
describe EXACTLY what needs to be modified.

RULES:
  1. Only modify files that actually need changes for this request
  2. You may also create NEW files if the request requires them
  3. Do NOT touch files that are unrelated to the edit request
  4. Be specific: describe the exact changes, not vague instructions
  5. Maximum 10 file changes per edit (keep it focused)
  6. Preserve the project's existing style, patterns, and conventions

Output ONLY this JSON:
""" + """{
  "analysis": "<1-2 sentence summary of what the project does and current state>",
  "plan": [
    {
      "file": "<relative file path>",
      "action": "modify" or "create",
      "description": "<specific description of what to change/create and why>"
    }
  ]
}

Output ONLY JSON."""

    try:
        plan_response = llm_call(
            model=manager_model,
            prompt=plan_prompt,
            system="Senior developer. Analyze existing code. Plan precise, minimal edits. Output only valid JSON.",
            max_tokens=4096,
            temperature=0.15,
        )

        plan = json.loads(clean_json(plan_response))
        if not isinstance(plan, dict) or "plan" not in plan:
            blog.error("Manager returned invalid plan", severity="fatal")
            blog.complete(success=False, files_written=0, elapsed_sec=0)
            return

        changes = plan.get("plan", [])
        analysis = plan.get("analysis", "")

        if analysis:
            blog.info(f"Analysis: {analysis}")

        if not changes:
            blog.info("Manager says no changes needed")
            blog.complete(success=True, files_written=0, elapsed_sec=int(time.time() - start_time))
            return

        blog.info(f"Planned {len(changes)} file change(s):")
        for c in changes:
            blog.info(f"  [{c.get('action','?')}] {c.get('file','?')} - {c.get('description','')[:80]}")

        blog.plan(
            files_total=len(changes),
            file_paths=[c.get("file", "?") for c in changes],
        )

    except Exception as exc:
        blog.error(f"Edit planning failed: {exc}", severity="fatal")
        blog.complete(success=False, files_written=0, elapsed_sec=0)
        return

    blog.phase("apply_edits", "Coder applying changes", model=coder_model)

    files_modified = 0
    for i, change in enumerate(changes[:10]):
        target_file = change.get("file", "")
        action = change.get("action", "modify")
        description = change.get("description", "")

        if not target_file:
            continue

        blog.file_start(path=target_file, index=i + 1, total=len(changes))

        if action == "modify":
            if target_file not in files:
                for fpath in files:
                    if os.path.basename(fpath) == os.path.basename(target_file):
                        target_file = fpath
                        break
                else:
                    blog.warning(f"File not found: {target_file}, skipping")
                    continue

            current_code = files[target_file]

            context_files = []
            for fpath, content in files.items():
                if fpath != target_file:
                    preview = "\n".join(content.splitlines()[:30])
                    context_files.append(f"--- {fpath} ---\n{preview}")
            other_context = "\n\n".join(context_files[:5])
            if len(other_context) > 6000:
                other_context = other_context[:6000] + "\n...(truncated)"

            edit_prompt = f"""Modify this file according to the instruction below.

INSTRUCTION: {description}

OVERALL GOAL: {goal}

FILE TO MODIFY: {target_file}

CURRENT CODE:
{current_code}

OTHER PROJECT FILES (for reference):
{other_context}

RULES:
  1. Output the COMPLETE modified file - not just the changed parts
  2. Do NOT break existing functionality that is unrelated to the edit
  3. Do NOT add new external dependencies unless the instruction specifically requires it
  4. Preserve the existing code style and conventions
  5. Output ONLY code, no markdown fences, no explanations"""

            try:
                response = llm_call(
                    model=coder_model,
                    prompt=edit_prompt,
                    system=f"Expert {language} developer. Apply the requested edit cleanly. Output ONLY the complete modified file.",
                    max_tokens=14336,
                    temperature=0.1,
                )

                new_code = strip_fences(response)

                if len(new_code) < len(current_code) * 0.3:
                    blog.warning(
                        f"Edit shrank {target_file} too much ({len(new_code)} vs {len(current_code)}), skipping"
                    )
                    continue

                full_path = os.path.join(project_dir, target_file)
                write_file(full_path, new_code)
                files[target_file] = new_code
                files_modified += 1

                blog.file_done(path=target_file, chars=len(new_code))
                blog.info(f"Modified: {target_file}")

            except Exception as exc:
                blog.warning(f"Failed to modify {target_file}: {exc}")
                blog.file_done(path=target_file, chars=len(current_code))

        elif action == "create":
            context_files = []
            for fpath, content in list(files.items())[:5]:
                preview = "\n".join(content.splitlines()[:30])
                context_files.append(f"--- {fpath} ---\n{preview}")
            other_context = "\n\n".join(context_files)
            if len(other_context) > 6000:
                other_context = other_context[:6000] + "\n...(truncated)"

            create_prompt = f"""Create a new file for an existing project.

INSTRUCTION: {description}

OVERALL GOAL: {goal}

NEW FILE PATH: {target_file}

EXISTING PROJECT FILES (for reference):
{other_context}

RULES:
  1. Output ONLY the file content
  2. Match the coding style of existing files
  3. Do NOT add unnecessary dependencies
  4. Output ONLY code, no markdown fences, no explanations"""

            try:
                response = llm_call(
                    model=coder_model,
                    prompt=create_prompt,
                    system=f"Expert {language} developer. Create the file cleanly. Output ONLY code.",
                    max_tokens=14336,
                    temperature=0.1,
                )

                new_code = strip_fences(response)
                full_path = os.path.join(project_dir, target_file)
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                write_file(full_path, new_code)
                files[target_file] = new_code
                files_modified += 1

                blog.file_done(path=target_file, chars=len(new_code))
                blog.info(f"Created: {target_file}")

            except Exception as exc:
                blog.warning(f"Failed to create {target_file}: {exc}")
                blog.file_done(path=target_file, chars=0)

    if files_modified == 0:
        blog.warning("No files were modified")
        blog.complete(success=False, files_written=0, elapsed_sec=int(time.time() - start_time))
        return

    blog.info(f"Applied {files_modified} change(s)")

    blog.phase("compile_check", f"Checking compilation ({language})")

    success, errors = compile_check(project_dir, language)
    if success:
        blog.verify(True, "edit_compile", "Project compiles after edits")
    else:
        blog.verify(False, "edit_compile", f"{len(errors)} compile errors after edits")
        for err in errors[:10]:
            blog.error(err, severity="compile")

        blog.phase("self_correct", "Attempting to fix compile errors")
        err_text = "\n".join(errors[:20])
        files = self_correct(project_dir, language, coder_model, files, err_text)

        success2, errors2 = compile_check(project_dir, language)
        if success2:
            blog.verify(True, "edit_compile_retry", "Compiles after self-correction")
        else:
            blog.warning(f"Still {len(errors2)} errors after self-correction")

    blog.phase("sandbox_test", "Running sandbox test")
    for sandbox_attempt in range(MAX_SANDBOX_RETRIES):
        test_ok, test_output = sandbox_test(project_dir, language)
        if test_ok:
            blog.verify(True, "edit_sandbox", f"Sandbox OK (attempt {sandbox_attempt + 1}): {test_output[:100]}")
            break
        blog.warning(
            f"Sandbox failed (attempt {sandbox_attempt + 1}/{MAX_SANDBOX_RETRIES}): {test_output[:200]}"
        )
        files = self_correct(project_dir, language, coder_model, files, test_output)
    else:
        blog.warning("Edit sandbox failed all self-correction attempts")

    elapsed = int(time.time() - start_time)
    blog.complete(
        success=True,
        files_written=files_modified,
        elapsed_sec=elapsed,
        output_dir=project_dir,
    )
