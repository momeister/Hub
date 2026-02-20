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
from skills.builder.engine.repair import repair_file
from skills.builder.engine.sandbox import sandbox_test
from skills.builder.engine.skeletons import generate_all_skeletons, fill_in_file


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
) -> dict:
    """Create initial workspace for the agent pipeline."""
    return {
        "goal": goal,
        "manager_model": manager_model,
        "coder_model": coder_model,
        "output_dir": output_dir,
        "ctx_tokens": ctx_tokens,
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

    prompt = f"""You are a code project reviewer. A blueprint was generated for this goal:

GOAL: "{ws['goal']}"

BLUEPRINT SUMMARY:
  Language: {language}
  Framework: {blueprint.get('framework', 'none')}
  Files ({len(files)}):
{file_summary}
  Dependency order: {dep_order[:20]}
  Dependencies: {dep_content[:300]}

Review for these issues ONLY:
1. WRONG LANGUAGE: Does the language fit the goal?
2. MISSING FILES: Are critical files missing? (entry point, config, core logic)
3. EXCESS FILES: Too many files for a simple project?
4. DEP ORDER: Will the build order cause import failures?
5. GOAL MISMATCH: Does the file plan actually implement what was asked?
6. MISSING FEATURES: Go through EVERY feature/requirement in the goal word by word.
   Does each requested feature have a corresponding file or function?
   Examples of commonly missed features:
   - "move history" -> needs a component/function to track and display moves
   - "bot/AI opponent" -> needs AI logic file/function
   - "UI" -> needs interactive event handlers (not just rendering)
   - "play against" -> needs game loop with turn management
   If ANY requested feature has no corresponding file, add it.

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

    try:
        response = llm_call(
            model=ws["manager_model"],
            prompt=prompt,
            system="You are a strict code reviewer. Output only valid JSON.",
            max_tokens=2048,
            temperature=0.05,
        )
        raw = json.loads(clean_json(response))
        if isinstance(raw, dict) and not raw.get("approved", True):
            patches = raw.get("patches", [])
            for patch in patches[:5]:
                action = patch.get("action", "")
                if action == "add_file":
                    path = patch.get("path", "")
                    purpose = patch.get("purpose", "")
                    if path and path not in {f["path"] for f in blueprint["files"]}:
                        blueprint["files"].append({
                            "path": path, "purpose": purpose,
                            "exports": [], "imports": [],
                            "estimated_lines": 30, "critical": False,
                        })
                        if path not in blueprint.get("dependency_order", []):
                            blueprint["dependency_order"].append(path)
                        blog.info(f"Planner added file: {path}")
                elif action == "remove_file":
                    path = patch.get("path", "")
                    if path:
                        before = len(blueprint["files"])
                        blueprint["files"] = [f for f in blueprint["files"] if f["path"] != path]
                        if len(blueprint["files"]) < before:
                            blueprint["dependency_order"] = [
                                p for p in blueprint.get("dependency_order", []) if p != path
                            ]
                            blog.info(f"Planner removed file: {path}")
                elif action == "reorder":
                    new_order = patch.get("dependency_order", [])
                    if new_order and isinstance(new_order, list):
                        valid_paths = {f["path"] for f in blueprint["files"]}
                        if all(p in valid_paths for p in new_order):
                            for p in blueprint.get("dependency_order", []):
                                if p not in new_order:
                                    new_order.append(p)
                            blueprint["dependency_order"] = new_order
                            blog.info("Planner reordered dependency chain")
            for issue in raw.get("issues", [])[:5]:
                blog.warning(f"Planner issue: {issue}")
        else:
            blog.info("Planner approved blueprint")
    except Exception as exc:
        blog.warning(f"Planner self-review failed ({exc}), proceeding")

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
    if deps.get("content"):
        manifest_map = {
            "requirements_txt": "requirements.txt",
            "package_json": "package.json",
            "cargo_toml": "Cargo.toml",
            "go_mod": "go.mod",
            "pom_xml": "pom.xml",
        }
        dep_type = deps.get("type", "")
        manifest_name = manifest_map.get(dep_type)
        if manifest_name:
            manifest_path = os.path.join(output_dir, manifest_name)
            if not os.path.exists(manifest_path):
                write_file(manifest_path, deps["content"])
                blog.info(f"Wrote manifest: {manifest_name}")

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

        max_repairs = MAX_REPAIR_ATTEMPTS.get(language, 3)
        repaired_ok = False

        for repair_attempt in range(1, max_repairs + 1):
            blog.repair(file_path, repair_attempt, max_repairs)
            repaired = repair_file(
                file_path, code, errors, language, coder_model,
                written_files=written_files,
            )
            write_file(full_path, repaired)
            written_files[file_path] = repaired
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

    ws["written_files"] = written_files
    return ws


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

    # Install dependencies in venv before sandbox
    if language == "python":
        req_path = os.path.join(output_dir, "requirements.txt")
        if os.path.exists(req_path):
            blog.info("Installing dependencies in virtual environment...")
            dep_ok, dep_msg = install_deps(output_dir, language)
            if dep_ok:
                blog.verify(True, "deps_install", "Dependencies installed in venv")
            else:
                blog.warning(f"Dependency install issue: {dep_msg}")

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
                blog.warning("Critic exhausted all repair attempts")

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
