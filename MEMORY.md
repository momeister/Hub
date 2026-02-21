# AI Hub Memory


## Facts


## Sessions

### Session 6 — Undefined Function References Fix
**Files modified:** `agents.py`, `skeletons.py`

#### Root cause:
`updateTurnIndicator is not defined` — The coder generates files one-by-one using `fill_in_file()`. When generating `app.js`, it sees the skeleton of `ui.js` which declares `updateTurnIndicator()`. So `app.js` calls that function. But when `ui.js` is later filled in, the coder may rename, omit, or reorganize the function. Result: `app.js` calls a function that no longer exists anywhere.

Session 5's fixes catch **missing files** (HTML `<script src>` pointing to non-existent files). But this is a **missing function** problem — the file exists, it just doesn't contain the function that other files expect.

#### Fixes applied:
1. **Code coherence validation (agents.py)** — New `_validate_code_coherence()` function added after the cross-file reference check in `agent_coder`. After ALL files are generated, it concatenates all source files and asks the coder LLM to identify functions/variables/classes that are CALLED but never DEFINED in any project file. Filters out browser globals, DOM methods, standard library, and third-party imports. For each undefined reference found, it patches the target file by generating the missing function definitions with full context from the calling files. Safety check ensures patched files aren't drastically smaller than originals. Works for JavaScript, TypeScript, and Python projects.
2. **Skeleton function enforcement (skeletons.py)** — `fill_in_file()` now parses the skeleton for the current file to extract all function/class names defined in it. These names are injected into the prompt as an explicit CRITICAL rule: "Your skeleton defines these names: X, Y, Z. You MUST implement ALL of them. Other files depend on these. Do NOT rename, omit, or reorganize them." This prevents the coder from diverging from the skeleton's API contract during fill-in.

### Session 5 — Builder Quality & Telegram Quiet Mode
**Files modified:** `agents.py`, `sandbox.py`, `compile_checks.py`, `core/telegram/builder.py`, `core/telegram/state.py`, `core/telegram/handlers/callbacks.py`

#### Root causes identified:
1. **game.js 404**: HTML files reference `<script src="game.js">` but the file was never generated. The coder only validates that PLANNED files exist — never checks cross-references IN code.
2. **No JS compile checking**: `compile_check()` maps JavaScript to `compile_check_typescript` which requires `tsc`. When tsc is absent (Docker), returns `True, []` — zero checking.
3. **Browser validation skipped for server projects**: `_validate_browser_project()` only runs for pure HTML projects (no server). A JS project with `server.js + index.html` only tests if the server starts — never validates HTML refs.
4. **Telegram floods**: Every single event (file_start, file_done, repair, verify, polish, etc.) sends a Telegram message.

#### Fixes applied:
1. **Cross-file reference validation (agents.py)** — After coder phase generates all files AND validates planned files, a new pass scans ALL HTML files for `<script src>` and `<link href>` tags pointing to local files. If a referenced file doesn't exist on disk, it's auto-generated using `fill_in_file()` with context from the HTML and other project files. This prevents the game.js 404 scenario.
2. **JS compile check fallback (compile_checks.py)** — New `compile_check_javascript()` function: if `tsc` is available, delegates to TypeScript checker. Otherwise, uses `node --check *.js` which validates syntax without executing. Skips `node_modules`. Caps at 50 files. The `compile_check()` dispatcher now maps JavaScript to this new function.
3. **Browser validation for ALL HTML projects (sandbox.py)** — `_is_browser_project()` now checks root + `public/`, `static/`, `www/`, `dist/` subdirs. `_validate_browser_project()` recursively finds all HTML files and validates their script/CSS references. `sandbox_test()` runs browser validation FIRST for any project with HTML files, even server-based ones. If HTML refs are broken but server starts, it still returns failure.
4. **Telegram quiet mode (builder.py, state.py, callbacks.py)** — New `build_verbose` flag (default: `False`) and `build_events` list in state.py. In quiet mode, only essential messages are sent: build start, tech stack, file plan, approval requests, critical/fatal errors (max 5), timeouts, and completion. All other events (phase, file_done, repair, verify, polish) are stored in `build_events` and only sent if verbose is on. Build start message now includes toggle buttons. Three new callback handlers: `build_verbose_on`, `build_verbose_off`, `build_status_query` (shows progress + last 5 events with refresh/toggle buttons).

### Session 4 — Multi-Language Nesting & Context Window Fixes
**Files modified:** `pipeline.py`, `agents.py`, `context.py`, `cli.py`, `artifacts.py`, `core/telegram/builder.py`

#### Fixes applied:
1. **Backend building its own frontend (duplicate nesting)** — When `sub_files` was empty (no files matched the subproject prefix), `architect_phase()` was called for the subproject but without context about other subprojects. The architect would design a full-stack app including frontend files, creating `output/backend/frontend/` and `output/backend/backend/` nesting. Fix: The sub-architect prompt now explicitly names all OTHER subprojects that are built separately and instructs NOT to include them. Added `_sanitize_sub_blueprint()` that strips own-prefix paths and removes other-subproject paths as a safety net.
2. **Structural compile errors (Duplicate module)** — When `backend/main.py` and `backend/backend/main.py` both existed, mypy reported "Duplicate module named main". The repair loop tried 3 times to fix this by editing code, but the issue is structural. Added detection for "Duplicate module" errors that finds and removes the duplicate file before attempting code repairs.
3. **Nuclear regen appearing to restart everything** — After "Fresh regeneration for backend/main.py", the coder loop continued to `frontend/index.html` etc. which LOOKED like a restart but was just the loop continuing to the next files. Root cause was Bug #1 (frontend files in backend blueprint). Fixed by #1.
4. **File counter not resetting between subprojects** — `tg_state.build_state["files_done"]` was never reset when a new `plan` event arrived, causing "10/11", "11/11", "11/11"... in Telegram. Now resets to 0 on each `plan` event.
5. **Context window increased to 256k** — `DEFAULT_CTX_TOKENS` changed from 131072 to 262144. Updated cli.py mode configs, banner text, and artifacts.py README template.
6. **Post-coder validation** — Added check after the coder loop to detect planned files that were never generated (e.g., `move_history.js` planned but skipped). Missing files are now generated at the end.

### Session 3 — Dependency & Testing Fixes
**Files modified:** `pipeline.py`, `agents.py`, `manifest.py`, `blueprint.py`

#### Fixes applied:
1. **Missing requirements.txt in subprojects** — `pipeline.py` had `"dependencies": {}` hardcoded for sub-blueprints. Added `_extract_sub_dependencies()` that extracts deps from master blueprint per subproject language, or returns a typed stub so the Retriever generates one.
2. **Retriever now auto-generates manifests** — When `dependencies.type` is set but `content` is empty (subprojects), the Retriever calls `_generate_manifest_from_blueprint()` which uses the LLM to produce a `requirements.txt` / `package.json` from the file list.
3. **Executor installs deps for ALL languages** — Previously only installed Python deps. Now installs pip/npm/cargo/go deps and triggers `validate_manifest()` repair on failure.
4. **npm 404 / hallucinated package detection** — `manifest.py` now has `_extract_bad_packages()` that parses pip/npm/cargo 404 errors and `_strip_bad_packages_from_manifest()` that auto-removes them before LLM repair. Repair loop runs up to 3 attempts (was 1).
5. **Critic dep-aware repair** — Before code repair, Critic checks if sandbox failure is a dependency issue (ModuleNotFoundError etc.) and tries manifest repair first.
6. **Blueprint prompt** — Rule 11 added: "ONLY use packages that ACTUALLY EXIST on PyPI/npm/crates.io"

## Projects
