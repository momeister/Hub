# AI Hub Memory


## Facts


## Sessions

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
