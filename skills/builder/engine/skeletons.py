"""
skills/builder/engine/skeletons.py - Skeleton generation
========================================================
"""

from __future__ import annotations

import json
import os

from core.utils import estimate_tokens

from skills.builder.engine.context import blog, llm_call, strip_fences
from skills.builder.engine.utils import parse_multi_file_output


def _build_prioritized_context(
    goal_text: str,
    contracts: list,
    written_files: dict,
    pending_skeletons: dict,
    deps: dict,
    ctx_tokens: int,
    current_file_path: str,
    api_endpoints: list | None = None,
    stub_instructions: list | None = None,
) -> str:
    """
    Baut den Kontext in Prioritätsreihenfolge auf und schneidet nur
    die am wenigsten wichtigen Teile ab, wenn das Limit erreicht wird.

    Priorität (höchste zuerst):
    1. PROJECT GOAL + DATA CONTRACTS  — immer vollständig
    2. PENDING SKELETONS              — immer vollständig (klein, kritisch)
    3. DEPENDENCIES                   — immer vollständig (klein, kritisch)
    4. DIRECTLY IMPORTED files        — vollständig (meist 1-2 Dateien)
    5. OTHER IMPLEMENTED files        — gekürzt auf je 50 Zeilen
    """
    max_chars = int(ctx_tokens * 0.8 * 3.5)  # Token → char estimate

    parts_critical = []   # Immer drin, nie gekürzt
    parts_important = []  # Vollständig, wenn Platz vorhanden
    parts_optional = []   # Gekürzt, wenn nötig

    # Prio 1: Goal + Contracts (immer)
    if goal_text:
        parts_critical.append(f"=== PROJECT GOAL ===\n{goal_text}")
    if contracts:
        contract_lines = [
            f"  {c['name']}: {c['structure']}\n  Example: {c['example']}"
            for c in contracts
        ]
        parts_critical.append(
            "=== SHARED DATA CONTRACTS (MANDATORY) ===\n" + "\n\n".join(contract_lines)
        )
    if api_endpoints:
        ep_lines = [
            f"  {ep.get('method', '?')} {ep.get('path', '?')}: {ep.get('description', '')}"
            for ep in api_endpoints
        ]
        parts_critical.append(
            "=== API ENDPOINTS (MANDATORY — use EXACT paths) ===\n" + "\n".join(ep_lines)
        )

    # Stub instructions from Contract-First system (System 1)
    if stub_instructions:
        parts_critical.append(
            "=== CONTRACT STUBS (MANDATORY — do NOT modify signatures) ===\n"
            + "\n".join(f"  - {inst}" for inst in stub_instructions)
        )

    # Prio 2: Pending Skeletons (immer — der Coder braucht die Interfaces)
    if pending_skeletons:
        sk_lines = ["=== PENDING FILES (skeleton only, not yet implemented) ==="]
        for sk_path, sk_code in pending_skeletons.items():
            sk_lines.append(f"\n=== {sk_path} ===\n{sk_code}")
        parts_critical.append("\n".join(sk_lines))

    # Prio 3: Dependencies (immer)
    if deps.get("content"):
        parts_critical.append(
            f"=== DEPENDENCIES ({deps.get('type', '')}) ===\n{deps['content']}"
        )

    # Prio 4: Direkt importierte Dateien (vollständig)
    # Heuristik: Dateien deren Name im current file skeleton vorkommt
    directly_imported = {}
    other_implemented = {}
    current_skeleton = pending_skeletons.get(current_file_path, "")
    for w_path, w_code in written_files.items():
        stem = os.path.splitext(os.path.basename(w_path))[0]
        if stem.lower() in current_skeleton.lower():
            directly_imported[w_path] = w_code
        else:
            other_implemented[w_path] = w_code

    if directly_imported:
        di_lines = ["=== DIRECTLY IMPORTED FILES (full code) ==="]
        for w_path, w_code in directly_imported.items():
            di_lines.append(f"\n=== {w_path} (fully implemented) ===\n{w_code}")
        parts_important.append("\n".join(di_lines))

    # Prio 5: Andere implementierte Dateien (gekürzt auf 50 Zeilen)
    if other_implemented:
        oi_lines = ["=== OTHER IMPLEMENTED FILES (first 50 lines each) ==="]
        for w_path, w_code in other_implemented.items():
            preview = "\n".join(w_code.splitlines()[:50])
            oi_lines.append(f"\n=== {w_path} (implemented, preview) ===\n{preview}")
        parts_optional.append("\n".join(oi_lines))

    # Zusammenbauen mit Budgetüberwachung
    critical_str = "\n\n".join(parts_critical)
    important_str = "\n\n".join(parts_important)
    optional_str = "\n\n".join(parts_optional)

    result = critical_str
    remaining = max_chars - len(result)

    if remaining > 0 and important_str:
        if len(important_str) <= remaining:
            result += "\n\n" + important_str
            remaining -= len(important_str)
        else:
            result += "\n\n" + important_str[:remaining]
            remaining = 0

    if remaining > 0 and optional_str:
        if len(optional_str) <= remaining:
            result += "\n\n" + optional_str
        else:
            result += "\n\n" + optional_str[:remaining]

    return result


def generate_all_skeletons(blueprint: dict, coder_model: str) -> dict:
    """Generate skeletons for all files, batching if the project is large."""
    language = blueprint["language"]
    files_list = blueprint["files"]

    total_estimated = sum(f.get("estimated_lines", 30) for f in files_list)

    # Batch large projects to avoid exceeding LLM context limits
    if total_estimated > 500 and len(files_list) > 6:
        blog.phase(
            "skeleton_batch",
            f"Large project ({len(files_list)} files, ~{total_estimated} lines) — batching skeleton generation",
            model=coder_model,
        )
        return _generate_skeletons_batched(blueprint, coder_model)

    return _generate_skeletons_single(blueprint, coder_model)


def _generate_skeletons_batched(blueprint: dict, coder_model: str) -> dict:
    """Generate skeletons in batches for large projects."""
    files_list = blueprint["files"]
    all_skeletons: dict[str, str] = {}

    # Split into batches of ~6 files each
    batch_size = 6
    batches = [files_list[i:i + batch_size] for i in range(0, len(files_list), batch_size)]

    blog.info(f"Splitting {len(files_list)} files into {len(batches)} batches of ~{batch_size}")

    for batch_idx, batch_files in enumerate(batches, 1):
        blog.phase(
            "skeleton_batch_n",
            f"Skeleton batch {batch_idx}/{len(batches)} ({len(batch_files)} files)",
            model=coder_model,
        )

        # Create a sub-blueprint with just this batch's files
        batch_blueprint = dict(blueprint)
        batch_blueprint["files"] = batch_files

        # Include already-generated skeletons as context so later batches
        # know about earlier files' interfaces
        batch_blueprint["_prior_skeletons"] = dict(all_skeletons)

        batch_skeletons = _generate_skeletons_single(batch_blueprint, coder_model)
        all_skeletons.update(batch_skeletons)
        blog.info(f"Batch {batch_idx}: generated {len(batch_skeletons)} skeletons (total: {len(all_skeletons)})")

    return all_skeletons


def _generate_skeletons_single(blueprint: dict, coder_model: str) -> dict:
    """Generate skeletons for all files in one call."""
    language = blueprint["language"]
    files_list = blueprint["files"]

    blog.phase("skeleton", f"Generating skeletons for {len(files_list)} files", model=coder_model)

    file_specs = []
    for f in files_list:
        spec = f"  - {f['path']}: {f['purpose']}\n    Exports: {', '.join(f.get('exports', []))}"
        file_specs.append(spec)
    file_specs_str = "\n".join(file_specs)

    dep_info = ""
    deps = blueprint.get("dependencies", {})
    if deps.get("content"):
        dep_info = f"\nDEPENDENCIES ({deps.get('type', '')}):\n{deps['content']}\n"

    # Extract the FULL project goal for context (not just the name)
    goal = blueprint.get("_goal", "") or blueprint.get("project_name", "").replace("_", " ")

    # Build data contracts section
    contracts = blueprint.get("data_contracts", [])
    contracts_section = ""
    if contracts:
        contract_lines = []
        for c in contracts:
            contract_lines.append(
                f"  {c['name']}: {c['description']}\n"
                f"    Structure: {c['structure']}\n"
                f"    Example: {c['example']}\n"
                f"    Used in: {', '.join(c.get('consumed_by', []) + c.get('produced_by', []))}"
            )
        contracts_section = (
            "\nSHARED DATA CONTRACTS (CRITICAL — use these EXACT structures, do NOT deviate):\n"
            + "\n".join(contract_lines)
            + "\n"
        )

    # Build API endpoints section for full-stack projects
    api_endpoints = blueprint.get("api_endpoints", [])
    api_section = ""
    if api_endpoints:
        ep_lines = []
        for ep in api_endpoints:
            ep_lines.append(
                f"  {ep.get('method', '?')} {ep.get('path', '?')}: {ep.get('description', '')}\n"
                f"    Request: {json.dumps(ep.get('request_body', {}))}\n"
                f"    Response: {json.dumps(ep.get('response_body', {}))}"
            )
        api_section = (
            "\nAPI ENDPOINTS (CRITICAL — frontend and backend MUST use these EXACT paths and formats):\n"
            + "\n".join(ep_lines)
            + "\n"
        )

    # Include prior skeletons from earlier batches (for batched generation)
    prior_section = ""
    prior_skeletons = blueprint.get("_prior_skeletons", {})
    if prior_skeletons:
        prior_parts = []
        for p_path, p_code in sorted(prior_skeletons.items()):
            # Truncate to keep context manageable
            preview = p_code[:1500] if len(p_code) > 1500 else p_code
            prior_parts.append(f"=== {p_path} ===\n{preview}")
        prior_section = (
            "\nALREADY GENERATED SKELETONS (from previous batch — use these interfaces):\n"
            + "\n\n".join(prior_parts)
            + "\n"
        )

    prompt = f"""Generate SKELETONS (signatures only, NO implementations) for ALL files in this {language} project.

PROJECT: "{blueprint.get('project_name', '')}"
FULL GOAL: "{goal}"
{contracts_section}
{api_section}
{prior_section}
{dep_info}
FILES TO CREATE:
{file_specs_str}

Output format:
=== path/to/file1.ext ===
[skeleton with imports, types, function signatures, NO implementations]
=== END ===

=== path/to/file2.ext ===
[skeleton...]
=== END ===

RULES (CRITICAL):
  1. ALL files in ONE response
  2. Signatures/types/interfaces ONLY — but annotated
  3. For every function: signature + one-line comment with EXACT return format
     JS example:   // returns: {{from:[row,col], to:[row,col], capture:piece|null}}
     Py example:   # returns: list[tuple[int,int]] — all valid (row,col) positions
     Rust example: // returns: Ok(Vec<Move>) or Err(InvalidMoveError)
  4. For every shared data structure: add a concrete example as comment
     JS example:   // Board: board[row][col] = "wK"|"bP"|null, row 0 = rank 8
     Py example:   # GameState: {{"board": [[str|None]*8]*8, "turn": "w"|"b",
     #               "castling": {{"wK":bool,...}}, "en_passant": [row,col]|None}}
  5. For structs/classes: fields + method signatures
  6. Imports must be CORRECT (use the exports list)
  7. NO implementation bodies
  8. Config files (Cargo.toml, package.json, etc.) should be COMPLETE
  9. For dependency manifests (requirements.txt, package.json, Cargo.toml):
     - Do NOT pin exact versions (no ==1.2.3)
     - Use loose version constraints (>=1.0 or just the package name)
     - Only include packages that ACTUALLY EXIST on PyPI/npm/crates.io
     - Prefer well-known, popular packages
  10. EVERY feature from the project goal MUST have corresponding function signatures.
      If the goal mentions "move history", there MUST be functions for recording/displaying moves.
      If the goal mentions "bot/AI", there MUST be functions for AI move selection.
      If the goal mentions "UI", there MUST be event handlers for user interaction (click, drag, input).
  11. FILE PATHS: Use EXACTLY the file paths from the FILES list above.
      Do NOT add any folder prefix to the paths. If the file is listed as "main.py",
      output "=== main.py ===" NOT "=== backend/main.py ===" or "=== project_name/main.py ===".
      The output directory is already set correctly — adding prefixes creates nested folders.
  12. For Python web apps (FastAPI/Flask), the entry point MUST include:
      if __name__ == "__main__": with uvicorn.run() or app.run()
  13. FULL-STACK API INTEGRATION (for projects with both frontend and backend):
      a. Frontend API module: every API function signature MUST have a doc-comment specifying:
         - Endpoint URL and HTTP method (e.g. POST /games)
         - Request body structure
         - Response body structure
         - Transformed return type (what the frontend actually uses)
         Example:
         // createGame() - POST /games
         // Request: {{}} (empty)
         // Response: {{ game_id: string, board_fen: string, turn: string, status: string }}
         // Returns: {{ gameId: string, board: string[][], turn: string, status: string }}
         function createGame() {{ }}
      b. Include data transformation utility signatures when backend/frontend formats differ:
         - fenToBoard(fen) — convert backend format to frontend renderable format
         - coordsToUCI(from, to) — convert UI coordinates to backend move format
         - Any other conversions needed based on the data contracts
      c. Backend route handler signatures MUST match the exact URL paths from the API contracts
      d. Backend MUST include CORS middleware setup in the entry point skeleton
      e. Frontend API calls MUST use the EXACT same URL paths as backend routes
      f. LOGIC SEPARATION: Business logic, game logic, AI/bot logic, and domain rules
         belong ONLY in backend files. Frontend files should only have:
         - API call functions (fetch/axios wrappers)
         - UI rendering and event handling
         - Data transformation utilities (converting backend format to UI format)
         Frontend must NOT have functions that duplicate backend computation."""

    system = f"Expert {language} architect. Output ONLY skeletons, no implementations."

    response = llm_call(
        model=coder_model,
        prompt=prompt,
        system=system,
        max_tokens=16384,
    )

    skeletons = parse_multi_file_output(response)
    blog.info(f"Generated {len(skeletons)} skeletons")

    return skeletons


def _generate_single_skeleton(file_spec: dict, blueprint: dict, coder_model: str) -> str:
    """Generate the skeleton for exactly one file as a targeted fallback."""
    path = file_spec["path"]
    purpose = file_spec.get("purpose", "")
    language = blueprint.get("language", "unknown")
    exports = file_spec.get("exports", [])
    imports_needed = file_spec.get("imports", [])

    prompt = f"""Generate a skeleton (stubs only, no implementation) for this {language} file.

FILE: {path}
PURPOSE: {purpose}
MUST EXPORT: {exports}
IMPORTS FROM OTHER PROJECT FILES: {imports_needed}

Rules:
- Function/class/variable stubs only (pass / ... / empty body)
- Correct import statements at the top
- NO implementation logic
- Output ONLY the file content, no fences, no explanation."""

    response = llm_call(
        model=coder_model,
        prompt=prompt,
        system=f"Expert {language} developer. Output ONLY skeleton code.",
        max_tokens=2048,
        temperature=0.05,
    )
    return strip_fences(response).strip()


def fill_in_file(
    file_spec: dict,
    all_skeletons: dict,
    written_files: dict,
    blueprint: dict,
    coder_model: str,
    ctx_tokens: int,
) -> str:
    """Fill in implementation for one file with full context."""
    path = file_spec["path"]
    purpose = file_spec.get("purpose", "")
    language = blueprint["language"]
    project_goal = blueprint.get("project_name", "").replace("_", " ")

    # Build context with prioritized truncation
    pending = {p: c for p, c in all_skeletons.items() if p not in written_files}
    contracts = blueprint.get("data_contracts", [])
    deps = blueprint.get("dependencies", {})

    context = _build_prioritized_context(
        goal_text=blueprint.get("_goal", ""),
        contracts=contracts,
        written_files=written_files,
        pending_skeletons=pending,
        deps=deps,
        ctx_tokens=ctx_tokens,
        current_file_path=path,
        api_endpoints=blueprint.get("api_endpoints", []),
        stub_instructions=blueprint.get("stub_instructions", []),
    )

    # Detect if this is a Python entry point that uses a web framework
    is_python_entry = (
        language == "python"
        and any(path.endswith(ep) for ep in ["main.py", "app.py", "server.py", "run.py"])
    )
    entry_point_rule = ""
    if is_python_entry:
        entry_point_rule = """
  6. CRITICAL: If this file creates a FastAPI/Flask/Starlette/web app, it MUST end with:
     if __name__ == "__main__":
         import uvicorn
         uvicorn.run(app, host="127.0.0.1", port=8000)
     This ensures the file can be run directly with 'python main.py'.
  7. CRITICAL: ALL features described in the project goal MUST be fully implemented.
     Do NOT leave placeholder functions or TODO comments. Every endpoint, every game
     mechanic, every UI element mentioned in the goal must work."""

    # Build a list of all function/class names from skeletons that other files
    # might depend on — the coder MUST implement every one of them.
    skeleton_for_this = all_skeletons.get(path, "")
    exports_hint = ""
    if skeleton_for_this:
        import re as _skel_re
        skel_funcs = _skel_re.findall(
            r'(?:function\s+|def\s+|class\s+|export\s+(?:default\s+)?(?:function\s+|class\s+)?)([A-Za-z_][A-Za-z0-9_]*)\s*[({]',
            skeleton_for_this,
        )
        if skel_funcs:
            exports_hint = (
                f"\n  6. CRITICAL: Your skeleton defines these names: {', '.join(skel_funcs)}\n"
                f"     You MUST implement ALL of them. Other files depend on these.\n"
                f"     Do NOT rename, omit, or reorganize them.\n"
                f"     Every function/class from the skeleton MUST appear in your output."
            )

    # Full-stack API integration rules (conditional)
    fullstack_rules = ""
    api_endpoints = blueprint.get("api_endpoints", [])
    if api_endpoints:
        # Detect if this is a frontend or backend file
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

        # Build endpoint reference
        ep_ref = "\n".join(
            f"    {ep.get('method', '?')} {ep.get('path', '?')}"
            for ep in api_endpoints
        )

        if is_frontend:
            fullstack_rules = f"""
  FULL-STACK FRONTEND RULES (CRITICAL — prevents blank-screen bugs):
  - API endpoint URLs MUST match the backend EXACTLY. The defined endpoints are:
{ep_ref}
  - EVERY API call MUST use try-catch with proper error handling:
    try {{
      const response = await fetch('/exact/backend/path', ...);
      if (!response.ok) {{ const err = await response.json(); throw new Error(err.detail || 'Request failed'); }}
      const data = await response.json();
      // use data
    }} catch (error) {{ console.error('Context:', error); showUserError(error.message); }}
  - Add data transformation functions when the backend format differs from the UI format
  - Include loading states (show spinner/message while waiting for API responses)
  - NEVER render UI elements that depend on API data before the data is received
  - Validate API responses before using: check that expected fields exist
  - Log API responses during development: console.log('API Response:', data);
  - LOGIC SEPARATION (CRITICAL): Do NOT implement any business/game/domain logic in the frontend.
    ALL computation, validation, rule enforcement, AI/bot logic, and data processing MUST happen
    in the backend. The frontend's ONLY job is:
    1. Send user actions to the backend via API calls
    2. Receive results from the backend
    3. Render the results in the UI
    Example: For a chess game, the frontend MUST NOT validate moves or generate bot moves.
    Instead, it sends the user's move to the backend and displays the backend's response.
  - NO STUBS: Every function must be FULLY implemented. No empty functions, no TODO comments,
    no placeholder returns. If a function exists, it must work completely."""
        elif is_backend:
            fullstack_rules = f"""
  FULL-STACK BACKEND RULES (CRITICAL — ensures frontend integration works):
  - Route paths MUST match EXACTLY. The defined endpoints are:
{ep_ref}
  - Add CORS middleware to allow the frontend origin:
    For FastAPI: app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    For Express: app.use(cors())
    For Go/Gin: r.Use(cors.Default())
  - Return proper error responses with a "detail" field:
    For FastAPI: raise HTTPException(status_code=400, detail="specific error message")
    For Express: res.status(400).json({{ detail: "specific error message" }})
  - Validate all input data before processing
  - Use the EXACT same field names in responses as defined in the API contracts
  - NEVER change response field names from what the data contracts specify
  - LOGIC OWNERSHIP (CRITICAL): ALL business logic, game logic, domain logic, AI/bot logic,
    rule enforcement, validation, and computation MUST be implemented HERE in the backend.
    The frontend will ONLY send user actions and display results.
    Every endpoint must do the actual work, not just pass through data.
  - NO STUBS: Every route handler and every helper function must be FULLY implemented.
    No empty functions, no TODO comments, no placeholder returns.
    Every endpoint must return real, computed data."""

    prompt = f"""Implement the file: {path}

Purpose: {purpose}

{context}

OUTPUT:
Complete implementation for {path}. Output ONLY the code, no fences, no explanation.

RULES:
  1. Use EXACT imports from skeletons
  2. Full implementation — NO placeholders, NO stubs, NO TODO comments, NO empty function bodies.
     Every function must contain real, working logic. "pass" or "..." as a function body is FORBIDDEN.
     If you write a function, it MUST do real work. No "# implement later" comments.
  3. Follow language idioms
  4. Handle errors properly
  5. Add brief comments for complex logic
  6. JAVASCRIPT SPECIFIC:
     - If this file interacts with the DOM, include an init() function
     - At the end of the file, add the DOMContentLoaded pattern:
       if (document.readyState === 'loading') {{
         document.addEventListener('DOMContentLoaded', init);
       }} else {{
         init();
       }}
     - If making API calls, ALWAYS define: const API_BASE_URL = 'http://localhost:8000';
     - NEVER use relative URLs like fetch('/api/...') — always use fetch(`${{API_BASE_URL}}/api/...`)
  7. PYTHON BACKEND SPECIFIC:
     - If using FastAPI, ALWAYS include CORS middleware:
       from fastapi.middleware.cors import CORSMiddleware
       app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
     - ALWAYS end with: if __name__ == "__main__": import uvicorn; uvicorn.run(app, host="127.0.0.1", port=8000)
  8. EVERY function from the skeleton MUST be implemented with full logic.
     Ask yourself: "If I run this code right now, will every function produce correct results?"
     If not, keep implementing until it does.{exports_hint}{entry_point_rule}{fullstack_rules}"""

    system = f"Expert {language} developer. Output ONLY code, no markdown fences."

    response = llm_call(
        model=coder_model,
        prompt=prompt,
        system=system,
        max_tokens=14336,
        temperature=0.1,
    )

    return strip_fences(response)
