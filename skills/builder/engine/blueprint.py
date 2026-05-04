"""
skills/builder/engine/blueprint.py - Blueprint parsing and architect
====================================================================
"""

from __future__ import annotations

import json

from skills.builder.engine.context import blog, llm_call, clean_json, validate_blueprint
from skills.builder.engine.fallback import detect_fallback_language, make_fallback_blueprint
from skills.builder.engine.utils import generate_project_name


def _validate_and_normalize_blueprint(raw: object, goal: str) -> dict:
    if isinstance(raw, list):
        blog.warning("Blueprint was wrapped in a JSON array - unwrapping")
        raw = next((item for item in raw if isinstance(item, dict)), None)
        if raw is None:
            raise ValueError("Blueprint is an array with no dict elements")

    if not isinstance(raw, dict):
        raise ValueError(f"Blueprint is {type(raw).__name__}, expected dict")

    blueprint = raw

    if not isinstance(blueprint.get("language"), str) or not blueprint["language"].strip():
        raise ValueError("Blueprint missing or empty 'language' field")

    if not isinstance(blueprint.get("files"), list) or not blueprint["files"]:
        raise ValueError("Blueprint missing or empty 'files' list")

    clean_files = []
    for f in blueprint["files"]:
        if isinstance(f, dict) and isinstance(f.get("path"), str) and f["path"].strip():
            f.setdefault("purpose", "")
            f.setdefault("exports", [])
            f.setdefault("imports", [])
            f.setdefault("estimated_lines", 20)
            f.setdefault("critical", False)
            clean_files.append(f)
    if not clean_files:
        raise ValueError("Blueprint 'files' list has no valid entries (need dict with 'path')")
    blueprint["files"] = clean_files

    blueprint.setdefault("project_name", generate_project_name(goal))
    blueprint.setdefault("framework", "")
    blueprint.setdefault("why", "")
    blueprint.setdefault("is_multi_language", False)
    blueprint.setdefault("dependency_order", [f["path"] for f in blueprint["files"]])
    blueprint.setdefault("dependencies", {"type": "none", "content": ""})
    blueprint.setdefault("architecture_decisions", [])
    blueprint.setdefault("safe_stack_violations", [])
    blueprint.setdefault("estimated_complexity", "medium")
    blueprint.setdefault("subprojects", [])
    blueprint.setdefault("api_endpoints", [])

    if not isinstance(blueprint["dependencies"], dict):
        blueprint["dependencies"] = {"type": "none", "content": ""}

    return blueprint


def _parse_and_validate_blueprint(response: str, goal: str, architect_model: str) -> dict:
    try:
        raw = json.loads(clean_json(response))
        return _validate_and_normalize_blueprint(raw, goal)
    except Exception as exc:
        blog.error(f"Blueprint parse/validation failed: {exc}")
        blog.warning(f"LLM response preview: {response[:300]}")

    blog.warning("Retrying architect with simplified prompt...")
    retry_prompt = f"""Create a JSON object for this project: "{goal}"

The JSON MUST have these fields:
- "project_name": snake_case name (string)
- "language": programming language (string)
- "files": array of objects, each with "path" (string) and "purpose" (string)
- "dependency_order": array of file path strings

Output ONLY the JSON object. No explanation."""

    try:
        retry_response = llm_call(
            model=architect_model,
            prompt=retry_prompt,
            system="Output valid JSON only. No markdown, no explanation.",
            max_tokens=4096,
            temperature=0.05,
        )
        raw = json.loads(clean_json(retry_response))
        blueprint = _validate_and_normalize_blueprint(raw, goal)
        blog.info("Retry architect succeeded")
        return blueprint
    except Exception as exc2:
        blog.error(f"Retry also failed: {exc2}")

    fallback_lang = detect_fallback_language(goal)
    blog.warning(f"Using hardcoded fallback: {fallback_lang}")
    return make_fallback_blueprint(goal, fallback_lang)


def architect_phase(goal: str, architect_model: str) -> dict:
    blog.phase("architect", "Analyzing requirements and designing architecture", model=architect_model)

    prompt = f"""You are a senior software architect. Analyze this project requirement and produce a COMPLETE architectural blueprint as JSON.

PROJECT REQUIREMENT: "{goal}"

Your blueprint must include:
1. Tech stack decision (language, framework, reasoning)
2. Complete file structure with dependency analysis
3. Dependency versions
4. Architecture decisions with reasoning

FEATURE COMPLETENESS (CRITICAL):
  - Read the project requirement CAREFULLY word by word
  - Extract EVERY feature mentioned (explicit or implied)
  - EACH feature MUST have corresponding files and logic in the blueprint
  - Example: "chess game with move history and bot" requires:
    * Chess board rendering with interactive piece movement (click/drag)
    * Full chess rules enforcement (legal moves, check, checkmate, castling, en passant)
    * Move history display (list of moves in standard notation)
    * AI bot opponent (at minimum: random legal moves; ideally: basic evaluation)
    * Game state management (whose turn, game over detection)
  - If the user asks for a UI, it MUST be interactive (clickable, draggable, responsive)
  - If the user asks for a "game", pieces/elements MUST be movable/interactive
  - NEVER create a static/display-only UI when interaction is requested or implied

LANGUAGE SELECTION GUIDE (choose objectively, NO bias):
  - **Games (snake, tetris, pong, chess, etc.)**:
    - Web-based (browser): HTML + JavaScript + Canvas (simple, runs everywhere)
    - Desktop GUI: rust (macroquad), c++ (SDL), c# (MonoGame)
    - CLI terminal games: python (curses), c++, go
    - For simple visual games, ALWAYS prefer HTML+JS (widest reach, easiest to test)
  - Web frontend: typescript (react/vue/svelte) or plain javascript
  - Web backend (performance): go (gin/fiber), rust (axum/actix)
  - Web backend (rapid dev): python (fastapi/flask), javascript (express), go (gin)
  - CLI tools: python, go, rust
  - Mobile: kotlin (android), swift (ios), dart (flutter)
  - Desktop GUI: c# (WPF), python (tkinter/PyQt), electron, rust (tauri)
  - Data science: python (pandas/numpy/matplotlib)
  - Systems programming: c, c++, rust
  - Automation/scripting: python, go

COMPLEXITY DECISION (evaluate this BEFORE choosing is_multi_language):
  - A "bot" or "AI opponent" in a game does NOT require a backend. It can run in-browser (JS Minimax, chess.js, etc.)
  - A "database" or "persistence" only requires a backend if data must survive page refresh AND be shared across users.
  - Set is_multi_language=true ONLY IF the project genuinely requires:
    (a) server-side computation that cannot run in a browser, OR
    (b) persistent storage shared across multiple users/sessions, OR
    (c) the user EXPLICITLY asks for a separate backend API.
  - If in doubt: build Single-Language first. A self-contained HTML+JS project that works is better than a broken Full-Stack project.

ANTI-PATTERNS - DO NOT:
  - Pick obscure or experimental frameworks (no Dioxus, Yew, Leptos for simple projects)
  - Pick a language that doesn't match the domain (no Rust for a simple script, no C++ for a web API)
  - Use niche build tools or package managers
  - Over-engineer: a simple project needs a simple stack (e.g. HTML+JS, not React+TypeScript+Tailwind+Redux)
  - Mix multiple paradigms or frameworks unnecessarily
  - Create placeholder/stub files — every file must have a clear, implementable purpose
  - Omit features that the user explicitly requested

WHEN IN DOUBT: choose the MOST COMMON, MOST POPULAR option for the domain.
  Examples: Python for scripts/CLI, HTML+JS for browser apps, Go or Python for web APIs,
  React+TypeScript for complex web frontends, HTML+JS+Canvas for simple browser games.

FULL-STACK PROJECT RULES (when project requires BOTH frontend AND backend):
  - Set is_multi_language=true with separate subprojects for frontend and backend
  - Define ALL API endpoints explicitly in the "api_endpoints" field (see schema below)
  - Every endpoint MUST specify: exact URL path, HTTP method, request body, response body
  - Use CONSISTENT parameter names everywhere (e.g. game_id in URL, request, AND response — not game_id in backend and gameId in frontend)
  - Backend MUST include CORS middleware/headers to allow the frontend origin
  - Specify exact ports (e.g. backend: 8000, frontend: 3000)
  - If backend returns a specialized format (FEN, UCI, etc.), plan a data transformation utility in the frontend
  - Frontend MUST have: error handling for API calls, loading states, user-friendly error messages
  - Backend MUST have: input validation, proper error responses with "detail" field, CORS headers
  - Include a proxy/CORS configuration note in architecture_decisions
  - CRITICAL: The #1 cause of blank-screen bugs in generated full-stack apps is frontend calling wrong endpoints
    or expecting wrong data formats. Be EXPLICIT about every endpoint URL and every response shape.
  - LOGIC SEPARATION (CRITICAL):
    * ALL business logic, game logic, AI/bot logic, rule enforcement, validation, and computation
      MUST be planned as backend files ONLY
    * The frontend must NOT contain any files that duplicate backend computation
    * Frontend files should ONLY handle: API calls, UI rendering, event handling, and data format conversion
    * Example: For a chess game, move validation and bot logic are BACKEND files.
      The frontend only sends moves to the backend API and displays the result.
    * In the file plan, clearly mark each file's responsibility (API layer, UI, or backend logic)

RULES:
  1. If user mentions language explicitly, use it
  2. If user mentions "browser" or "web", use HTML+JavaScript (or TypeScript for complex apps)
  3. For simple games, ALWAYS use HTML+JavaScript+Canvas
  4. Choose what fits the DOMAIN best - prefer mainstream over exotic
  5. If both frontend + backend needed, set is_multi_language=true and fill subprojects
  6. Use ONLY well-known, maintained, popular libraries - no obscure crates/packages
  7. List ALL files in dependency order (base files first)
  8. Keep the stack MINIMAL - fewer dependencies = fewer problems
  9. Each file's "purpose" must be specific and actionable, NOT vague
  10. For Python web backends, the entry point MUST include uvicorn/flask startup code
  11. ONLY use packages that ACTUALLY EXIST on PyPI/npm/crates.io - do NOT hallucinate package names
  12. For full-stack projects with is_multi_language=true:
      - File paths in each subproject must be RELATIVE to the subproject root
      - Use "main.py" NOT "backend/main.py" in the files list
      - The subproject directory is created automatically — nesting it in the path creates duplicates
      - Example: files for the backend subproject should be ["main.py", "routes.py"] not ["backend/main.py", "backend/routes.py"]

OUTPUT: A single JSON object with this exact schema:
""" + """{
  "project_name": "<snake_case project name>",
  "language": "<primary language>",
  "framework": "<framework if any, empty string if none>",
  "why": "<2-3 sentence explanation of tech choice>",
  "is_multi_language": <boolean>,
  "files": [
    {
      "path": "<relative file path>",
      "purpose": "<what this file does>",
      "exports": ["<exported identifiers>"],
      "imports": ["<identifiers imported from OTHER project files>"],
      "estimated_lines": <number>,
      "critical": <boolean - true for build manifests and entry points>
    }
  ],
  "dependency_order": ["<file paths in build order>"],
  "dependencies": {
    "type": "<cargo_toml|requirements_txt|package_json|go_mod|pom_xml|none>",
    "content": "<actual dependency file content>"
  },
  "architecture_decisions": [
    {
      "decision": "<what was decided>",
      "reasoning": "<why>"
    }
  ],
  "safe_stack_violations": [],
  "estimated_complexity": "<simple|medium|complex>",
  "subprojects": [
    {
      "name": "<backend|frontend|etc>",
      "language": "<language>",
      "framework": "<framework>"
    }
  ],
  "api_endpoints": [
    {
      "method": "GET|POST|PUT|DELETE",
      "path": "/exact/url/path",
      "request_body": {"field": "type"},
      "response_body": {"field": "type"},
      "description": "what this endpoint does"
    }
  ]
}

IMPORTANT: Output ONLY the JSON object. No explanation, no fences."""

    system = "You are a senior tech lead and software architect. Choose mainstream, well-proven technologies. Output only valid JSON."

    response = llm_call(
        model=architect_model,
        prompt=prompt,
        system=system,
        max_tokens=8192,
        temperature=0.1,
    )

    blueprint = _parse_and_validate_blueprint(response, goal, architect_model)

    stack_warnings = validate_blueprint(blueprint)
    if stack_warnings:
        for w in stack_warnings:
            blog.warning(f"Safe Stack: {w}")
        blueprint.setdefault("safe_stack_violations", []).extend(stack_warnings)

    blog.tech(
        language=blueprint.get("language", "?"),
        framework=blueprint.get("framework", ""),
        why=blueprint.get("why", ""),
        is_multi=blueprint.get("is_multi_language", False),
    )
    blog.plan(
        files_total=len(blueprint.get("files", [])),
        file_paths=[f["path"] for f in blueprint.get("files", [])],
        complexity=blueprint.get("estimated_complexity", ""),
    )

    for dec in blueprint.get("architecture_decisions", []):
        blog.info(f"Architecture: {dec.get('decision', '')} -- {dec.get('reasoning', '')}")

    return blueprint


def data_contract_phase(blueprint: dict, architect_model: str) -> dict:
    """
    Generiert explizite Datenstruktur-Definitionen für alle zentralen
    Objekte des Projekts. Diese werden in blueprint["data_contracts"]
    gespeichert und an JEDEN nachfolgenden LLM-Call übergeben.

    Das verhindert inkonsistente Datenstrukturen zwischen Dateien —
    die häufigste Ursache für nicht-lauffähige generierte Projekte.
    """
    # Idempotent: skip if already generated
    if blueprint.get("data_contracts"):
        return blueprint

    language = blueprint.get("language", "unknown")
    goal = blueprint.get("_goal", "") or blueprint.get("project_name", "")
    files_summary = "\n".join(
        f"  - {f['path']}: {f.get('purpose', '')}"
        for f in blueprint.get("files", [])[:20]
    )
    framework = blueprint.get("framework", "")

    prompt = f"""A {language} project is being built for this goal:

GOAL: "{goal}"
FRAMEWORK: {framework or "none"}

PLANNED FILES:
{files_summary}

Your task: Define ALL shared data structures that will be passed between files.

For each shared object/type/interface, provide:
1. Its exact name (as used in code)
2. Its exact structure (field names, types, example values)
3. Which files produce it and which files consume it

RULES:
- Only define structures that are actually shared between 2+ files
- Be CONCRETE: use real field names, real types, real example values
- For games: define board representation, game state, move format
- For APIs: define request/response shapes, database models
- For CLI tools: define config objects, data pipeline structures
- If no shared structures exist (e.g. single-file script), return empty contracts

FULL-STACK API CONTRACTS (CRITICAL — for projects with both frontend and backend):
- Define an API ENDPOINT CONTRACT for EVERY endpoint:
  - Exact URL path and HTTP method (e.g. "POST /games", "GET /games/{{game_id}}")
  - Request body structure with field names and types
  - Response body structure with field names and types
  - Error response structure (e.g. {{"detail": "error message"}})
  - Data transformations needed (e.g. backend sends FEN string, frontend needs 2D array)
- CRITICAL: Frontend API calls MUST use the EXACT same URLs and field names as the backend routes
  - If backend defines route "/games/{{game_id}}/move", frontend MUST call "/games/${{gameId}}/move"
  - If backend returns {{"board_fen": "..."}}, frontend MUST read response.board_fen (NOT response.board)
- Include transformation contracts when data formats differ between frontend and backend
  - Example: fenToBoard(fen: string) -> string[][] for converting FEN to renderable board
  - Example: coordsToUCI(from: [row,col], to: [row,col]) -> string for move notation

Output ONLY this JSON:
{{
  "contracts": [
    {{
      "name": "<TypeName or variable name>",
      "description": "<one sentence what this represents>",
      "structure": "<exact definition — field:type pairs or interface>",
      "example": "<concrete example value in the target language>",
      "produced_by": ["<file.py>"],
      "consumed_by": ["<file.py>", "<file2.py>"]
    }}
  ]
}}

Examples of good contracts:

For a chess game (JavaScript):
{{
  "name": "BoardState",
  "description": "2D array representing the chess board",
  "structure": "board[row][col]: string|null, where row 0 = rank 8 (black side), row 7 = rank 1 (white side)",
  "example": "board[0][4] = 'bK', board[7][4] = 'wK', board[3][3] = null",
  "produced_by": ["game.js"],
  "consumed_by": ["renderer.js", "ai.js", "moves.js"]
}}

For a FastAPI backend (Python):
{{
  "name": "TaskModel",
  "description": "Pydantic model for a task item",
  "structure": "id: int, title: str, done: bool, created_at: datetime",
  "example": "TaskModel(id=1, title='Buy milk', done=False, created_at=datetime(2024,1,1))",
  "produced_by": ["models.py"],
  "consumed_by": ["routes.py", "database.py"]
}}

For a full-stack API endpoint contract:
{{
  "name": "API:POST /games",
  "description": "Create a new game - endpoint contract between frontend and backend",
  "structure": "Request: {{}} (empty body) | Response: {{game_id: string, board_fen: string, turn: string, status: string}}",
  "example": "fetch('/games', {{method: 'POST'}}).then(r => r.json()) => {{game_id: 'abc123', board_fen: 'rnbqkbnr/...', turn: 'white', status: 'active'}}",
  "produced_by": ["backend/routes.py"],
  "consumed_by": ["frontend/api.js", "frontend/app.js"]
}}

For a data transformation utility:
{{
  "name": "fenToBoard",
  "description": "Converts FEN string from backend to 2D array for frontend rendering",
  "structure": "input: string (FEN notation) -> output: (string|null)[][] (8x8 board array)",
  "example": "fenToBoard('rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR') => [['r','n','b',...], ...]",
  "produced_by": ["frontend/utils.js"],
  "consumed_by": ["frontend/board.js", "frontend/app.js"]
}}

Output ONLY the JSON object. No explanation."""

    blog.phase("data_contracts", "Generating shared data contracts", model=architect_model)

    try:
        response = llm_call(
            model=architect_model,
            prompt=prompt,
            system="Expert software architect. Define concrete shared data structures. Output ONLY JSON.",
            max_tokens=4096,
            temperature=0.1,
        )
        raw = json.loads(clean_json(response))
        contracts = raw.get("contracts", []) if isinstance(raw, dict) else []

        if contracts:
            blueprint["data_contracts"] = contracts
            blog.info(f"Generated {len(contracts)} data contract(s):")
            for c in contracts:
                blog.info(f"  {c.get('name', '?')}: {c.get('description', '?')}")
        else:
            blog.info("No shared data contracts needed for this project")
            blueprint["data_contracts"] = []

    except Exception as exc:
        blog.warning(f"Data contract generation failed ({exc}), continuing without contracts")
        blueprint["data_contracts"] = []

    return blueprint
