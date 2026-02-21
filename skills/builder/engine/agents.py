"""
skills/builder/engine/agents.py - Sequential Agent Architecture
================================================================
Five agents that run sequentially (one at a time to fit in VRAM):
  1. Planner   - Architect + blueprint design (manager model)
  2. Retriever - Gathers context, validates deps, ensures critical files
  3. Coder     - Skeleton generation + file-by-file fill-in (coder model)
  4. Executor  - Runs the project, checks if it starts, sandbox tests
  5. Critic    - Reviews output, suggests fixes, triggers repair loops

Each agent receives the workspace dict and returns an updated version.
Only one LLM model is loaded at a time (sequential, not parallel).
"""

from __future__ import annotations

import json
import os
import time

from skills.builder.engine.context import (
    blog, llm_call, clean_json, strip_fences, write_file,
    validate_blueprint, DEFAULT_CTX_TOKENS, MAX_REPAIR_ATTEMPTS, MAX_SANDBOX_RETRIES,
)
from skills.builder.engine.blueprint import architect_phase
from skills.builder.engine.compile_checks import compile_check
from skills.builder.engine.critical_files import ensure_critical_files
from skills.builder.engine.deps import install_deps, get_venv_python
from skills.builder.engine.formatters import format_code
from skills.builder.engine.manifest import validate_manifest
from skills.builder.engine.repair import repair_file, patch_repair_file
from skills.builder.engine.error_analysis import analyze_errors
from skills.builder.engine.sandbox import sandbox_test
from skills.builder.engine.skeletons import generate_all_skeletons, fill_in_file, _generate_single_skeleton


# ---------------------------------------------------------------------------
# Helper: ensure Python entry points are runnable
# ---------------------------------------------------------------------------
def _ensure_python_entry_points(output_dir: str, written_files: dict) -> None:
    """Ensure Python entry point files (main.py, app.py, server.py) have
    if __name__ == '__main__' blocks so they can be run with 'python main.py'.

    For FastAPI/Flask/Starlette apps: adds uvicorn.run() block.
    For plain scripts: no change needed (they run inline).
    """
    entry_candidates = ["main.py", "app.py", "server.py", "run.py"]

    for entry in entry_candidates:
        fpath = os.path.join(output_dir, entry)
        if not os.path.exists(fpath):
            continue

        try:
            content = open(fpath, "r", encoding="utf-8").read()
        except Exception:
            continue

        # Check if the file uses a web framework
        uses_fastapi = "FastAPI" in content or "from fastapi" in content
        uses_flask = "Flask(" in content or "from flask" in content
        uses_starlette = "Starlette(" in content or "from starlette" in content

        has_main_block = 'if __name__' in content

        if has_main_block:
            continue  # Already has entry point

        if uses_fastapi or uses_starlette:
            # Detect app variable name
            app_var = "app"
            for line in content.splitlines():
                if "FastAPI(" in line or "Starlette(" in line:
                    parts = line.split("=")
                    if len(parts) >= 2:
                        app_var = parts[0].strip()
                    break

            block = (
                f"\n\nif __name__ == \"__main__\":\n"
                f"    import uvicorn\n"
                f"    uvicorn.run({app_var}, host=\"127.0.0.1\", port=8000)\n"
            )
            content += block
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(content)
            if entry in written_files:
                written_files[entry] = content
            blog.info(f"Added uvicorn entry point to {entry}")

        elif uses_flask:
            # Detect app variable name
            app_var = "app"
            for line in content.splitlines():
                if "Flask(" in line:
                    parts = line.split("=")
                    if len(parts) >= 2:
                        app_var = parts[0].strip()
                    break

            block = (
                f"\n\nif __name__ == \"__main__\":\n"
                f"    {app_var}.run(host=\"127.0.0.1\", port=8000, debug=True)\n"
            )
            content += block
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(content)
            if entry in written_files:
                written_files[entry] = content
            blog.info(f"Added Flask entry point to {entry}")


# ---------------------------------------------------------------------------
# Workspace: shared state passed between agents
# ---------------------------------------------------------------------------
def make_workspace(
    goal: str,
    manager_model: str,
    coder_model: str,
    output_dir: str,
    ctx_tokens: int = DEFAULT_CTX_TOKENS,
    review_model: str = "",
) -> dict:
    """Create initial workspace for the agent pipeline."""
    return {
        "goal": goal,
        "manager_model": manager_model,
        "coder_model": coder_model,
        "output_dir": output_dir,
        "ctx_tokens": ctx_tokens,
        "review_model": review_model,
        "blueprint": None,
        "skeletons": {},
        "written_files": {},
        "language": "",
        "compile_ok": False,
        "sandbox_ok": False,
        "errors": [],
        "phase": "init",
        "start_time": time.time(),
    }


# ---------------------------------------------------------------------------
# Agent 1: PLANNER
# ---------------------------------------------------------------------------
def agent_planner(ws: dict) -> dict:
    """Architect phase: analyze goal, pick tech stack, design file structure.

    Uses the manager model (reasoning-heavy, loaded first then unloaded).
    """
    blog.phase("agent_planner", "Agent 1/5: PLANNER - Designing architecture", model=ws["manager_model"])
    ws["phase"] = "planner"

    blueprint = architect_phase(ws["goal"], ws["manager_model"])
    # Store the goal in the blueprint so coder agents have access to it
    blueprint["_goal"] = ws["goal"]

    # Critic review of blueprint (still using manager model while loaded)
    blog.phase("planner_review", "Planner reviewing own blueprint", model=ws["manager_model"])

    language = blueprint.get("language", "unknown")
    files = blueprint.get("files", [])
    dep_order = blueprint.get("dependency_order", [])

    file_summary = "\n".join(
        f"  - {f['path']}: {f.get('purpose', '?')}" for f in files[:25]
    )
    dep_content = blueprint.get("dependencies", {}).get("content", "")[:500]

    # Fix 7: Negative examples for common blueprint mistakes
    negative_examples = """
KNOWN BLUEPRINT MISTAKES (check for these specifically):
- Game requested but no interactive event handlers (click, keydown) in file list
- "move history" feature but no component/file/function for tracking moves
- "AI opponent" / "bot" requested but no AI logic file
- Multi-file project but no __init__.py or index file connecting modules
- Python web API without uvicorn/startup entry point
- HTML project with <script src="x.js"> but x.js not in file list
- "save/load" feature requested but no file persistence logic planned
- "user authentication" but no session/token management file
"""

    review_prompt = f"""You are a code project reviewer. A blueprint was generated for this goal:

GOAL: "{ws['goal']}"

BLUEPRINT SUMMARY:
  Language: {language}
  Framework: {blueprint.get('framework', 'none')}
  Files ({len(files)}):
{file_summary}
  Dependency order: {dep_order[:20]}
  Dependencies: {dep_content[:300]}

{negative_examples}

Review for these issues ONLY:
1. WRONG LANGUAGE: Does the language fit the goal?
2. MISSING FILES: Are critical files missing? (entry point, config, core logic)
3. EXCESS FILES: Too many files for a simple project?
4. DEP ORDER: Will the build order cause import failures?
5. GOAL MISMATCH: Does the file plan actually implement what was asked?
6. MISSING FEATURES: Go through EVERY feature/requirement in the goal word by word.
   Does each requested feature have a corresponding file or function?

Output ONLY this JSON:
""" + """{
  "approved": true/false,
  "issues": ["issue 1", "issue 2"],
  "patches": [
    {"action": "add_file", "path": "...", "purpose": "..."},
    {"action": "remove_file", "path": "..."},
    {"action": "reorder", "dependency_order": ["..."]}
  ]
}

If the blueprint looks good, return {"approved": true, "issues": [], "patches": []}.
Output ONLY JSON."""

    def _apply_review_patches(bp: dict, raw: dict, source: str) -> dict:
        """Apply review patches from a reviewer to the blueprint."""
        if not isinstance(raw, dict) or raw.get("approved", True):
            blog.info(f"{source} approved blueprint")
            return bp
        for issue in raw.get("issues", [])[:5]:
            blog.warning(f"{source} issue: {issue}")
        for patch in raw.get("patches", [])[:8]:
            action = patch.get("action", "")
            if action == "add_file":
                path = patch.get("path", "")
                purpose = patch.get("purpose", "")
                if path and path not in {f["path"] for f in bp["files"]}:
                    bp["files"].append({
                        "path": path, "purpose": purpose,
                        "exports": [], "imports": [],
                        "estimated_lines": 30, "critical": False,
                    })
                    if path not in bp.get("dependency_order", []):
                        bp["dependency_order"].append(path)
                    blog.info(f"{source} added file: {path}")
            elif action == "remove_file":
                path = patch.get("path", "")
                if path:
                    before = len(bp["files"])
                    bp["files"] = [f for f in bp["files"] if f["path"] != path]
                    if len(bp["files"]) < before:
                        bp["dependency_order"] = [
                            p for p in bp.get("dependency_order", []) if p != path
                        ]
                        blog.info(f"{source} removed file: {path}")
            elif action == "reorder":
                new_order = patch.get("dependency_order", [])
                if new_order and isinstance(new_order, list):
                    valid_paths = {f["path"] for f in bp["files"]}
                    if all(p in valid_paths for p in new_order):
                        for p in bp.get("dependency_order", []):
                            if p not in new_order:
                                new_order.append(p)
                        bp["dependency_order"] = new_order
                        blog.info(f"{source} reordered dependency chain")
        return bp

    # Stage 1: Self-Review (manager_model, temperature=0.3)
    blog.phase("planner_self_review", "Planner self-review (Stage 1/2)", model=ws["manager_model"])
    try:
        response_1 = llm_call(
            model=ws["manager_model"],
            prompt=review_prompt,
            system="You are a strict code reviewer. Output only valid JSON.",
            max_tokens=2048,
            temperature=0.3,
        )
        raw_1 = json.loads(clean_json(response_1))
        blueprint = _apply_review_patches(blueprint, raw_1, "Planner[self]")
    except Exception as exc:
        blog.warning(f"Planner self-review failed ({exc}), continuing")

    # Stage 2: Co-Review (independent second model, if configured)
    review_model = ws.get("review_model", "")
    if review_model:
        blog.phase("planner_co_review", "Co-reviewer blueprint review (Stage 2/2)", model=review_model)
        # Co-Reviewer gets the (possibly already patched) blueprint
        # and a fresh, context-free prompt
        co_file_summary = "\n".join(
            f"  - {f['path']}: {f.get('purpose', '?')}"
            for f in blueprint.get("files", [])[:25]
        )
        co_dep_content = blueprint.get("dependencies", {}).get("content", "")[:500]
        co_prompt = f"""You are an independent code project reviewer with NO prior context.
Review this blueprint for a software project and find any issues.

GOAL: "{ws['goal']}"

BLUEPRINT:
  Language: {blueprint.get('language', '?')}
  Framework: {blueprint.get('framework', 'none')}
  Files ({len(blueprint.get('files', []))}):
{co_file_summary}
  Dependency order: {blueprint.get('dependency_order', [])[:20]}
  Dependencies: {co_dep_content[:300]}

{negative_examples}

Be CRITICAL. Assume nothing is correct unless you verify it.
Focus especially on: missing features from the goal, wrong file structure,
impossible dependency orders, missing entry points.

Output ONLY this JSON:
""" + """{
  "approved": true/false,
  "issues": ["issue 1", "issue 2"],
  "patches": [
    {"action": "add_file", "path": "...", "purpose": "..."},
    {"action": "remove_file", "path": "..."},
    {"action": "reorder", "dependency_order": ["..."]}
  ]
}

Output ONLY JSON."""

        try:
            response_2 = llm_call(
                model=review_model,
                prompt=co_prompt,
                system="You are a strict independent code reviewer. Output only valid JSON.",
                max_tokens=2048,
                temperature=0.2,
            )
            raw_2 = json.loads(clean_json(response_2))
            blueprint = _apply_review_patches(blueprint, raw_2, f"CoReviewer[{review_model}]")
        except Exception as exc:
            blog.warning(f"Co-reviewer failed ({exc}), continuing with self-review result")
    else:
        blog.info("No review_model configured, skipping co-review (Stage 2)")

    ws["blueprint"] = blueprint
    ws["language"] = blueprint.get("language", "unknown")

    blog.tech(
        language=ws["language"],
        framework=blueprint.get("framework", ""),
        why=blueprint.get("why", ""),
        is_multi=blueprint.get("is_multi_language", False),
    )
    blog.plan(
        files_total=len(blueprint.get("files", [])),
        file_paths=[f["path"] for f in blueprint.get("files", [])],
        complexity=blueprint.get("estimated_complexity", ""),
    )

    return ws


# ---------------------------------------------------------------------------
# Helper: generate a dependency manifest from blueprint file list
# ---------------------------------------------------------------------------
def _generate_manifest_from_blueprint(
    ws: dict, manifest_name: str, dep_type: str, output_dir: str
) -> None:
    """Generate a dependency manifest with framework-first approach:
    1. Start with the framework package (known correct)
    2. Add packages derivable from file purposes
    3. LLM only fills gaps with strict "known packages only" constraint
    """
    blueprint = ws["blueprint"]
    language = blueprint.get("language", "")
    framework = blueprint.get("framework", "").lower().strip()
    goal = blueprint.get("_goal", ws.get("goal", ""))

    # ── Step 1: Framework seed (always correct) ──────────────────────────
    FRAMEWORK_SEEDS = {
        # Python
        "fastapi":   "fastapi>=0.100\nuvicorn[standard]>=0.20\npydantic>=2.0\n",
        "flask":     "flask>=2.3\n",
        "django":    "django>=4.2\n",
        "starlette": "starlette>=0.27\nuvicorn[standard]>=0.20\n",
        "aiohttp":   "aiohttp>=3.9\n",
        # JS/TS — handled via package.json below
        "express":   None,
        "react":     None,
        "vue":       None,
        "next":      None,
        "svelte":    None,
    }

    seed = FRAMEWORK_SEEDS.get(framework, "")

    if dep_type == "requirements_txt":
        # Known safe additions derived from file purposes
        files_text = " ".join(f.get("purpose", "") for f in blueprint.get("files", []))
        additions = []
        KNOWN_SAFE = {
            "sqlalchemy": "sqlalchemy>=2.0",
            "sqlite":     "# sqlite3 is stdlib",
            "jwt":        "python-jose[cryptography]>=3.3",
            "auth":       "passlib[bcrypt]>=1.7",
            "pydantic":   "pydantic>=2.0",
            "httpx":      "httpx>=0.24",
            "requests":   "requests>=2.31",
            "redis":      "redis>=5.0",
            "celery":     "celery>=5.3",
            "pandas":     "pandas>=2.0",
            "numpy":      "numpy>=1.24",
            "matplotlib": "matplotlib>=3.7",
            "pytest":     "pytest>=7.4",
            "dotenv":     "python-dotenv>=1.0",
        }
        for keyword, pkg_line in KNOWN_SAFE.items():
            if keyword in files_text.lower() and pkg_line not in (seed or ""):
                if not pkg_line.startswith("#"):
                    additions.append(pkg_line)

        base_content = (seed or "") + "\n".join(additions)

        # LLM only for gaps, if really needed
        if not base_content.strip():
            prompt = (
                f"Generate a minimal requirements.txt for a Python {framework or 'script'} project.\n"
                f"Goal: {goal[:200]}\n\n"
                "STRICT RULES:\n"
                "- ONLY include packages you are 100% certain exist on PyPI\n"
                "- Maximum 5 packages\n"
                "- Prefer stdlib over external packages\n"
                "- Output ONLY the requirements.txt content, no explanation\n"
            )
            try:
                response = llm_call(
                    model=ws.get("coder_model") or ws.get("manager_model", ""),
                    prompt=prompt,
                    system="Output ONLY valid requirements.txt content. Max 5 well-known packages.",
                    max_tokens=256,
                    temperature=0.0,
                )
                base_content = strip_fences(response).strip()
            except Exception as exc:
                blog.warning(f"Manifest LLM fallback fehlgeschlagen: {exc}")
                base_content = ""

        if base_content.strip():
            manifest_path = os.path.join(output_dir, manifest_name)
            write_file(manifest_path, base_content)
            blueprint["dependencies"]["content"] = base_content
            blog.info(f"Framework-first {manifest_name} geschrieben ({len(base_content)} bytes)")

    elif dep_type == "package_json":
        # For package.json: derive skeleton from framework
        PACKAGE_JSON_SEEDS = {
            "express": {
                "name": blueprint.get("project_name", "project"),
                "version": "1.0.0",
                "main": "index.js",
                "scripts": {"start": "node index.js", "dev": "nodemon index.js"},
                "dependencies": {"express": "^4.18.2"},
            },
            "react": {
                "name": blueprint.get("project_name", "project"),
                "version": "0.1.0",
                "private": True,
                "scripts": {"start": "react-scripts start", "build": "react-scripts build"},
                "dependencies": {"react": "^18.2.0", "react-dom": "^18.2.0", "react-scripts": "5.0.1"},
            },
        }
        seed_json = PACKAGE_JSON_SEEDS.get(framework)
        if seed_json:
            import json as _json
            content = _json.dumps(seed_json, indent=2)
            manifest_path = os.path.join(output_dir, manifest_name)
            write_file(manifest_path, content)
            blueprint["dependencies"]["content"] = content
            blog.info(f"Framework-first package.json für {framework} geschrieben")
        else:
            # Unknown JS framework: fall back to LLM approach
            files_desc = "\n".join(
                f"  - {f['path']}: {f.get('purpose', '')}"
                for f in blueprint.get("files", [])[:30]
            )
            hint = blueprint.get("dependencies", {}).get("_hint", "")
            prompt = f"""Generate the content for a **{manifest_name}** file for this project.

PROJECT GOAL: {goal}
LANGUAGE: {language}
FRAMEWORK: {framework}
FILES:
{files_desc}

{"DEPENDENCY HINTS: " + hint if hint else ""}

RULES:
- Only include packages that ACTUALLY EXIST on npm.
- Do NOT invent or hallucinate package names.
- Maximum 10 dependencies.
- Valid JSON with name, version, dependencies, and scripts.
- Output ONLY the raw file content, no markdown fences, no explanation."""

            try:
                response = llm_call(
                    model=ws.get("coder_model") or ws.get("manager_model", ""),
                    prompt=prompt,
                    system="You are a dependency manifest generator. Output ONLY the file content.",
                    max_tokens=1024,
                    temperature=0.05,
                )
                content = strip_fences(response).strip()
                if content:
                    manifest_path = os.path.join(output_dir, manifest_name)
                    write_file(manifest_path, content)
                    blog.info(f"Generated {manifest_name} via LLM ({len(content)} bytes)")
                    blueprint["dependencies"]["content"] = content
            except Exception as exc:
                blog.warning(f"Failed to generate {manifest_name}: {exc}")
    else:
        # Other dep types (Cargo.toml, go.mod, etc.): keep original LLM approach
        files_desc = "\n".join(
            f"  - {f['path']}: {f.get('purpose', '')}"
            for f in blueprint.get("files", [])[:30]
        )
        hint = blueprint.get("dependencies", {}).get("_hint", "")
        prompt = f"""Generate the content for a **{manifest_name}** file for this project.

PROJECT GOAL: {goal}
LANGUAGE: {language}
FRAMEWORK: {framework}
FILES:
{files_desc}

{"DEPENDENCY HINTS: " + hint if hint else ""}

RULES:
- Only include packages that ACTUALLY EXIST on the respective registry.
- Do NOT invent or hallucinate package names.
- Output ONLY the raw file content, no markdown fences, no explanation."""

        try:
            response = llm_call(
                model=ws.get("coder_model") or ws.get("manager_model", ""),
                prompt=prompt,
                system="You are a dependency manifest generator. Output ONLY the file content.",
                max_tokens=1024,
                temperature=0.05,
            )
            content = strip_fences(response).strip()
            if content:
                manifest_path = os.path.join(output_dir, manifest_name)
                write_file(manifest_path, content)
                blog.info(f"Generated {manifest_name} via LLM ({len(content)} bytes)")
                blueprint["dependencies"]["content"] = content
        except Exception as exc:
            blog.warning(f"Failed to generate {manifest_name}: {exc}")


# ---------------------------------------------------------------------------
# Agent 2: RETRIEVER
# ---------------------------------------------------------------------------
def agent_retriever(ws: dict) -> dict:
    """Validate critical files, dependencies, manifests. Prepare build environment.

    This agent does NOT use an LLM - it's pure validation and env setup.
    Runs between planner (manager model) and coder (coder model) so
    models can be swapped in VRAM during this CPU-only phase.
    """
    blog.phase("agent_retriever", "Agent 2/5: RETRIEVER - Validating & preparing environment")
    ws["phase"] = "retriever"

    blueprint = ws["blueprint"]
    language = ws["language"]
    output_dir = ws["output_dir"]

    os.makedirs(output_dir, exist_ok=True)

    # Ensure critical files exist in blueprint
    blueprint = ensure_critical_files(blueprint, language)
    ws["blueprint"] = blueprint

    # Validate safe stack
    stack_warnings = validate_blueprint(blueprint)
    if stack_warnings:
        for w in stack_warnings:
            blog.warning(f"Safe Stack: {w}")
        blueprint.setdefault("safe_stack_violations", []).extend(stack_warnings)

    # Set up virtual environment for Python projects
    # NOTE: When running in Docker (Linux) but outputting to a Windows host volume,
    # the venv would contain Linux symlinks (lib64) that don't work on Windows.
    # We create the venv for in-Docker testing only; it gets cleaned up after build.
    if language == "python":
        blog.info("Setting up Python virtual environment for project")
        venv_python = get_venv_python(output_dir)
        blog.info(f"Venv Python: {venv_python}")

    # Write dependency manifest if provided in blueprint
    deps = blueprint.get("dependencies", {})
    dep_type = deps.get("type", "")
    manifest_map = {
        "requirements_txt": "requirements.txt",
        "package_json": "package.json",
        "cargo_toml": "Cargo.toml",
        "go_mod": "go.mod",
        "pom_xml": "pom.xml",
    }
    manifest_name = manifest_map.get(dep_type)

    if deps.get("content") and manifest_name:
        # Master blueprint provided explicit manifest content
        manifest_path = os.path.join(output_dir, manifest_name)
        if not os.path.exists(manifest_path):
            write_file(manifest_path, deps["content"])
            blog.info(f"Wrote manifest: {manifest_name}")
    elif dep_type and manifest_name and not deps.get("content"):
        # Type is known but content is empty (common for subprojects).
        # Generate a manifest via LLM based on the blueprint's file list.
        blog.info(f"Generating {manifest_name} from blueprint (no content provided)")
        _generate_manifest_from_blueprint(ws, manifest_name, dep_type, output_dir)

    blog.info(f"Retriever validated {len(blueprint.get('files', []))} files, language={language}")
    return ws


# ---------------------------------------------------------------------------
# Agent 3: CODER
# ---------------------------------------------------------------------------
def agent_coder(ws: dict) -> dict:
    """Generate all code: skeletons first, then fill in one file at a time.

    Uses the coder model (specialized for code generation).
    """
    blog.phase("agent_coder", "Agent 3/5: CODER - Generating code", model=ws["coder_model"])
    ws["phase"] = "coder"

    blueprint = ws["blueprint"]
    coder_model = ws["coder_model"]
    output_dir = ws["output_dir"]
    ctx_tokens = ws["ctx_tokens"]
    language = ws["language"]
    files_list = blueprint["files"]
    dep_order = blueprint.get("dependency_order", [f["path"] for f in files_list])
    files_total = len(files_list)

    # Phase 1: Generate all skeletons
    blog.phase("skeleton", f"Generating skeletons for {files_total} files", model=coder_model)
    skeletons = generate_all_skeletons(blueprint, coder_model)

    if not skeletons:
        blog.warning("Zero skeletons parsed, retrying...")
        skeletons = generate_all_skeletons(blueprint, coder_model)

    if not skeletons:
        blog.error("Skeleton generation failed after 2 attempts", severity="fatal")
        raise RuntimeError("Could not generate any file skeletons")

    actual = len(skeletons)
    if actual < files_total:
        blog.warning(f"Only {actual}/{files_total} skeletons generated")

    # Fix 2: Targeted retry for individually missing skeletons
    planned_paths = {f["path"] for f in files_list}
    missing_skeletons = planned_paths - set(skeletons.keys())
    if missing_skeletons:
        blog.warning(f"Einzelne Skeletons fehlen, generiere nach: {missing_skeletons}")
        for missing_path in sorted(missing_skeletons):
            missing_spec = next((f for f in files_list if f["path"] == missing_path), None)
            if not missing_spec:
                continue
            try:
                single_skeleton = _generate_single_skeleton(missing_spec, blueprint, coder_model)
                if single_skeleton:
                    skeletons[missing_path] = single_skeleton
                    blog.info(f"Skeleton nachgeneriert: {missing_path}")
            except Exception as exc:
                blog.warning(f"Konnte Skeleton für {missing_path} nicht generieren: {exc}")

    # Write skeletons to disk
    for path, skeleton in skeletons.items():
        full_path = os.path.join(output_dir, path)
        write_file(full_path, skeleton)

    ws["skeletons"] = skeletons

    # Phase 1.5: Validate manifest and install deps
    blog.phase("manifest_validation", "Validating manifest and installing dependencies")
    validate_manifest(output_dir, language, coder_model, skeletons)

    # Phase 2: Fill in each file
    written_files: dict[str, str] = {}
    file_index = 0

    for file_path in dep_order:
        file_spec = next((f for f in files_list if f["path"] == file_path), None)
        if not file_spec:
            blog.warning(f"File {file_path} in dep order but not in plan, skipping")
            continue

        file_index += 1
        blog.file_start(file_path, file_index, files_total)

        code = fill_in_file(file_spec, skeletons, written_files, blueprint, coder_model, ctx_tokens)

        full_path = os.path.join(output_dir, file_path)
        write_file(full_path, code)
        written_files[file_path] = code
        skeletons[file_path] = code  # Fix 1: Replace skeleton with finished code for subsequent files
        blog.file_done(file_path, len(code))

        # Quick compile check on just-written file
        success, all_errors = compile_check(output_dir, language)
        file_basename = os.path.basename(file_path)
        errors = [e for e in all_errors if file_basename in e or file_path in e]

        if not errors:
            continue

        blog.warning(f"Compile errors after {file_path}: {len(errors)}")
        for e in errors[:3]:
            blog.error(e, file=file_path, severity="compile")

        # Detect structural errors that can't be fixed by code repair
        structural_error = False
        for e in errors:
            e_lower = e.lower()
            if "duplicate module" in e_lower or "duplicate entry" in e_lower:
                structural_error = True
                # Find and remove the duplicate file
                import re as _re
                dup_match = _re.search(r'also at "([^"]+)"', e)
                if dup_match:
                    dup_path = dup_match.group(1)
                    if os.path.exists(dup_path) and dup_path != full_path:
                        try:
                            os.remove(dup_path)
                            blog.info(f"Removed duplicate module: {dup_path}")
                            # Also clean up empty parent dirs
                            dup_parent = os.path.dirname(dup_path)
                            if dup_parent and os.path.isdir(dup_parent) and not os.listdir(dup_parent):
                                os.rmdir(dup_parent)
                        except OSError as exc:
                            blog.warning(f"Could not remove duplicate: {exc}")
                break

        if structural_error:
            # Re-check after removing duplicates
            success_s, all_errors_s = compile_check(output_dir, language)
            errors_s = [e for e in all_errors_s if file_basename in e or file_path in e]
            if not errors_s:
                blog.verify(True, "compile", f"{file_path} fixed by removing duplicate")
                continue
            # If still broken, fall through to normal repair
            errors = errors_s

        max_repairs = MAX_REPAIR_ATTEMPTS.get(language, 3)
        repaired_ok = False

        for repair_attempt in range(1, max_repairs + 1):
            blog.repair(file_path, repair_attempt, max_repairs)

            # Fix 3: Enrich errors with error analysis hints
            analysis = analyze_errors(errors, language)
            enriched_errors = list(errors) + [f"[HINT] {analysis['llm_hint']}"]

            # Fix 3: If import_error, add relevant file content as context
            if analysis.get("category") == "import_error" and written_files:
                import re as _err_re
                for err in errors:
                    for fpath, fcode in written_files.items():
                        if fpath == file_path:
                            continue
                        # Check if the imported symbol is in another project file
                        fname_stem = os.path.splitext(os.path.basename(fpath))[0]
                        if fname_stem in err:
                            enriched_errors.append(
                                f"[CONTEXT] Content of {fpath} (referenced in error):\n{fcode[:5000]}"
                            )
                            break

            repaired = repair_file(
                file_path, code, enriched_errors, language, coder_model,
                written_files=written_files,
            ) if repair_attempt == 1 else patch_repair_file(
                file_path, code, enriched_errors, language, coder_model,
                written_files=written_files,
            )
            write_file(full_path, repaired)
            written_files[file_path] = repaired
            skeletons[file_path] = repaired  # Fix 1: Also update skeletons during repair
            code = repaired
            format_code(output_dir, language)

            success2, all_errors2 = compile_check(output_dir, language)
            errors2 = [e for e in all_errors2 if file_basename in e or file_path in e]
            if not errors2:
                blog.verify(True, "compile", f"{file_path} repaired")
                repaired_ok = True
                break
            errors = errors2

        if not repaired_ok:
            # Nuclear regen: fresh attempt with error context
            blog.phase("nuclear_regen", f"Fresh regeneration for {file_path}")
            file_spec_extra = dict(file_spec)
            error_summary = "\n".join(errors[:10])
            file_spec_extra["purpose"] = (
                f"{file_spec.get('purpose', '')} "
                f"[PREVIOUS ATTEMPT HAD ERRORS - avoid these: {error_summary[:500]}]"
            )
            fresh_code = fill_in_file(
                file_spec_extra, skeletons, written_files, blueprint, coder_model, ctx_tokens,
            )
            write_file(full_path, fresh_code)
            written_files[file_path] = fresh_code
            format_code(output_dir, language)

        # Fix 4: Intermediate coherence check every 4 files
        if file_index % 4 == 0 and file_index > 0 and language in ("javascript", "typescript", "python"):
            blog.info(f"Zwischen-Coherence-Check nach {file_index} Dateien...")
            written_files = _validate_code_coherence(
                output_dir, language, coder_model, ctx_tokens,
                written_files, blueprint, skeletons,
            )

    # Post-coder validation: ensure all planned files were actually generated
    planned_paths = {f["path"] for f in files_list}
    missing = planned_paths - set(written_files.keys())
    if missing:
        blog.warning(f"Missing {len(missing)} planned files after coder phase: {', '.join(sorted(missing)[:10])}")
        for miss_path in sorted(missing):
            miss_spec = next((f for f in files_list if f["path"] == miss_path), None)
            if miss_spec:
                blog.info(f"Generating missing file: {miss_path}")
                try:
                    code = fill_in_file(miss_spec, skeletons, written_files, blueprint, coder_model, ctx_tokens)
                    miss_full = os.path.join(output_dir, miss_path)
                    write_file(miss_full, code)
                    written_files[miss_path] = code
                    blog.file_done(miss_path, len(code))
                except Exception as exc:
                    blog.error(f"Failed to generate missing file {miss_path}: {exc}", severity="warning")

    # ── Cross-file reference validation ──────────────────────────────────
    # Scan ALL HTML files for <script src> and <link href> pointing to
    # local files that don't actually exist on disk.  Generate any missing
    # referenced files so the delivered project never has 404s.
    import re as _re

    _referenced_missing: list[str] = []
    for fpath, content in list(written_files.items()):
        if not fpath.endswith((".html", ".htm")):
            continue
        html_dir = os.path.dirname(fpath)  # relative dir of the HTML file

        # Collect local script src references
        for src in _re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', content, _re.IGNORECASE):
            if src.startswith(("http://", "https://", "//", "data:")):
                continue
            ref_path = os.path.normpath(os.path.join(html_dir, src)).replace("\\", "/")
            if ref_path not in written_files and not os.path.exists(os.path.join(output_dir, ref_path)):
                _referenced_missing.append(ref_path)

        # Collect local CSS href references
        for href in _re.findall(r'<link[^>]+href=["\']([^"\']+\.css)["\']', content, _re.IGNORECASE):
            if href.startswith(("http://", "https://", "//", "data:")):
                continue
            ref_path = os.path.normpath(os.path.join(html_dir, href)).replace("\\", "/")
            if ref_path not in written_files and not os.path.exists(os.path.join(output_dir, ref_path)):
                _referenced_missing.append(ref_path)

    # Deduplicate
    _referenced_missing = list(dict.fromkeys(_referenced_missing))

    if _referenced_missing:
        blog.warning(
            f"Found {len(_referenced_missing)} file(s) referenced in HTML but missing on disk: "
            + ", ".join(_referenced_missing[:10])
        )
        for ref_path in _referenced_missing:
            blog.info(f"Generating missing referenced file: {ref_path}")
            # Build a minimal spec for the missing file
            ext = os.path.splitext(ref_path)[1].lower()
            if ext in (".js", ".mjs"):
                purpose = (
                    f"JavaScript file referenced via <script src> in the project HTML. "
                    f"It must contain all logic that the HTML page expects from '{ref_path}'. "
                    f"Examine the HTML and other project files to determine what functions, "
                    f"classes, and DOM interactions this file should provide."
                )
            elif ext == ".css":
                purpose = (
                    f"CSS stylesheet referenced via <link href> in the project HTML. "
                    f"It must contain all styles the HTML page expects from '{ref_path}'."
                )
            else:
                purpose = f"Asset file referenced in the HTML. Path: {ref_path}"

            ref_spec = {"path": ref_path, "purpose": purpose}
            try:
                code = fill_in_file(ref_spec, skeletons, written_files, blueprint, coder_model, ctx_tokens)
                ref_full = os.path.join(output_dir, ref_path)
                write_file(ref_full, code)
                written_files[ref_path] = code
                blog.file_done(ref_path, len(code))
                blog.info(f"Generated missing referenced file: {ref_path} ({len(code)} chars)")
            except Exception as exc:
                blog.error(f"Failed to generate referenced file {ref_path}: {exc}", severity="warning")

    # ── Code coherence validation ────────────────────────────────────────
    # After ALL files are generated, check for functions/variables that are
    # CALLED but never DEFINED anywhere in the project.  This catches the
    # case where the coder references a skeleton function that was never
    # implemented (e.g., updateTurnIndicator called but not defined).
    if language in ("javascript", "typescript") and written_files:
        written_files = _validate_code_coherence(
            output_dir, language, coder_model, ctx_tokens,
            written_files, blueprint, skeletons,
        )
    elif language == "python" and written_files:
        written_files = _validate_code_coherence(
            output_dir, language, coder_model, ctx_tokens,
            written_files, blueprint, skeletons,
        )

    ws["written_files"] = written_files
    return ws


# ---------------------------------------------------------------------------
# Helper: Code coherence validation
# ---------------------------------------------------------------------------
def _validate_code_coherence(
    output_dir: str,
    language: str,
    coder_model: str,
    ctx_tokens: int,
    written_files: dict,
    blueprint: dict,
    skeletons: dict,
) -> dict:
    """Validate that all cross-file function/variable references resolve.

    Uses the LLM to review all generated code and identify functions,
    variables, or classes that are CALLED but never DEFINED.  If issues
    are found, the affected files are patched to add the missing
    definitions.
    """
    blog.phase("coherence_check", "Validating cross-file code coherence", model=coder_model)

    # Build a combined view of code files (only source code, not configs)
    code_exts = {
        "javascript": (".js", ".mjs", ".jsx"),
        "typescript": (".ts", ".tsx", ".mts"),
        "python": (".py",),
    }
    exts = code_exts.get(language, (".js", ".py"))
    code_files = {p: c for p, c in written_files.items() if p.endswith(exts)}

    if not code_files:
        return written_files

    # Concatenate all code for the LLM review
    code_parts = []
    for fpath in sorted(code_files.keys()):
        code_parts.append(f"=== {fpath} ===\n{code_files[fpath]}")
    all_code = "\n\n".join(code_parts)

    # Respect context limits
    max_chars = int(ctx_tokens * 2.5)  # rough char-to-token ratio
    if len(all_code) > max_chars:
        all_code = all_code[:max_chars] + "\n... (truncated)"

    review_prompt = f"""Review ALL {language} source files below from a SINGLE project.
Find functions, variables, or classes that are CALLED or REFERENCED but
NEVER DEFINED or IMPORTED in ANY of the project files.

Ignore:
  - Browser/Node globals (document, window, console, setTimeout, fetch, require, module, exports, process, Buffer, __dirname, Promise, Map, Set, Array, Object, JSON, Math, Date, RegExp, Error, parseInt, parseFloat, isNaN, alert, confirm, prompt)
  - DOM methods (getElementById, querySelector, addEventListener, createElement, appendChild, etc.)
  - Standard library functions
  - Third-party package imports (from node_modules or pip packages)
  - Method calls on objects (like obj.method() — only flag if obj itself is undefined)
  - Callbacks passed to event listeners

Only flag references where the function/class IS expected to be defined
within THIS project but is missing.

{all_code}

Output ONLY valid JSON — an array of issues.  Each issue:
{{"file": "<file that CALLS the undefined reference>",
  "reference": "<function/variable name that is undefined>",
  "called_as": "<the actual call expression, e.g. updateTurnIndicator()>",
  "should_be_in": "<file where it logically belongs, or same file>"}}

If NO issues found, output: []
Output ONLY JSON, no explanation."""

    try:
        response = llm_call(
            model=coder_model,
            prompt=review_prompt,
            system=f"Expert {language} code reviewer. Find undefined references. Output ONLY valid JSON.",
            max_tokens=4096,
            temperature=0.05,
        )
        issues = json.loads(clean_json(response))
    except Exception as exc:
        blog.warning(f"Coherence check parse failed: {exc}")
        return written_files

    if not isinstance(issues, list) or len(issues) == 0:
        blog.verify(True, "coherence", "No undefined cross-file references found")
        return written_files

    blog.warning(f"Found {len(issues)} undefined reference(s): "
                 + ", ".join(i.get('reference', '?') for i in issues[:8]))

    # Group issues by the file that SHOULD contain the definition
    from collections import defaultdict
    fixes_by_file: dict[str, list[dict]] = defaultdict(list)
    for issue in issues:
        target = issue.get("should_be_in", issue.get("file", ""))
        # Normalise: if the target file doesn't exist, put it in the caller
        if target not in written_files:
            target = issue.get("file", "")
        if target in written_files:
            fixes_by_file[target].append(issue)

    for target_file, file_issues in fixes_by_file.items():
        current_code = written_files[target_file]
        missing_names = [i.get("reference", "?") for i in file_issues]

        blog.info(f"Patching {target_file} to add: {', '.join(missing_names)}")

        # Provide context: the files that CALL the missing references
        callers_context = []
        caller_files = set(i.get("file", "") for i in file_issues)
        for cf in caller_files:
            if cf in written_files and cf != target_file:
                callers_context.append(f"=== {cf} ===\n{written_files[cf]}")

        callers_str = "\n\n".join(callers_context[:3])
        if len(callers_str) > 10000:
            callers_str = callers_str[:10000] + "\n...(truncated)"

        fix_prompt = f"""The file below is part of a {language} project.
These functions/variables are CALLED in other files but are MISSING from this file:

{chr(10).join(f"  - {i.get('reference','?')}: called as {i.get('called_as','?')} in {i.get('file','?')}" for i in file_issues)}

CURRENT FILE ({target_file}):
{current_code}

FILES THAT CALL THE MISSING REFERENCES:
{callers_str}

RULES:
  1. Output the COMPLETE file with ALL missing functions/variables ADDED
  2. Implement them properly — study how they are called in the other files
     to determine the correct signature, parameters, and return values
  3. Do NOT remove or break any existing code
  4. Do NOT add imports for packages not already used
  5. Output ONLY code, no markdown fences, no explanation"""

        try:
            fix_response = llm_call(
                model=coder_model,
                prompt=fix_prompt,
                system=f"Expert {language} developer. Add missing function definitions. Output ONLY code.",
                max_tokens=14336,
                temperature=0.1,
            )
            fixed_code = strip_fences(fix_response)

            # Safety check: patched file shouldn't be drastically smaller
            if len(fixed_code) >= len(current_code) * 0.7:
                full_path = os.path.join(output_dir, target_file)
                write_file(full_path, fixed_code)
                written_files[target_file] = fixed_code
                blog.info(f"Patched {target_file}: added {', '.join(missing_names)}")
            else:
                blog.warning(f"Coherence fix for {target_file} was too small, skipping")
        except Exception as exc:
            blog.warning(f"Coherence fix failed for {target_file}: {exc}")

    blog.verify(True, "coherence", f"Patched {len(fixes_by_file)} file(s) for undefined references")
    return written_files


# ---------------------------------------------------------------------------
# Agent 4: EXECUTOR
# ---------------------------------------------------------------------------
def agent_executor(ws: dict) -> dict:
    """Run the generated project: compile check, sandbox test, validate it starts.

    This agent does NOT use an LLM - pure execution and validation.
    Runs between coder and critic so coder model can be unloaded.
    """
    blog.phase("agent_executor", "Agent 4/5: EXECUTOR - Testing generated project")
    ws["phase"] = "executor"

    output_dir = ws["output_dir"]
    language = ws["language"]

    # Ensure Python entry points have if __name__ == "__main__" block
    if language == "python":
        _ensure_python_entry_points(output_dir, ws.get("written_files", {}))

    # Final compile check
    blog.phase("final_compile", "Final compilation check")
    success, errors = compile_check(output_dir, language)
    ws["compile_ok"] = success

    if success:
        blog.verify(True, "final_compile", "Project compiles successfully")
    else:
        blog.verify(False, "final_compile", f"{len(errors)} errors remain")
        ws["errors"] = errors
        for e in errors[:10]:
            blog.error(e, severity="compile")

    # Fix 5: HTML/JS smoke test for web projects
    if language in ("javascript", "html", "typescript"):
        from skills.builder.engine.compile_checks import html_smoke_test
        html_ok, html_errors = html_smoke_test(output_dir)
        if not html_ok:
            blog.warning(f"HTML smoke test fehlgeschlagen:\n{html_errors}")
            ws["errors"].extend(html_errors.splitlines())
            ws["compile_ok"] = False
        else:
            blog.verify(True, "html_smoke", "HTML/JS smoke test bestanden")

    # Install dependencies before sandbox test
    _MANIFEST_FOR_LANG = {
        "python":     "requirements.txt",
        "javascript": "package.json",
        "typescript": "package.json",
        "rust":       "Cargo.toml",
        "go":         "go.mod",
    }
    manifest_file = _MANIFEST_FOR_LANG.get(language, "")
    if manifest_file:
        manifest_path = os.path.join(output_dir, manifest_file)
        if os.path.exists(manifest_path):
            blog.info(f"Installing & validating dependencies ({manifest_file})...")
            # validate_manifest handles install + repair loop in one call
            dep_ok = validate_manifest(
                output_dir, language,
                ws.get("coder_model") or ws.get("manager_model", ""),
            )
            if dep_ok:
                blog.verify(True, "deps_install", f"Dependencies OK ({language})")
            else:
                blog.warning(f"Dependency issues could not be fully resolved for {manifest_file}")
                ws["errors"].append(f"deps_install: {manifest_file} has unresolved dependency errors")
        else:
            blog.warning(f"No {manifest_file} found — dependencies may be missing")

    # Sandbox test
    blog.phase("sandbox_test", "Sandbox: checking if project starts and runs")
    test_ok, test_output = sandbox_test(output_dir, language)
    ws["sandbox_ok"] = test_ok

    if test_ok:
        blog.verify(True, "sandbox", f"Project starts and runs: {test_output[:100]}")
    else:
        blog.warning(f"Sandbox failed: {test_output[:200]}")
        ws["errors"].append(f"sandbox: {test_output[:500]}")
        ws["sandbox_output"] = test_output

    return ws


# ---------------------------------------------------------------------------
# Agent 5: CRITIC
# ---------------------------------------------------------------------------
def agent_critic(ws: dict) -> dict:
    """Review results, trigger self-correction if needed, polish output.

    Uses the coder model for repairs (re-loaded if needed).
    Uses the manager model for UX polish suggestions.
    """
    blog.phase("agent_critic", "Agent 5/5: CRITIC - Reviewing and polishing", model=ws["coder_model"])
    ws["phase"] = "critic"

    output_dir = ws["output_dir"]
    language = ws["language"]
    coder_model = ws["coder_model"]
    manager_model = ws["manager_model"]
    written_files = ws["written_files"]

    # Self-correction loop if sandbox failed
    if not ws["sandbox_ok"] and written_files:
        sandbox_output = ws.get("sandbox_output", "")
        if sandbox_output:
            # Check if failure is a dependency problem first
            _dep_repair_attempted = False
            dep_keywords = ["ModuleNotFoundError", "ImportError", "Cannot find module",
                            "MODULE_NOT_FOUND", "no matching distribution",
                            "Could not find a version"]
            if any(kw.lower() in sandbox_output.lower() for kw in dep_keywords):
                blog.info("Critic: sandbox failure looks like a dependency issue, repairing manifest")
                dep_ok = validate_manifest(output_dir, language, coder_model)
                _dep_repair_attempted = True
                if dep_ok:
                    # Re-test after dep fix
                    test_ok, test_output = sandbox_test(output_dir, language)
                    if test_ok:
                        blog.verify(True, "critic_dep_fix", "Fixed by repairing dependencies")
                        ws["sandbox_ok"] = True
                        sandbox_output = ""
                    else:
                        sandbox_output = test_output

            # Code repair loop (only if still failing)
            if not ws["sandbox_ok"] and sandbox_output:
                for attempt in range(MAX_SANDBOX_RETRIES):
                    blog.phase(
                        "critic_repair",
                        f"Critic repair attempt {attempt + 1}/{MAX_SANDBOX_RETRIES}",
                        model=coder_model,
                    )

                    # Diagnose the failure
                    file_list = "\n".join(f"  - {p}" for p in sorted(written_files.keys())[:20])
                    diag_prompt = f"""A {language} project failed at runtime. Diagnose the failure.

ERROR OUTPUT:
{sandbox_output[:2000]}

PROJECT FILES:
{file_list}

Output ONLY this JSON:
""" + """{
  "file": "<path of file to fix>",
  "root_cause": "<what went wrong>",
  "fix_strategy": "<how to fix it>"
}"""

                    problem_file = None
                    fix_context = ""
                    try:
                        diag_response = llm_call(
                            model=coder_model,
                            prompt=diag_prompt,
                            system="Expert debugger. Output only valid JSON.",
                            max_tokens=1536,
                            temperature=0.05,
                        )
                        diag = json.loads(clean_json(diag_response))
                        if isinstance(diag, dict) and "file" in diag:
                            pf = diag["file"]
                            if pf in written_files:
                                problem_file = pf
                            else:
                                for fpath in written_files:
                                    if os.path.basename(fpath) == os.path.basename(pf):
                                        problem_file = fpath
                                        break
                            if problem_file:
                                fix_context = (
                                    f"\nDIAGNOSIS: {diag.get('root_cause', '')}\n"
                                    f"FIX STRATEGY: {diag.get('fix_strategy', '')}\n"
                                )
                                blog.info(f"Critic identified problem: {problem_file}")
                    except Exception:
                        pass

                    if not problem_file:
                        # Fallback: find entry point
                        for entry in ["main.py", "src/main.rs", "main.go", "index.js", "index.ts", "app.py"]:
                            if entry in written_files:
                                problem_file = entry
                                break

                    if not problem_file:
                        blog.warning("Critic cannot identify file to repair")
                        break

                    code = written_files[problem_file]
                    blog.repair(problem_file, attempt + 1, MAX_SANDBOX_RETRIES)

                    repair_prompt = f"""The following {language} project was generated but FAILED at runtime.

ERROR OUTPUT:
{sandbox_output[:3000]}
{fix_context}
FILE TO FIX: {problem_file}

CURRENT CODE:
{code[:10000]}

Fix the runtime error. Output ONLY the corrected complete code for {problem_file}.
No explanation, no fences."""

                    try:
                        response = llm_call(
                            model=coder_model,
                            prompt=repair_prompt,
                            system=f"Expert {language} developer. Fix runtime errors. Output ONLY code.",
                            max_tokens=14336,
                            temperature=0.05,
                        )
                        repaired = strip_fences(response)
                        full_path = os.path.join(output_dir, problem_file)
                        write_file(full_path, repaired)
                        written_files[problem_file] = repaired
                        blog.info(f"Repaired {problem_file} ({len(repaired)} chars)")
                    except Exception as exc:
                        blog.warning(f"Repair failed: {exc}")
                        break

                    # Re-test
                    test_ok, test_output = sandbox_test(output_dir, language)
                    if test_ok:
                        blog.verify(True, "critic_sandbox", f"Fixed on attempt {attempt + 1}")
                        ws["sandbox_ok"] = True
                        break
                    sandbox_output = test_output
                    blog.warning(f"Still failing after repair attempt {attempt + 1}")
                else:
                    blog.warning("Critic exhausted all repair attempts — project may have issues")

    # UX Polish phase (uses manager model for suggestions, coder for applying)
    if written_files:
        blog.phase("critic_polish", "Critic: UX polish review", model=manager_model)

        file_summaries = []
        for fpath, content in sorted(written_files.items()):
            lines = content.splitlines()[:60]
            preview = "\n".join(lines)
            file_summaries.append(f"=== {fpath} ({len(content)} chars) ===\n{preview}")

        project_context = "\n\n".join(file_summaries)
        if len(project_context) > 12000:
            project_context = project_context[:12000] + "\n\n... (truncated)"

        suggest_prompt = f"""You are a senior UX reviewer. A {language} project was built for this goal:

GOAL: "{ws['goal']}"

The project is COMPLETE. Suggest 1-2 small, high-impact improvements.

CURRENT PROJECT FILES:
{project_context}

Think about:
  - First-time user experience (start screens, instructions)
  - Error handling (friendly messages, not crashes)
  - Visual polish (colors, formatting, layout)
  - Missing standard features (exit option, help text)

RULES:
  1. Only modify EXISTING files
  2. No new dependencies
  3. No major architectural changes
  4. Each suggestion must be < 50 lines of code changes

Output ONLY this JSON:
""" + """{
  "suggestions": [
    {
      "file": "<file path to modify>",
      "what": "<1-sentence description>",
      "why": "<why a user would expect this>",
      "priority": <1-3>
    }
  ]
}

If project looks polished, return {"suggestions": []}.
Output ONLY JSON."""

        try:
            suggest_response = llm_call(
                model=manager_model,
                prompt=suggest_prompt,
                system="Senior UX reviewer. Output only valid JSON.",
                max_tokens=2048,
                temperature=0.2,
            )
            raw = json.loads(clean_json(suggest_response))
            if isinstance(raw, dict):
                suggestions = raw.get("suggestions", [])
                suggestions.sort(key=lambda s: s.get("priority", 99))

                for s in suggestions[:2]:
                    blog.polish_suggestion(
                        file=s.get("file", "?"),
                        what=s.get("what", "?"),
                        why=s.get("why", ""),
                        priority=s.get("priority", 1),
                    )

                # Apply top 1-2 suggestions using coder model
                for suggestion in suggestions[:2]:
                    target_file = suggestion.get("file", "")
                    what = suggestion.get("what", "")
                    why = suggestion.get("why", "")

                    if not target_file or target_file not in written_files:
                        for fpath in written_files:
                            if os.path.basename(fpath) == os.path.basename(target_file):
                                target_file = fpath
                                break
                        else:
                            continue

                    current_code = written_files[target_file]

                    apply_prompt = f"""Apply this improvement to the file below.

IMPROVEMENT: {what}
REASON: {why}

FILE: {target_file}

CURRENT CODE:
{current_code[:10000]}

RULES:
  1. Output the COMPLETE file with the improvement
  2. Do NOT break existing functionality
  3. Do NOT add new imports for external packages
  4. Output ONLY code, no fences"""

                    try:
                        apply_response = llm_call(
                            model=coder_model,
                            prompt=apply_prompt,
                            system=f"Expert {language} developer. Output ONLY code.",
                            max_tokens=14336,
                            temperature=0.1,
                        )
                        polished = strip_fences(apply_response)
                        if len(polished) >= len(current_code) * 0.5:
                            full_path = os.path.join(output_dir, target_file)
                            write_file(full_path, polished)
                            written_files[target_file] = polished
                            blog.polish_applied(file=target_file, what=what, chars=len(polished))
                    except Exception as exc:
                        blog.warning(f"Polish failed for {target_file}: {exc}")

        except Exception as exc:
            blog.warning(f"Polish phase failed: {exc}")

    ws["written_files"] = written_files
    return ws


# ---------------------------------------------------------------------------
# Agent Pipeline Runner
# ---------------------------------------------------------------------------
AGENT_SEQUENCE = [
    ("Planner",   agent_planner),
    ("Retriever", agent_retriever),
    ("Coder",     agent_coder),
    ("Executor",  agent_executor),
    ("Critic",    agent_critic),
]


def run_agent_pipeline(ws: dict) -> dict:
    """Run all 5 agents sequentially."""
    blog.info("=" * 70)
    blog.info("AI BUILDER v3 - AGENT PIPELINE")
    blog.info("=" * 70)
    blog.info(f"Agents  : {' -> '.join(name for name, _ in AGENT_SEQUENCE)}")
    blog.info(f"Project : {ws['goal']}")
    blog.info(f"Manager : {ws['manager_model']}")
    blog.info(f"Coder   : {ws['coder_model']}")
    blog.info(f"Context : {ws['ctx_tokens']:,} tokens")

    for agent_name, agent_fn in AGENT_SEQUENCE:
        try:
            blog.info(f"--- Starting agent: {agent_name} ---")
            ws = agent_fn(ws)
            blog.info(f"--- Agent {agent_name} completed ---")
        except Exception as exc:
            blog.error(f"Agent {agent_name} failed: {exc}", severity="fatal")
            ws["errors"].append(f"agent_{agent_name.lower()}: {exc}")
            if agent_name in ("Planner", "Coder"):
                # These are critical - abort pipeline
                raise
            # Retriever, Executor, Critic failures are non-fatal
            blog.warning(f"Continuing despite {agent_name} failure")

    return ws
