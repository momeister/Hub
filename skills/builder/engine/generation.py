"""
skills/builder/engine/generation.py - Skeleton-fill-in generation
=================================================================
"""

from __future__ import annotations

import os

from skills.builder.engine.compile_checks import compile_check
from skills.builder.engine.context import blog, MAX_REPAIR_ATTEMPTS
from skills.builder.engine.formatters import format_code
from skills.builder.engine.manifest import validate_manifest
from skills.builder.engine.repair import repair_file, patch_repair_file
from skills.builder.engine.error_analysis import analyze_errors
from skills.builder.engine.skeletons import generate_all_skeletons, fill_in_file, _generate_single_skeleton
from skills.builder.engine.context import write_file
from skills.builder.engine.utils import sanitize_skeleton_paths
from core.llm_client import call_builder_session
from core.utils import estimate_tokens, strip_code_fences


def _compress_session(
    session_messages: list[dict],
    language: str,
    written_files: dict,
) -> list[dict]:
    """
    Baut eine neue Session auf mit geschriebenem Code als Kontext
    statt dem vollständigen Gesprächsverlauf.
    Bewahrt den System-Prompt, verwirft den alten Chat-Verlauf,
    fügt alle bereits geschriebenen Dateien als eine kompakte
    user-Message ein.
    """
    system_msg = session_messages[0]  # System-Prompt behalten
    code_summary = "\n\n".join(
        f"=== {path} (already written) ===\n{code}"
        for path, code in written_files.items()
    )
    return [
        system_msg,
        {"role": "user", "content":
            f"Context from previous session — these files are already written:\n\n"
            f"{code_summary}\n\nContinue with the next file."},
        {"role": "assistant", "content":
            "Understood. I have the context of all written files. "
            "Ready for the next file."},
    ]


def _build_fill_in_prompt(
    file_spec: dict,
    all_skeletons: dict,
    written_files: dict,
    blueprint: dict,
) -> str:
    """Build the fill-in prompt for a file (without LLM call)."""
    path = file_spec["path"]
    purpose = file_spec.get("purpose", "")
    language = blueprint["language"]

    ctx_parts = []

    # Project goal
    goal = blueprint.get("_goal", "") or blueprint.get("project_name", "").replace("_", " ")
    if goal:
        ctx_parts.append(f"=== PROJECT GOAL ===\n{goal}")

    # Data contracts (critical for consistency)
    contracts = blueprint.get("data_contracts", [])
    if contracts:
        contract_lines = [
            f"  {c['name']}: {c['structure']}\n  Example: {c['example']}"
            for c in contracts
        ]
        ctx_parts.append(
            "=== SHARED DATA CONTRACTS (MANDATORY — use EXACT field names) ===\n"
            + "\n\n".join(contract_lines)
        )

    # API endpoints (critical for full-stack)
    api_endpoints = blueprint.get("api_endpoints", [])
    if api_endpoints:
        ep_lines = [
            f"  {ep.get('method', '?')} {ep.get('path', '?')}: {ep.get('description', '')}"
            for ep in api_endpoints
        ]
        ctx_parts.append(
            "=== API ENDPOINTS (MANDATORY — use EXACT paths and methods) ===\n"
            + "\n".join(ep_lines)
        )

    # Fix 1: Show already-implemented files (from skeletons dict updated with finished code)
    implemented = {p: c for p, c in all_skeletons.items() if p in written_files}
    if implemented:
        ctx_parts.append("=== ALREADY IMPLEMENTED FILES ===")
        for impl_path, impl_code in implemented.items():
            ctx_parts.append(f"\n=== {impl_path} (IMPLEMENTED) ===\n{impl_code}")

    pending = {p: c for p, c in all_skeletons.items() if p not in written_files}
    if pending:
        ctx_parts.append("=== PENDING FILES (skeleton only, not yet implemented) ===")
        for sk_path, sk_code in pending.items():
            ctx_parts.append(f"\n=== {sk_path} ===\n{sk_code}")

    deps = blueprint.get("dependencies", {})
    if deps.get("content"):
        ctx_parts.append(f"\n\n=== DEPENDENCIES ({deps.get('type', '')}) ===\n{deps['content']}")

    context = "\n".join(ctx_parts)

    # Full-stack rules based on file type
    fullstack_rules = ""
    if api_endpoints:
        path_lower = path.lower()
        is_frontend = any(kw in path_lower for kw in [
            "frontend/", "client/", "src/", "public/",
            "api.js", "api.ts", "app.jsx", "app.tsx",
            ".html", ".jsx", ".tsx", ".vue", ".svelte",
        ])
        is_backend = any(kw in path_lower for kw in [
            "backend/", "server/", "routes", "app.py", "main.py",
            "server.py", "handler", "controller", "middleware",
        ])

        ep_ref = "\n".join(
            f"    {ep.get('method', '?')} {ep.get('path', '?')}"
            for ep in api_endpoints
        )

        if is_frontend:
            fullstack_rules = f"""
  FULL-STACK FRONTEND RULES:
  - API endpoint URLs MUST match the backend EXACTLY:
{ep_ref}
  - Use try-catch for ALL API calls with proper error handling
  - Do NOT implement business/game/domain logic — call the backend API instead
  - Every function must be FULLY implemented, no stubs or TODOs"""
        elif is_backend:
            fullstack_rules = f"""
  FULL-STACK BACKEND RULES:
  - Route paths MUST match EXACTLY:
{ep_ref}
  - Include CORS middleware for cross-origin requests
  - ALL business logic must be fully implemented here
  - Every function must be FULLY implemented, no stubs or TODOs"""

    return f"""Implement the file: {path}

Purpose: {purpose}

{context}

OUTPUT:
Complete implementation for {path}. Output ONLY the code, no fences, no explanation.

RULES:
  1. Use EXACT imports from skeletons
  2. Full implementation, no placeholders, no stubs, no TODO comments.
     FORBIDDEN: pass, ..., TODO, FIXME, empty function bodies, placeholder returns.
  3. Follow {language} idioms
  4. Handle errors properly
  5. Add brief comments for complex logic
  6. JAVASCRIPT: If using DOM, include DOMContentLoaded init pattern at end of file.
     If making API calls, define const API_BASE_URL = 'http://localhost:8000' and use it.
  7. PYTHON BACKEND: Include CORS middleware if using FastAPI/Flask.
     End with if __name__ == "__main__": uvicorn.run() or app.run().{fullstack_rules}"""


def skeleton_fill_in_generate(
    goal: str,
    blueprint: dict,
    coder_model: str,
    output_dir: str,
    ctx_tokens: int,
    ws: dict | None = None,
) -> dict:
    """Generate all files using skeleton-fill-in strategy."""
    language = blueprint["language"]
    files = blueprint["files"]
    dep_order = blueprint.get("dependency_order", [f["path"] for f in files])

    files_total = len(files)
    blog.phase("fill_in", f"Generating {files_total} files (skeleton-fill-in)", model=coder_model)

    skeletons = generate_all_skeletons(blueprint, coder_model)

    if not skeletons:
        blog.warning("Zero skeletons parsed from LLM output, retrying generation...")
        skeletons = generate_all_skeletons(blueprint, coder_model)

    if not skeletons:
        blog.error("Skeleton generation failed after 2 attempts", severity="fatal")
        raise RuntimeError("Could not generate any file skeletons from LLM output")

    # Fix: Strip subproject name prefix from skeleton paths to prevent
    # nested folders (e.g. output/backend/backend/main.py)
    skeletons = sanitize_skeleton_paths(skeletons, blueprint)

    expected = len(files)
    actual = len(skeletons)
    if actual < expected:
        blog.warning(f"Only {actual}/{expected} skeletons generated - some files may be missing")

    # Fix 2: Targeted retry for individually missing skeletons
    planned_paths = {f["path"] for f in files}
    missing_skeletons = planned_paths - set(skeletons.keys())
    if missing_skeletons:
        blog.warning(f"Einzelne Skeletons fehlen, generiere nach: {missing_skeletons}")
        for missing_path in sorted(missing_skeletons):
            missing_spec = next((f for f in files if f["path"] == missing_path), None)
            if not missing_spec:
                continue
            try:
                single_skeleton = _generate_single_skeleton(missing_spec, blueprint, coder_model)
                if single_skeleton:
                    skeletons[missing_path] = single_skeleton
                    blog.info(f"Skeleton nachgeneriert: {missing_path}")
            except Exception as exc:
                blog.warning(f"Konnte Skeleton für {missing_path} nicht generieren: {exc}")

    for path, skeleton in skeletons.items():
        full_path = os.path.join(output_dir, path)
        write_file(full_path, skeleton)

    # System 1: Load pre-written contract stubs into workspace and disk.
    _pre_written = blueprint.get("pre_written_files", {})
    if _pre_written:
        blog.info(f"Loading {len(_pre_written)} pre-written contract stub(s)")
        for _stub_path, _stub_code in _pre_written.items():
            _stub_full_path = os.path.join(output_dir, _stub_path)
            write_file(_stub_full_path, _stub_code)
            skeletons[_stub_path] = _stub_code

    blog.phase("manifest_validation", "Validating manifest and installing dependencies")
    validate_manifest(output_dir, language, coder_model, skeletons)

    # NOTE: We intentionally skip compile-checking skeletons here.
    # Skeletons are empty stubs (pass/...) so mypy will always flag
    # "Missing return statement [empty-body]". Those are expected and
    # would only trigger pointless repair loops.

    written_files: dict[str, str] = {}

    # System 1: Pre-populate written_files with contract stubs so the Coder
    # treats them as already-implemented files.
    for _stub_path, _stub_code in _pre_written.items():
        written_files[_stub_path] = _stub_code

    file_index = 0

    # Persistent builder session
    session_messages = [
        {"role": "system", "content": (
            f"Expert {language} developer. "
            "Output ONLY code, no markdown fences, no explanation.\n\n"
            "CRITICAL RULES:\n"
            "1. NEVER leave empty functions, stubs, or placeholders. Every function MUST be fully implemented.\n"
            "   FORBIDDEN: pass, ..., TODO, FIXME, 'implement later', empty function bodies, placeholder returns.\n"
            "2. For frontend JavaScript files:\n"
            "   - ALWAYS define const API_BASE_URL = 'http://localhost:8000' at the top if making API calls\n"
            "   - ALWAYS use DOMContentLoaded or document.readyState check to initialize:\n"
            "     if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', init); } else { init(); }\n"
            "   - ALWAYS connect ALL event listeners in an init() function\n"
            "   - NEVER use relative fetch URLs — always use fetch(`${API_BASE_URL}/path`)\n"
            "3. For Python backend (FastAPI/Flask):\n"
            "   - ALWAYS include CORS middleware (CORSMiddleware with allow_origins=['*'])\n"
            "   - ALWAYS end with: if __name__ == '__main__': uvicorn.run(app, host='127.0.0.1', port=8000)\n"
            "4. Every function that is defined MUST contain real, working implementation logic.\n"
            "5. ALL imports must resolve to actual files or installed packages."
        )}
    ]

    for file_path in dep_order:
        file_spec = next((f for f in files if f["path"] == file_path), None)
        if not file_spec:
            blog.warning(f"File {file_path} in dep order but not in plan, skipping")
            continue

        # System 1: Skip files that are pre-written contract stubs.
        if file_path in blueprint.get("pre_written_files", {}):
            blog.info(f"Skipping pre-written stub: {file_path}")
            continue

        file_index += 1
        blog.file_start(file_path, file_index, files_total)

        # Token-Budget prüfen vor jedem Call
        estimated = estimate_tokens(str(session_messages))
        max_session_tokens = int(ctx_tokens * 0.75)  # 75% für Input, 25% für Output
        if estimated > max_session_tokens:
            blog.warning("Session context near limit — compressing history")
            session_messages = _compress_session(session_messages, language, written_files)

        fill_in_prompt = _build_fill_in_prompt(file_spec, skeletons, written_files, blueprint)
        session_messages.append({"role": "user", "content": fill_in_prompt})

        raw_code, session_messages = call_builder_session(
            model=coder_model,
            session_messages=session_messages,
            max_tokens=14336,
            temperature=0.1,
        )
        code = strip_code_fences(raw_code)

        # System 2: Contract verification against OpenAPI spec (if present)
        openapi_spec = blueprint.get("openapi_spec", {})
        if openapi_spec and openapi_spec.get("paths") and file_path.endswith((".py", ".js", ".ts", ".jsx", ".tsx")):
            try:
                from skills.builder.engine.contract_verifier import run_contract_verification_loop
                from skills.builder.engine.context import llm_call as _cv_llm_call
                # Use the real ws so violations are tracked in the main workspace.
                # Fall back to a minimal dict if ws was not passed.
                _cv_target_ws = ws if ws is not None else {
                    "openapi_spec": openapi_spec,
                    "contract_violations": [],
                }
                code = run_contract_verification_loop(
                    _cv_target_ws, file_path, code,
                    max_repair_attempts=2,
                    llm_call_fn=_cv_llm_call,
                    coder_model=coder_model,
                )
            except Exception as _cv_exc:
                blog.warning(f"Contract verification skipped for {file_path}: {_cv_exc}")

        full_path = os.path.join(output_dir, file_path)
        write_file(full_path, code)
        written_files[file_path] = code
        skeletons[file_path] = code  # Fix 1: Replace skeleton with finished code for subsequent files

        blog.file_done(file_path, len(code))

        success, all_errors = compile_check(output_dir, language)

        # Filter: only act on errors in the file we just wrote.
        # Other files are still skeletons (empty stubs) so they will
        # always have errors — we must not repair the current file
        # because of skeleton errors in OTHER files.
        file_basename = os.path.basename(file_path)
        errors = [
            e for e in all_errors
            if file_basename in e or file_path in e
        ]

        if not errors:
            # No errors in THIS file — skip repair even if skeletons elsewhere fail
            continue

        if errors:
            blog.warning(f"Compile errors after {file_path}: {len(errors)}")
            for err in errors[:3]:
                blog.error(err, file=file_path, severity="compile")

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
                            fname_stem = os.path.splitext(os.path.basename(fpath))[0]
                            if fname_stem in err:
                                enriched_errors.append(
                                    f"[CONTEXT] Content of {fpath} (referenced in error):\n{fcode[:5000]}"
                                )
                                break

                # Fix 8: Use patch repair from attempt 2 onwards
                if repair_attempt == 1:
                    repaired = repair_file(
                        file_path,
                        code,
                        enriched_errors,
                        language,
                        coder_model,
                        written_files=written_files,
                    )
                else:
                    repaired = patch_repair_file(
                        file_path,
                        code,
                        enriched_errors,
                        language,
                        coder_model,
                        written_files=written_files,
                    )

                write_file(full_path, repaired)
                written_files[file_path] = repaired
                skeletons[file_path] = repaired  # Fix 1: Also update skeletons during repair
                code = repaired
                format_code(output_dir, language)

                success2, all_errors2 = compile_check(output_dir, language)
                # Filter to current file only
                errors2 = [
                    e for e in all_errors2
                    if file_basename in e or file_path in e
                ]
                if not errors2:
                    blog.verify(True, "compile", f"{file_path} repaired")
                    repaired_ok = True
                    break
                errors = errors2
                if repair_attempt == max_repairs:
                    blog.warning(f"Could not fix {file_path} after {max_repairs} repair attempts")

            if not repaired_ok:
                blog.phase("nuclear_regen", f"Fresh regeneration for {file_path}")
                file_spec_extra = dict(file_spec)
                error_summary = "\n".join(errors[:10])
                file_spec_extra["purpose"] = (
                    f"{file_spec.get('purpose', '')} "
                    f"[PREVIOUS ATTEMPT HAD ERRORS - avoid these: {error_summary[:500]}]"
                )
                fresh_code = fill_in_file(
                    file_spec_extra,
                    skeletons,
                    written_files,
                    blueprint,
                    coder_model,
                    ctx_tokens,
                )
                write_file(full_path, fresh_code)
                written_files[file_path] = fresh_code
                code = fresh_code
                format_code(output_dir, language)

                success3, all_errors3 = compile_check(output_dir, language)
                # Filter to current file only
                errors3 = [
                    e for e in all_errors3
                    if file_basename in e or file_path in e
                ]
                if not errors3:
                    blog.verify(True, "compile", f"{file_path} fixed via nuclear regen")
                else:
                    blog.error(
                        f"Nuclear regen also failed for {file_path} ({len(errors3)} errors) - continuing",
                        file=file_path,
                        severity="warning",
                    )

    return written_files
