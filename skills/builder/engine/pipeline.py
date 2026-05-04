"""
skills/builder/engine/pipeline.py - Build orchestration
=======================================================
Uses the 5-agent sequential pipeline:
  Planner -> Retriever -> Coder -> Executor -> Critic

Each agent runs one at a time (sequential model loading for VRAM efficiency).
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import time
from pathlib import Path

from skills.builder.engine.agents import (
    make_workspace,
    agent_planner,
    agent_retriever,
)
from skills.builder.engine.artifacts import (
    generate_readme,
    generate_multi_language_readme,
    generate_project_start_bat,
)
from skills.builder.engine.blueprint import architect_phase, data_contract_phase
from skills.builder.engine.context import blog, llm_call, DEFAULT_CTX_TOKENS, IN_DOCKER, set_active_output_dir, _meaningful_lines
from skills.builder.engine.critic import critic_review_blueprint

# File-based approval signal -- uses the output volume so host and container
# share the same mount point (host writes, container reads).
_SIGNAL_DIR = "/app/output"
_APPROVAL_SIGNAL = os.path.join(_SIGNAL_DIR, ".build_approval")
_BUILD_TOKEN_FILE = os.path.join(_SIGNAL_DIR, ".build_token")


def _generate_build_token() -> str:
    """Generate and write a cryptographic build approval token."""
    token = secrets.token_hex(32)
    try:
        os.makedirs(_SIGNAL_DIR, mode=0o700, exist_ok=True)
        with open(_BUILD_TOKEN_FILE, "w") as f:
            f.write(token)
    except OSError as e:
        blog.warning(f"Could not write build token: {e}")
    return token


def _cleanup_build_token():
    """Remove the build token file."""
    try:
        if os.path.exists(_BUILD_TOKEN_FILE):
            os.remove(_BUILD_TOKEN_FILE)
    except OSError:
        pass


def _cleanup_approval_signal():
    """Remove stale approval signal file."""
    try:
        if os.path.exists(_APPROVAL_SIGNAL):
            os.remove(_APPROVAL_SIGNAL)
    except OSError as _e:
        blog.warning(f"[non-critical] Could not remove approval signal: {_e}")


def _cleanup_docker_venvs(output_dir: str) -> None:
    """Remove .venv directories created inside Docker.

    When the builder runs in Docker (Linux) but the output is mounted from
    a Windows host, the .venv contains Linux symlinks (lib64 -> lib) that
    cause [WinError 1920] on Windows.  The project_start.bat creates a
    fresh, Windows-native .venv when the user runs the project.
    """
    if not IN_DOCKER:
        return  # Only clean up when running in Docker

    for venv_dir in Path(output_dir).rglob(".venv"):
        if venv_dir.is_dir():
            try:
                shutil.rmtree(str(venv_dir))
                blog.info(f"Cleaned up Docker .venv: {venv_dir.relative_to(output_dir)}")
            except Exception as exc:
                blog.warning(f"Could not remove .venv {venv_dir}: {exc}")


def _wait_for_approval(timeout: int = 7200, build_token: str = "") -> str:
    """Poll for approval signal file written by the gateway.

    Returns 'approved' or 'cancelled'.
    The approval file must contain 'approved:<token>' with a valid build_token.
    """
    if not build_token:
        blog.error(
            "_wait_for_approval aufgerufen ohne build_token – verweigere Approval",
            severity="security",
        )
        return "cancelled"

    _cleanup_approval_signal()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(_APPROVAL_SIGNAL):
            try:
                with open(_APPROVAL_SIGNAL, "r") as f:
                    content = f.read().strip().lower()
                os.remove(_APPROVAL_SIGNAL)
                parts = content.split(":", 1)
                if parts[0] == "approved" and len(parts) == 2 and parts[1] == build_token:
                    _cleanup_build_token()
                    return "approved"
                elif parts[0] == "cancelled":
                    _cleanup_build_token()
                    return "cancelled"
                else:
                    blog.warning("Approval signal has invalid or missing token")
                    _cleanup_build_token()
                    return "cancelled"
            except OSError:
                _cleanup_build_token()
                return "cancelled"
        time.sleep(1)
    _cleanup_build_token()
    return "cancelled"  # timeout


# -- Manifest type lookup for each language --------------------------------
_LANG_MANIFEST = {
    "python":     ("requirements_txt", "requirements.txt"),
    "javascript": ("package_json",     "package.json"),
    "typescript": ("package_json",     "package.json"),
    "rust":       ("cargo_toml",       "Cargo.toml"),
    "go":         ("go_mod",           "go.mod"),
}


def _extract_sub_dependencies(
    master_blueprint: dict,
    sub_name: str,
    sub_lang: str,
    sub_files: list[dict],
) -> dict:
    """Extract dependency info for a subproject from the master blueprint.

    Strategy (in priority order):
    1. If master ``dependencies.content`` is non-empty AND the master
       dependency type matches this subproject's language, use it directly.
    2. If the sub_files list contains a manifest file for this language
       (e.g. ``requirements.txt``, ``package.json``), extract its content
       from the file spec so the Retriever can write it.
    3. Return a stub with the correct *type* so Agent Retriever knows it
       must generate a manifest even when ``content`` is empty.
    """
    manifest_info = _LANG_MANIFEST.get(sub_lang.lower())
    if not manifest_info:
        return {}

    dep_type, manifest_filename = manifest_info

    # --- Strategy 1: master-level deps match this subproject's language ---
    master_deps = master_blueprint.get("dependencies") or {}
    if master_deps.get("content") and master_deps.get("type") == dep_type:
        return dict(master_deps)  # shallow copy

    # --- Strategy 2: manifest content lives inside sub_files spec ---------
    for fspec in sub_files:
        fname = fspec.get("file", "")
        if fname.endswith(manifest_filename):
            # The file spec itself may carry a description that lists deps
            desc = fspec.get("description", "")
            if desc:
                return {"type": dep_type, "content": "", "_hint": desc}
            break

    # --- Strategy 3: stub so Retriever generates one ----------------------
    return {"type": dep_type, "content": ""}


def _sanitize_sub_blueprint(
    sub_blueprint: dict, sub_name: str, all_sub_names: list[str]
) -> dict:
    """Clean up a sub-blueprint generated by architect_phase for a subproject.

    Fixes two problems:
    1. File paths prefixed with the subproject's own name (e.g. ``backend/main.py``
       when output_dir is already ``output/backend``) → strips the prefix.
    2. File paths belonging to OTHER subprojects (e.g. ``frontend/index.html``
       in a backend blueprint) → removes them entirely.
    """
    own_prefix = sub_name + "/"
    other_prefixes = [n + "/" for n in all_sub_names if n != sub_name]

    cleaned_files = []
    removed_paths = []

    for f in sub_blueprint.get("files", []):
        fpath = f["path"]

        # Remove files that belong to another subproject
        if any(fpath.startswith(op) for op in other_prefixes):
            removed_paths.append(fpath)
            continue

        # Strip own subproject prefix (architect wrote "backend/main.py" → "main.py")
        if fpath.startswith(own_prefix):
            f = dict(f)
            f["path"] = fpath[len(own_prefix):]

        cleaned_files.append(f)

    if removed_paths:
        blog.warning(
            f"Removed {len(removed_paths)} files belonging to other subprojects: "
            f"{', '.join(removed_paths[:5])}"
        )

    if len(cleaned_files) != len(sub_blueprint.get("files", [])):
        blog.info(
            f"Sanitized sub-blueprint for '{sub_name}': "
            f"{len(sub_blueprint['files'])} → {len(cleaned_files)} files"
        )

    sub_blueprint["files"] = cleaned_files

    # Also fix dependency_order
    cleaned_dep_order = []
    for fpath in sub_blueprint.get("dependency_order", []):
        if any(fpath.startswith(op) for op in other_prefixes):
            continue
        if fpath.startswith(own_prefix):
            fpath = fpath[len(own_prefix):]
        cleaned_dep_order.append(fpath)
    sub_blueprint["dependency_order"] = cleaned_dep_order

    # Enforce correct language / flags
    sub_blueprint["is_multi_language"] = False
    sub_blueprint["subprojects"] = []

    return sub_blueprint


def _validate_cross_subproject_api(
    output_dir: str,
    subprojects: list[dict],
    blueprint: dict,
    coder_model: str,
    ctx_tokens: int,
) -> None:
    """Validate API consistency between frontend and backend subprojects.

    After ALL subprojects are built, reads the actual generated code from
    each subproject and uses the LLM to detect API mismatches:
    - Different endpoint URLs
    - Different request/response field names
    - Missing CORS setup
    - Business logic duplicated in frontend that belongs in backend
    - Stub/placeholder functions that were never implemented
    """
    from skills.builder.engine.context import llm_call, clean_json, strip_fences, write_file as ctx_write_file

    if len(subprojects) < 2:
        return

    blog.phase("api_validation", "Cross-subproject API consistency check", model=coder_model)

    # Collect source code from each subproject
    sub_code: dict[str, dict[str, str]] = {}
    for sub in subprojects:
        sub_name = sub["name"]
        sub_dir = os.path.join(output_dir, sub_name)
        sub_code[sub_name] = {}
        if not os.path.isdir(sub_dir):
            continue
        for root, _dirs, files in os.walk(sub_dir):
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in (".py", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".html", ".vue", ".svelte"):
                    continue
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, sub_dir).replace("\\", "/")
                try:
                    content = open(fpath, "r", encoding="utf-8", errors="ignore").read()
                    sub_code[sub_name][rel] = content
                except Exception as _e:
                    blog.warning(f"[non-critical] Could not read {fpath}: {_e}")

    if not any(sub_code.values()):
        blog.warning("No source code found for API validation")
        return

    # Build a combined view for the LLM
    parts = []
    for sub_name, files in sub_code.items():
        for fpath, content in sorted(files.items()):
            # Limit per file to keep within context
            preview = content[:2000] if len(content) > 2000 else content
            parts.append(f"=== {sub_name}/{fpath} ===\n{preview}")

    all_code = "\n\n".join(parts)
    # Fix 5: Hard limit on total context size
    max_chars = min(int(ctx_tokens * 2.0), 200_000)

    # Fix 5: If still too large, filter to main/entry-point files only
    PRIORITY_FILES = {"main.py", "app.py", "server.py", "index.js", "api.js", "routes.py"}
    if len(all_code) > 150_000:
        blog.warning("Code zu groß für API-Validation, filtere auf Haupt-Dateien")
        priority_parts = []
        for sub_name, files in sub_code.items():
            for fpath, content in sorted(files.items()):
                if os.path.basename(fpath) in PRIORITY_FILES:
                    preview = content[:2000] if len(content) > 2000 else content
                    priority_parts.append(f"=== {sub_name}/{fpath} ===\n{preview}")
        if priority_parts:
            all_code = "\n\n".join(priority_parts)

    if len(all_code) > max_chars:
        all_code = all_code[:max_chars] + "\n... (truncated)"

    # API endpoints from master blueprint for reference
    api_endpoints = blueprint.get("api_endpoints", [])
    api_ref = ""
    if api_endpoints:
        ep_lines = [
            f"  {ep.get('method','?')} {ep.get('path','?')}: {ep.get('description','')}"
            for ep in api_endpoints
        ]
        api_ref = "PLANNED API ENDPOINTS:\n" + "\n".join(ep_lines) + "\n\n"

    contracts = blueprint.get("data_contracts", [])
    contracts_ref = ""
    if contracts:
        ct_lines = [f"  {c.get('name','?')}: {c.get('structure','')}" for c in contracts]
        contracts_ref = "PLANNED DATA CONTRACTS:\n" + "\n".join(ct_lines) + "\n\n"

    validation_prompt = f"""Review the following multi-component project for API CONSISTENCY issues.

{api_ref}{contracts_ref}
ACTUAL GENERATED CODE:
{all_code}

Find CONCRETE issues in these categories:

1. ENDPOINT MISMATCH: Frontend calls a URL that the backend doesn't serve, or uses wrong HTTP method
   Example: Frontend calls POST /api/games but backend only has POST /games

2. FIELD NAME MISMATCH: Frontend reads response.gameId but backend sends game_id
   Example: Backend returns {{"board_fen": "..."}} but frontend reads response.board

3. REQUEST BODY MISMATCH: Frontend sends a different structure than backend expects
   Example: Frontend sends {{"move_uci": "e2e4"}} but backend expects {{"from": "e2", "to": "e4"}}

4. MISSING CORS: Backend has no CORS middleware but frontend makes cross-origin requests

5. DUPLICATED LOGIC: Frontend re-implements logic that should be a backend API call
   Example: Frontend has its own chess move validation instead of calling backend

6. STUB FUNCTIONS: Functions that are called but have empty bodies (pass, TODO, placeholder)

For each issue, provide the EXACT fix needed.

Output ONLY valid JSON:
{{
  "issues": [
    {{
      "category": "endpoint_mismatch|field_mismatch|request_mismatch|missing_cors|duplicated_logic|stub_function",
      "severity": "critical|warning",
      "frontend_file": "<file in frontend>",
      "backend_file": "<file in backend>",
      "description": "<what is wrong>",
      "fix_target": "<frontend|backend>",
      "fix_file": "<which file to fix>",
      "fix_description": "<exactly what to change>"
    }}
  ]
}}

If NO issues found, return {{"issues": []}}. Output ONLY JSON."""

    try:
        response = llm_call(
            model=coder_model,
            prompt=validation_prompt,
            system="Expert full-stack code reviewer. Find API inconsistencies. Output ONLY valid JSON.",
            max_tokens=8192,
            temperature=0.05,
        )
        raw = json.loads(clean_json(response))
        issues = raw.get("issues", []) if isinstance(raw, dict) else []
    except Exception as exc:
        blog.warning(f"Cross-subproject API validation failed: {exc}")
        return

    if not issues:
        blog.verify(True, "api_consistency", "No API consistency issues found between subprojects")
        return

    critical_issues = [i for i in issues if i.get("severity") == "critical"]
    warning_issues = [i for i in issues if i.get("severity") != "critical"]

    blog.warning(
        f"Found {len(issues)} API consistency issue(s) "
        f"({len(critical_issues)} critical, {len(warning_issues)} warnings)"
    )
    for issue in issues[:10]:
        blog.warning(
            f"  [{issue.get('category','?')}] {issue.get('description','?')} "
            f"(fix: {issue.get('fix_file','?')})"
        )

    # Auto-fix critical issues
    fixed_count = 0
    for issue in critical_issues[:8]:
        fix_target_sub = issue.get("fix_target", "")
        fix_file = issue.get("fix_file", "")

        if not fix_target_sub or not fix_file:
            continue

        # Determine which subproject directory to fix
        fix_sub_name = None
        for sub in subprojects:
            if sub["name"] == fix_target_sub:
                fix_sub_name = sub["name"]
                break
        if not fix_sub_name:
            # Try matching by name in the fix_file path
            for sub_name in sub_code:
                if fix_file in sub_code[sub_name]:
                    fix_sub_name = sub_name
                    break
        if not fix_sub_name:
            continue

        fix_dir = os.path.join(output_dir, fix_sub_name)
        fix_path = os.path.join(fix_dir, fix_file)

        if not os.path.exists(fix_path):
            # Try alternative: fix_file might include the subproject name
            alt_file = fix_file.replace(f"{fix_sub_name}/", "")
            fix_path = os.path.join(fix_dir, alt_file)
            fix_file = alt_file

        if not os.path.exists(fix_path):
            blog.warning(f"Cannot fix {fix_file}: file not found at {fix_path}")
            continue

        try:
            current_code = open(fix_path, "r", encoding="utf-8").read()
        except Exception as _e:
            blog.warning(f"[non-critical] Could not read {fix_path}: {_e}")
            continue

        # Gather the counterpart file content for context
        counterpart_sub = issue.get("backend_file", "") if fix_target_sub != "backend" else issue.get("frontend_file", "")
        counterpart_code = ""
        for sn, sf in sub_code.items():
            if counterpart_sub in sf:
                counterpart_code = f"\n\n=== COUNTERPART FILE ({sn}/{counterpart_sub}) ===\n{sf[counterpart_sub][:5000]}"
                break

        fix_prompt = f"""Fix this API consistency issue in a full-stack project.

ISSUE: {issue.get('description', '')}
CATEGORY: {issue.get('category', '')}
FIX NEEDED: {issue.get('fix_description', '')}

CURRENT FILE ({fix_file}):
{current_code[:10000]}
{counterpart_code}

RULES:
  1. Output the COMPLETE fixed file
  2. Fix ONLY the described issue
  3. Do NOT break any existing functionality
  4. Do NOT add new dependencies
  5. Ensure all API URLs, field names, and request/response structures match
  6. Output ONLY code, no fences, no explanation"""

        try:
            fix_response = llm_call(
                model=coder_model,
                prompt=fix_prompt,
                system="Expert developer. Fix API consistency issues. Output ONLY code.",
                max_tokens=14336,
                temperature=0.1,
            )
            fixed_code = strip_fences(fix_response)

            # Safety: don't shrink files drastically (Fix 6: meaningful lines check)
            original_lines = _meaningful_lines(current_code)
            fixed_lines = _meaningful_lines(fixed_code)
            if original_lines == 0 or fixed_lines >= original_lines * 0.6:
                ctx_write_file(fix_path, fixed_code)
                if fix_sub_name in sub_code and fix_file in sub_code[fix_sub_name]:
                    sub_code[fix_sub_name][fix_file] = fixed_code
                fixed_count += 1
                blog.info(f"Fixed API issue in {fix_sub_name}/{fix_file}: {issue.get('description','')[:80]}")
            else:
                blog.warning(f"Fix for {fix_file} too few meaningful lines ({fixed_lines} vs {original_lines}), skipping")
        except Exception as exc:
            blog.warning(f"Auto-fix failed for {fix_file}: {exc}")

    if fixed_count:
        blog.verify(True, "api_fixes", f"Fixed {fixed_count}/{len(critical_issues)} critical API issues")
    elif critical_issues:
        blog.warning(f"Could not auto-fix {len(critical_issues)} critical API issues")


def _integration_smoke_test(
    output_dir: str,
    subprojects: list[dict],
    blueprint: dict,
) -> None:
    """Integration smoke test for full-stack projects.

    Scans frontend files for fetch()/axios calls, extracts the URLs,
    and compares them against planned api_endpoints in the blueprint.
    Logs warnings for any URL called in frontend but not planned in backend.
    No automatic fixes — logging only.
    """
    import re as _re

    api_endpoints = blueprint.get("api_endpoints", [])
    if not api_endpoints:
        blog.info("No api_endpoints in blueprint, skipping integration smoke test")
        return

    blog.phase("integration_smoke", "Integration smoke test: frontend URLs vs backend endpoints")

    # Collect all planned endpoint paths
    planned_paths = set()
    for ep in api_endpoints:
        path = ep.get("path", "")
        if path:
            # Normalize: remove path params like {game_id} -> *
            normalized = _re.sub(r'\{[^}]+\}', '*', path)
            planned_paths.add(normalized)
            planned_paths.add(path)  # also keep original

    # Scan frontend subproject files for fetch/axios URLs
    frontend_urls: list[tuple[str, str]] = []  # (file, url)
    for sub in subprojects:
        sub_name = sub.get("name", "").lower()
        # Only scan frontend-like subprojects
        if sub_name not in ("frontend", "client", "web"):
            continue
        sub_dir = os.path.join(output_dir, sub["name"])
        if not os.path.isdir(sub_dir):
            continue
        for root, _dirs, files in os.walk(sub_dir):
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".vue", ".svelte", ".html"):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    content = open(fpath, "r", encoding="utf-8", errors="ignore").read()
                except Exception:
                    continue
                rel_path = os.path.relpath(fpath, output_dir).replace("\\", "/")

                # Extract fetch() URLs
                for m in _re.finditer(r"""fetch\s*\(\s*[`'"](.*?)[`'"]""", content):
                    url = m.group(1)
                    # Strip template expressions like ${API_BASE_URL}
                    url = _re.sub(r'\$\{[^}]+\}', '', url)
                    if url.startswith("/"):
                        frontend_urls.append((rel_path, url))

                # Extract axios URLs
                for m in _re.finditer(r"""axios\.(?:get|post|put|delete|patch)\s*\(\s*[`'"](.*?)[`'"]""", content, _re.IGNORECASE):
                    url = m.group(1)
                    url = _re.sub(r'\$\{[^}]+\}', '', url)
                    if url.startswith("/"):
                        frontend_urls.append((rel_path, url))

    if not frontend_urls:
        blog.info("No frontend API calls found to validate")
        blog.verify(True, "integration_smoke", "No frontend API calls to check")
        return

    # Compare each frontend URL against planned endpoints
    mismatches: list[str] = []
    for file_path, url in frontend_urls:
        # Normalize URL: strip query params, replace path segments that look like IDs
        clean_url = url.split("?")[0]
        # Replace UUID-like or numeric segments with *
        normalized_url = _re.sub(r'/[a-f0-9-]{8,}', '/*', clean_url)
        normalized_url = _re.sub(r'/\d+', '/*', normalized_url)

        # Check if this URL matches any planned endpoint
        matched = False
        for planned in planned_paths:
            # Simple matching: exact match or wildcard match
            if clean_url == planned or normalized_url == planned:
                matched = True
                break
            # Check if planned path (with wildcards) could match
            planned_pattern = planned.replace("*", "[^/]+")
            if _re.fullmatch(planned_pattern, clean_url):
                matched = True
                break
        if not matched:
            mismatches.append(f"{file_path} calls {url} — no matching backend endpoint")

    # Deduplicate
    mismatches = list(dict.fromkeys(mismatches))

    if mismatches:
        blog.warning(f"Integration smoke test: {len(mismatches)} URL mismatch(es)")
        for mm in mismatches[:15]:
            blog.warning(f"  {mm}")
    else:
        blog.info("All frontend API URLs match planned backend endpoints")

    blog.verify(
        len(mismatches) == 0,
        "integration_smoke",
        f"{len(frontend_urls)} frontend URL(s) checked, {len(mismatches)} mismatch(es)"
    )


def _validate_api_contract_completeness(blueprint: dict, manager_model: str) -> dict:
    """Pre-build validation of API endpoint definitions.

    Checks that all api_endpoints have complete request_body and response_body
    definitions and that field naming is consistent (no snake_case/camelCase mix).
    Corrects the blueprint in-place before any code is generated.
    """
    from skills.builder.engine.context import llm_call, clean_json

    api_endpoints = blueprint.get("api_endpoints", [])
    if not api_endpoints:
        return blueprint

    blog.phase("api_contract_precheck", "Validating API contract completeness", model=manager_model)

    ep_summary = json.dumps(api_endpoints, indent=2, default=str)
    contracts = blueprint.get("data_contracts", [])
    contracts_summary = json.dumps(contracts, indent=2, default=str) if contracts else "[]"

    prompt = f"""Review these API endpoint definitions for a full-stack project and fix any issues.

API ENDPOINTS:
{ep_summary}

DATA CONTRACTS:
{contracts_summary}

CHECK FOR:
1. Missing request_body or response_body (every endpoint must have both, even if empty {{}})
2. Inconsistent field naming — all fields must use snake_case (not camelCase)
3. Missing error response definitions
4. Endpoints without a clear HTTP method
5. Duplicate or contradictory endpoints

OUTPUT the corrected api_endpoints array as JSON:
{{
  "api_endpoints": [
    {{
      "method": "GET|POST|PUT|DELETE",
      "path": "/exact/path",
      "request_body": {{}},
      "response_body": {{}},
      "description": "what it does"
    }}
  ],
  "fixes_applied": ["description of each fix"]
}}

If no fixes needed, return the original endpoints unchanged.
Output ONLY JSON."""

    try:
        response = llm_call(
            model=manager_model,
            prompt=prompt,
            system="API design expert. Validate and fix endpoint definitions. Output ONLY valid JSON.",
            max_tokens=4096,
            temperature=0.05,
        )
        raw = json.loads(clean_json(response))
        if isinstance(raw, dict):
            fixed_endpoints = raw.get("api_endpoints", [])
            fixes = raw.get("fixes_applied", [])
            if fixed_endpoints:
                blueprint["api_endpoints"] = fixed_endpoints
                if fixes:
                    for fix in fixes[:10]:
                        blog.info(f"API contract fix: {fix}")
                    blog.info(f"API contract precheck: applied {len(fixes)} fix(es)")
                else:
                    blog.info("API contract precheck: all endpoints complete")
    except Exception as exc:
        blog.warning(f"API contract precheck failed ({exc}), continuing with existing definitions")

    return blueprint


def build_single_language_project(
    goal: str,
    blueprint: dict,
    manager_model: str,
    coder_model: str,
    output_dir: str,
    ctx_tokens: int,
    parent_goal: str = "",
    openapi_spec: dict | None = None,
) -> dict:
    """Build a single-language project using the 5-agent pipeline."""
    # Set allowed write scope for this (sub-)project
    set_active_output_dir(output_dir)
    language = blueprint["language"]
    # Ensure the goal is accessible to coder agents via the blueprint
    blueprint.setdefault("_goal", goal)

    # Generate data contracts if not already present
    if not blueprint.get("data_contracts"):
        blueprint = data_contract_phase(blueprint, manager_model)
        # ws updated below after assignment

    blog.info(f"Language: {language}")
    blog.info("Mode: AGENT PIPELINE (Planner->Retriever->Coder->Executor->Critic)")

    # Create workspace with pre-set blueprint (skips Planner agent)
    # BUILDER_REVIEW_MODEL: env var for co-reviewer model; falls back to manager_model
    _review_model = os.environ.get("BUILDER_REVIEW_MODEL", "") or manager_model
    ws = make_workspace(goal, manager_model, coder_model, output_dir, ctx_tokens,
                        review_model=_review_model)
    ws["blueprint"] = blueprint
    ws["language"] = language

    # Propagate OpenAPI spec from parent multi-language build (System 1+2)
    if openapi_spec:
        ws["openapi_spec"] = openapi_spec

    # Run Retriever -> Coder -> Executor -> Critic
    from skills.builder.engine.agents import (
        agent_retriever,
        agent_coder,
        agent_executor,
        agent_critic,
        AGENT_SEQUENCE,
    )

    sub_agents = [
        ("Retriever", agent_retriever),
        ("Coder", agent_coder),
        ("Executor", agent_executor),
        ("Critic", agent_critic),
    ]

    for agent_name, agent_fn in sub_agents:
        try:
            blog.info(f"--- Starting agent: {agent_name} ---")
            ws = agent_fn(ws)
            blog.info(f"--- Agent {agent_name} completed ---")
        except Exception as exc:
            blog.error(f"Agent {agent_name} failed: {exc}", severity="fatal")
            if agent_name in ("Coder",):
                raise
            blog.warning(f"Continuing despite {agent_name} failure")

    files = ws["written_files"]

    generate_readme(goal, blueprint, files, output_dir, manager_model, coder_model, parent_goal=parent_goal)
    generate_project_start_bat(blueprint, output_dir, files)

    return files


def _record_build_in_memory(
    goal: str,
    blueprint: dict | None,
    ws: dict,
    start_time: float,
    success: bool,
) -> None:
    """Record build result in Knowledge Graph and Knowledge Base (Systems 3+4).

    Called both on success and failure so that failed builds (the most
    valuable learning data) are also captured.
    """
    if blueprint is None:
        return  # No blueprint = planner didn't even finish

    try:
        from skills.builder.engine.knowledge_graph import record_build_result
        record_build_result(
            goal=goal,
            blueprint=blueprint,
            written_files=ws.get("written_files", {}),
            errors_encountered=ws.get("all_errors", []),
            repairs_applied=ws.get("all_repairs", []),
            success=success,
            build_duration=time.time() - start_time,
        )
    except Exception as _kg_rec_exc:
        blog.warning(f"KG record skipped: {_kg_rec_exc}")

    try:
        from skills.builder.engine.knowledge_base import get_knowledge_base
        kb = get_knowledge_base()
        if kb.available:
            kb.store_project_experience(
                goal=goal,
                language=blueprint.get("language", ""),
                framework=blueprint.get("framework", ""),
                success=success,
                key_decisions=[
                    d.get("decision", "")
                    for d in blueprint.get("architecture_decisions", [])
                ],
                problems_encountered=[
                    e.get("message", str(e))[:200]
                    for e in ws.get("all_errors", [])[:10]
                ],
                solutions_applied=[
                    r.get("description", str(r))[:200]
                    for r in ws.get("all_repairs", [])[:10]
                ],
            )
    except Exception as _kb_rec_exc:
        blog.warning(f"KB record skipped: {_kb_rec_exc}")


def build_project(
    goal: str,
    manager_model: str = "gpt-oss:120b",
    coder_model: str = "qwen3-coder-next",
    output_dir: str = "./output",
    ctx_tokens: int = DEFAULT_CTX_TOKENS,
) -> None:
    start_time = time.time()

    blog.info("=" * 70)
    blog.info("AI BUILDER v3 - AGENT PIPELINE")
    blog.info("=" * 70)
    blog.info(f"Project : {goal}")
    blog.info(f"Manager : {manager_model}")
    blog.info(f"Coder   : {coder_model}")
    blog.info(f"Context : {ctx_tokens:,} tokens")
    blog.info(f"Agents  : Planner -> Retriever -> Coder -> Executor -> Critic")

    os.makedirs(output_dir, exist_ok=True)
    set_active_output_dir(output_dir)

    # --- System 3+4: Query Knowledge Graph & Knowledge Base BEFORE planning ---
    kg_context = {}
    kb_context = ""
    try:
        from skills.builder.engine.knowledge_graph import query_similar_projects
        kg_context = query_similar_projects(goal)
        if kg_context.get("summary_for_prompt"):
            blog.info("KG: Found relevant past project data")
    except Exception as _kg_exc:
        blog.warning(f"KG query skipped: {_kg_exc}")

    try:
        from skills.builder.engine.knowledge_base import get_knowledge_base
        kb = get_knowledge_base()
        if kb.available:
            kb_context = kb.build_context_for_new_project(goal, "", "")
            if kb_context:
                blog.info("KB: Found relevant past experiences")
    except Exception as _kb_exc:
        blog.warning(f"KB query skipped: {_kb_exc}")

    # Agent 1: Planner (uses manager model)
    # BUILDER_REVIEW_MODEL: env var for co-reviewer model; falls back to manager_model
    _review_model = os.environ.get("BUILDER_REVIEW_MODEL", "") or manager_model
    ws = make_workspace(goal, manager_model, coder_model, output_dir, ctx_tokens,
                        review_model=_review_model)

    # Inject KG/KB context into workspace for planner
    ws["kg_context"] = kg_context
    ws["kb_context"] = kb_context

    try:
        ws = agent_planner(ws)
    except Exception:
        _record_build_in_memory(goal, ws.get("blueprint"), ws, start_time, success=False)
        raise
    blueprint = ws["blueprint"]
    # Critic läuft bereits intern in agent_planner — kein zweiter Call nötig

    if os.environ.get("TRIGGERED_BY") == "telegram":
        file_paths = [f["path"] for f in blueprint.get("files", [])]
        arch_decisions = blueprint.get("architecture_decisions", [])
        build_token = _generate_build_token()
        blog.approval_needed(
            language=blueprint.get("language", "?"),
            framework=blueprint.get("framework", ""),
            why=blueprint.get("why", ""),
            files_total=len(file_paths),
            file_paths=file_paths,
            complexity=blueprint.get("estimated_complexity", ""),
            architecture_decisions=[d.get("decision", "") for d in arch_decisions],
            build_token=build_token,
        )
        blog.info("Waiting for user approval via Telegram...")
        approval = _wait_for_approval(build_token=build_token)
        if approval != "approved":
            blog.info("Build cancelled by user")
            # Clean up empty output directory so rejected builds leave no trace
            try:
                if os.path.isdir(output_dir) and not os.listdir(output_dir):
                    os.rmdir(output_dir)
            except OSError as _e:
                blog.warning(f"[non-critical] Could not remove empty output dir: {_e}")
            blog.complete(
                success=False,
                files_written=0,
                elapsed_sec=int(time.time() - start_time),
                output_dir=output_dir,
            )
            return

        blog.phase("approved", "User approved tech stack and plan - continuing build")

    # Generate data contracts for cross-file consistency
    if not blueprint.get("data_contracts"):
        blueprint = data_contract_phase(blueprint, manager_model)
        ws["blueprint"] = blueprint

    # Full-stack enforcement: if the user goal clearly requires multi-language
    # (e.g. "backend + frontend", "REST API with web UI") but the architect
    # returned a single-language blueprint, re-run the architect with explicit
    # full-stack instructions.
    if not blueprint.get("is_multi_language"):
        _goal_lower = goal.lower()
        _fullstack_signals = [
            ("backend" in _goal_lower and "frontend" in _goal_lower),
            ("server" in _goal_lower and "client" in _goal_lower),
            ("api" in _goal_lower and any(kw in _goal_lower for kw in ["ui", "web", "browser", "frontend"])),
            ("full-stack" in _goal_lower or "fullstack" in _goal_lower or "full stack" in _goal_lower),
            ("rest" in _goal_lower and any(kw in _goal_lower for kw in ["react", "vue", "svelte", "html"])),
        ]
        if any(_fullstack_signals):
            blog.warning(
                "Goal requires full-stack (backend + frontend) but architect returned single-language. "
                "Re-running architect with explicit multi-language instructions."
            )
            try:
                blueprint = architect_phase(
                    f"{goal}\n\n"
                    "CRITICAL: This is a FULL-STACK project. You MUST return a multi-language blueprint "
                    "with is_multi_language=true and at least two subprojects (backend + frontend). "
                    "The backend and frontend MUST be separate subprojects with different languages. "
                    "Do NOT build this as a single-language project.",
                    manager_model,
                )
                if blueprint.get("is_multi_language") and blueprint.get("subprojects"):
                    blog.info(
                        f"Re-architect succeeded: {len(blueprint.get('subprojects', []))} subprojects"
                    )
                    ws["blueprint"] = blueprint
                    # Regenerate data contracts for the new blueprint
                    if not blueprint.get("data_contracts"):
                        blueprint = data_contract_phase(blueprint, manager_model)
                        ws["blueprint"] = blueprint
                else:
                    blog.warning(
                        "Re-architect still returned single-language — proceeding with original blueprint"
                    )
            except Exception as exc:
                blog.warning(f"Full-stack re-architect failed ({exc}), proceeding with original blueprint")

    if blueprint.get("is_multi_language") and blueprint.get("subprojects"):
        blog.info("Multi-language project detected")

        subprojects = blueprint["subprojects"]
        master_files = blueprint.get("files", [])
        # Collect all subproject names so each sub-architect knows what to exclude
        all_sub_names = [s["name"] for s in subprojects]

        # Pre-build: validate API contract completeness before generating any code
        # Skip if all endpoints already have complete request/response bodies
        api_eps = blueprint.get("api_endpoints", [])
        incomplete_eps = any(
            not ep.get("request_body") or not ep.get("response_body")
            for ep in api_eps
        )
        if incomplete_eps:
            blueprint = _validate_api_contract_completeness(blueprint, manager_model)
        else:
            blog.info("API contract completeness check skipped (all endpoints already complete)")

        # --- System 1: Contract-First OpenAPI Spec + Stub Generation ---
        try:
            from skills.builder.engine.openapi_contract import (
                generate_openapi_spec,
                generate_backend_stubs,
                generate_frontend_client_stubs,
                inject_stubs_into_blueprint,
            )

            spec = generate_openapi_spec(blueprint, llm_call, manager_model)
            ws["openapi_spec"] = spec

            # Determine backend/frontend languages & frameworks
            backend_lang = ""
            backend_fw = ""
            frontend_fw = ""
            for sp in subprojects:
                sp_name = sp.get("name", "").lower()
                if sp_name in ("backend", "server", "api"):
                    backend_lang = sp.get("language", "")
                    backend_fw = sp.get("framework", "")
                elif sp_name in ("frontend", "client", "web"):
                    frontend_fw = sp.get("framework", "")

            if backend_lang:
                backend_stubs = generate_backend_stubs(spec, backend_lang, backend_fw)
                frontend_stubs = generate_frontend_client_stubs(spec, frontend_fw)
                blueprint = inject_stubs_into_blueprint(blueprint, backend_stubs, frontend_stubs)
                ws["blueprint"] = blueprint
                blog.info(
                    f"Contract-First: generated {len(backend_stubs)} backend + "
                    f"{len(frontend_stubs)} frontend stubs from OpenAPI spec"
                )
            else:
                blog.warning("Contract-First: could not determine backend language, skipping stubs")

        except Exception as _oa_exc:
            blog.warning(f"Contract-First system skipped: {_oa_exc}")

        # Sort subprojects: backend first so frontend can reference backend code
        def _sub_sort_key(sub):
            name = sub.get("name", "").lower()
            if name in ("backend", "server", "api"):
                return 0
            if name in ("frontend", "client", "web"):
                return 2
            return 1
        subprojects = sorted(subprojects, key=_sub_sort_key)
        blog.info(f"Subproject build order: {[s['name'] for s in subprojects]}")

        # Track files already assigned to a subproject to prevent double-assignment
        assigned_files: set[str] = set()

        # Manifests that belong to specific languages — don't assign cross-language
        LANG_MANIFESTS = {
            "requirements.txt": "python",
            "setup.py": "python",
            "pyproject.toml": "python",
            "Pipfile": "python",
            "package.json": ("javascript", "typescript"),
            "package-lock.json": ("javascript", "typescript"),
            "tsconfig.json": "typescript",
            "Cargo.toml": "rust",
            "Cargo.lock": "rust",
            "go.mod": "go",
            "go.sum": "go",
        }

        for sub in subprojects:
            sub_name = sub["name"]
            sub_lang = sub["language"]
            sub_dir = os.path.join(output_dir, sub_name)

            blog.phase("subproject", f"Building {sub_name} ({sub_lang})")

            sub_files = []
            for f in master_files:
                fpath = f["path"]
                prefix = sub_name + "/"
                if fpath.startswith(prefix):
                    sub_file = dict(f)
                    sub_file["path"] = fpath[len(prefix):]
                    sub_files.append(sub_file)

            if not sub_files:
                # Fallback: assign files without subproject prefix by file extension
                LANG_EXTENSIONS = {
                    "python":     (".py", ".txt", ".cfg", ".ini", ".toml", ".env"),
                    "javascript": (".js", ".mjs", ".cjs", ".json", ".html", ".css"),
                    "typescript": (".ts", ".tsx", ".mts", ".json", ".html", ".css"),
                    "html":       (".html", ".css", ".js", ".svg"),
                    "go":         (".go", ".mod", ".sum"),
                    "rust":       (".rs", ".toml"),
                }
                exts = LANG_EXTENSIONS.get(sub_lang.lower(), ())
                for f in master_files:
                    fpath = f["path"]
                    # Skip files already assigned to another subproject
                    if fpath in assigned_files:
                        continue
                    # Skip files that belong to a different subproject
                    if any(fpath.startswith(other + "/") for other in all_sub_names if other != sub_name):
                        continue
                    # Skip manifests that belong to a different language
                    basename = os.path.basename(fpath)
                    manifest_lang = LANG_MANIFESTS.get(basename)
                    if manifest_lang:
                        if isinstance(manifest_lang, tuple):
                            if sub_lang.lower() not in manifest_lang:
                                continue
                        elif sub_lang.lower() != manifest_lang:
                            continue
                    if fpath.endswith(exts):
                        sub_files.append(dict(f))

                if sub_files:
                    blog.info(
                        f"Recovered {len(sub_files)} files for '{sub_name}' via extension matching "
                        f"(planner omitted subproject prefix)"
                    )

            if not sub_files:
                # Build list of OTHER subprojects so the architect knows NOT to include them
                other_subs = [s for s in all_sub_names if s != sub_name]
                other_subs_str = ", ".join(other_subs) if other_subs else "none"

                # Build API contract context so the sub-architect knows the exact endpoints
                api_contract_context = ""
                master_api = blueprint.get("api_endpoints", [])
                master_contracts = blueprint.get("data_contracts", [])
                if master_api:
                    ep_lines = [f"  {ep.get('method','?')} {ep.get('path','?')}: {ep.get('description','')}" for ep in master_api]
                    api_contract_context += (
                        "\n\nMANDATORY API ENDPOINTS (these are shared with the other subprojects — "
                        "you MUST use these EXACT paths, methods, request/response bodies):\n"
                        + "\n".join(ep_lines)
                    )
                if master_contracts:
                    ct_lines = [f"  {c.get('name','?')}: {c.get('structure','')}" for c in master_contracts]
                    api_contract_context += (
                        "\n\nMANDATORY DATA CONTRACTS (use these EXACT field names and types):\n"
                        + "\n".join(ct_lines)
                    )

                blog.warning(f"No files found for subproject '{sub_name}', running architect")
                sub_blueprint = architect_phase(
                    f"Build ONLY the {sub_name} component ({sub_lang}/{sub.get('framework','')}) "
                    f"for: {goal}. "
                    f"Use {sub_lang} with {sub.get('framework','')}. "
                    f"Do NOT change the language or framework. "
                    f"CRITICAL: This is ONLY the '{sub_name}' part of a multi-component project. "
                    f"The following components are built SEPARATELY and must NOT be included: {other_subs_str}. "
                    f"Do NOT create any files for other components. "
                    f"Do NOT create any subdirectories named '{sub_name}/' — files go at the root level. "
                    f"For example use 'main.py' NOT '{sub_name}/main.py'."
                    f"{api_contract_context}",
                    manager_model,
                )
                sub_blueprint = critic_review_blueprint(
                    f"{sub_name} for: {goal}", sub_blueprint, manager_model
                )
                # Safety: strip any file paths that start with the subproject's own name
                # (architect may still prefix them despite instructions)
                sub_blueprint = _sanitize_sub_blueprint(
                    sub_blueprint, sub_name, all_sub_names
                )
                # Enforce approved language/framework — sub-architect may choose differently
                if sub_blueprint.get("language", "").lower() != sub_lang.lower():
                    blog.warning(
                        f"Sub-architect chose '{sub_blueprint.get('language')}' for '{sub_name}', "
                        f"enforcing approved language '{sub_lang}'"
                    )
                    sub_blueprint["language"] = sub_lang
                if sub.get("framework") and not sub_blueprint.get("framework"):
                    sub_blueprint["framework"] = sub.get("framework", "")
                # Propagate master API endpoints and contracts to sub-blueprint
                if master_api:
                    sub_blueprint["api_endpoints"] = master_api
                if master_contracts:
                    sub_blueprint["data_contracts"] = master_contracts
            else:
                sub_dep_order = [
                    fpath[len(sub_name + "/"):]
                    for fpath in blueprint.get("dependency_order", [])
                    if fpath.startswith(sub_name + "/")
                ]
                if not sub_dep_order:
                    sub_dep_order = [f["path"] for f in sub_files]

                sub_blueprint = {
                    "project_name": sub_name,
                    "language": sub_lang,
                    "framework": sub.get("framework", ""),
                    "why": blueprint.get("why", ""),
                    "is_multi_language": False,
                    "files": sub_files,
                    "dependency_order": sub_dep_order,
                    "dependencies": _extract_sub_dependencies(blueprint, sub_name, sub_lang, sub_files),
                    "architecture_decisions": blueprint.get("architecture_decisions", []),
                    "safe_stack_violations": [],
                    "estimated_complexity": blueprint.get("estimated_complexity", "medium"),
                    "subprojects": [],
                    "api_endpoints": blueprint.get("api_endpoints", []),
                    "data_contracts": blueprint.get("data_contracts", []),
                }

                blog.info(
                    f"Derived sub-blueprint for '{sub_name}': {len(sub_files)} files, lang={sub_lang}"
                )

            try:
                build_single_language_project(
                    goal=f"{sub_name} for: {goal}",
                    blueprint=sub_blueprint,
                    manager_model=manager_model,
                    coder_model=coder_model,
                    output_dir=sub_dir,
                    ctx_tokens=ctx_tokens,
                    parent_goal=goal,
                    openapi_spec=ws.get("openapi_spec", {}),
                )
            except Exception:
                _record_build_in_memory(goal, blueprint, ws, start_time, success=False)
                raise

            # Track assigned files to prevent double-assignment in extension matching
            for f in sub_blueprint.get("files", []):
                assigned_files.add(f["path"])
                # Also track with prefix for prefix-matched files
                assigned_files.add(sub_name + "/" + f["path"])

        # Cross-subproject API consistency validation
        _validate_cross_subproject_api(
            output_dir, subprojects, blueprint, coder_model, ctx_tokens
        )

        # Integration smoke test: verify frontend URLs match backend endpoints
        _integration_smoke_test(output_dir, subprojects, blueprint)

        generate_multi_language_readme(goal, subprojects, output_dir)
        generate_project_start_bat(blueprint, output_dir, {}, subprojects=subprojects)
    else:
        try:
            build_single_language_project(goal, blueprint, manager_model, coder_model, output_dir, ctx_tokens)
        except Exception:
            _record_build_in_memory(goal, blueprint, ws, start_time, success=False)
            raise

    # Clean up Docker-created .venv directories (Linux symlinks break on Windows)
    _cleanup_docker_venvs(output_dir)

    elapsed = int(time.time() - start_time)
    blog.complete(
        success=True,
        files_written=len([f for f in Path(output_dir).rglob("*") if f.is_file()]),
        elapsed_sec=elapsed,
        output_dir=output_dir,
    )

    # Projekt in Memory speichern
    try:
        from core.memory import get_memory
        memory = get_memory()
        lang = blueprint.get("language", "?")
        fw = blueprint.get("framework", "")
        proj_name = blueprint.get("project_name", os.path.basename(output_dir))
        memory.add_project(
            name=proj_name,
            language=lang,
            summary=goal[:200],
            framework=fw,
        )
    except Exception as _e:
        blog.warning(f"[non-critical] Memory storage failed: {_e}")

    # --- System 3+4: Record build result in Knowledge Graph & Knowledge Base ---
    _record_build_in_memory(goal, blueprint, ws, start_time, success=True)
