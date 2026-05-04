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
    _code_hash,
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
from skills.builder.engine.utils import sanitize_skeleton_paths


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
        except Exception as _e:
            blog.warning(f"[non-critical] Could not read {fpath}: {_e}")
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
# Helper: ensure JavaScript files have DOMContentLoaded init pattern
# ---------------------------------------------------------------------------
def _ensure_js_init_pattern(output_dir: str, written_files: dict) -> None:
    """Ensure main JavaScript files have DOMContentLoaded + init() pattern.

    Scans JS files for DOM manipulation. If a file has DOM interactions but
    no DOMContentLoaded/readyState check, and it defines an init() function,
    appends the standard init pattern.
    """
    import re as _re

    for fpath, content in list(written_files.items()):
        if not fpath.endswith(('.js', '.mjs')):
            continue

        full_path = os.path.join(output_dir, fpath)
        if not os.path.exists(full_path):
            continue

        # Skip utility/module files that don't touch the DOM
        if any(kw in fpath.lower() for kw in ['utils', 'helpers', 'api', 'config', 'constants', 'lib/']):
            continue

        # Check if this file has DOM manipulation (indicates it's a main/app file)
        has_dom = any(kw in content for kw in [
            'document.getElementById', 'document.querySelector',
            'addEventListener', 'innerHTML', 'textContent',
            'appendChild', 'createElement',
        ])
        if not has_dom:
            continue

        # Already has init pattern — skip
        has_init_pattern = (
            'DOMContentLoaded' in content
            or 'document.readyState' in content
            or 'window.onload' in content
            or 'window.addEventListener("load"' in content
            or "window.addEventListener('load'" in content
        )
        if has_init_pattern:
            continue

        # Check if there's an init() function defined
        has_init_fn = bool(_re.search(r'function\s+init\s*\(', content))
        if not has_init_fn:
            continue

        # Append DOMContentLoaded init pattern
        block = (
            "\n\n// Initialize when DOM is ready\n"
            "if (document.readyState === 'loading') {\n"
            "    document.addEventListener('DOMContentLoaded', init);\n"
            "} else {\n"
            "    init();\n"
            "}\n"
        )
        content += block
        try:
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)
            written_files[fpath] = content
            blog.info(f"Added DOMContentLoaded init pattern to {fpath}")
        except OSError as exc:
            blog.warning(f"Could not write init pattern to {fpath}: {exc}")


# ---------------------------------------------------------------------------
# Helper: ensure frontend JS files use API_BASE_URL for API calls
# ---------------------------------------------------------------------------
def _ensure_frontend_api_base_url(output_dir: str, written_files: dict, blueprint: dict) -> None:
    """Ensure frontend JavaScript files define and use API_BASE_URL.

    Scans frontend JS files for fetch() calls with relative URLs (e.g. fetch('/api/...'))
    and injects a const API_BASE_URL if missing.
    """
    import re as _re

    api_endpoints = blueprint.get("api_endpoints", [])
    if not api_endpoints:
        return

    for fpath, content in list(written_files.items()):
        if not fpath.endswith(('.js', '.mjs', '.ts', '.jsx', '.tsx')):
            continue

        full_path = os.path.join(output_dir, fpath)
        if not os.path.exists(full_path):
            continue

        # Only check frontend-ish files
        is_frontend = any(kw in fpath.lower() for kw in [
            'frontend/', 'client/', 'public/',
            'api.js', 'api.ts', 'app.js', 'app.ts',
        ]) or fpath.endswith(('.html', '.jsx', '.tsx', '.vue', '.svelte'))

        # Also consider files NOT in backend/ as potential frontend
        is_backend = any(kw in fpath.lower() for kw in [
            'backend/', 'server/', 'routes', 'middleware',
        ])

        if not is_frontend and is_backend:
            continue

        # Check for fetch calls with relative URLs but no base URL constant
        has_relative_fetch = bool(_re.search(r"""fetch\s*\(\s*['"`]/""", content))
        has_base_url = any(kw in content for kw in [
            'API_BASE_URL', 'API_URL', 'BASE_URL', 'baseURL', 'baseUrl',
            'http://localhost:', 'http://127.0.0.1:',
        ])

        if has_relative_fetch and not has_base_url:
            # Inject API_BASE_URL constant at the top of the file
            base_url_line = "const API_BASE_URL = 'http://localhost:8000';\n\n"
            content = base_url_line + content

            # Replace relative fetch URLs with API_BASE_URL prefix
            content = _re.sub(
                r"""fetch\s*\(\s*(['"`])/""",
                r"fetch(\1${API_BASE_URL}/",
                content,
            )
            # Fix: convert simple quotes to template literals for interpolation
            content = _re.sub(
                r"""fetch\((['"])\$\{API_BASE_URL\}(/[^'"]+)\1""",
                r"fetch(`${API_BASE_URL}\2`",
                content,
            )

            try:
                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(content)
                written_files[fpath] = content
                blog.info(f"Added API_BASE_URL to {fpath}")
            except OSError as exc:
                blog.warning(f"Could not write API_BASE_URL to {fpath}: {exc}")


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
        # --- Systems 1-4 extensions ---
        "openapi_spec": {},           # OpenAPI 3.0 Spec (System 1)
        "contract_violations": [],    # Contract violations found (System 2)
        "symbol_table": {},           # AST symbol table (System 2)
        "kg_context": {},             # KG query result (System 3)
        "kb_context": "",             # KB context for Planner (System 4)
        "all_errors": [],             # All errors (for record_build_result)
        "all_repairs": [],            # All repairs (for record_build_result)
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

    # System 3+4: Enrich goal with KG/KB context if available
    enriched_goal = ws["goal"]
    kg_summary = ""
    if ws.get("kg_context") and ws["kg_context"].get("summary_for_prompt"):
        kg_summary = ws["kg_context"]["summary_for_prompt"]
    kb_ctx = ws.get("kb_context", "")

    if kg_summary or kb_ctx:
        context_parts = [enriched_goal]
        if kg_summary:
            context_parts.append(f"\n\n{kg_summary}")
        if kb_ctx:
            context_parts.append(f"\n\n{kb_ctx}")
        enriched_goal = "\n".join(context_parts)
        blog.info("Planner context enriched with KG/KB data")

    blueprint = architect_phase(enriched_goal, ws["manager_model"])
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
        # Known safe additions derived from file purposes AND project goal
        files_text = " ".join(f.get("purpose", "") for f in blueprint.get("files", []))
        scan_text = (files_text + " " + goal).lower()
        additions = []
        KNOWN_SAFE = {
            # Generic / common
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
            # Domain-specific libraries
            "chess":      "python-chess>=1.999",
            "pygame":     "pygame>=2.5",
            "pillow":     "Pillow>=10.0",
            "pil":        "Pillow>=10.0",
            "image":      "Pillow>=10.0",
            "opencv":     "opencv-python>=4.8",
            "cv2":        "opencv-python>=4.8",
            "scikit-learn": "scikit-learn>=1.3",
            "sklearn":    "scikit-learn>=1.3",
            "nltk":       "nltk>=3.8",
            "spacy":      "spacy>=3.6",
            "boto3":      "boto3>=1.28",
            "aws":        "boto3>=1.28",
            "selenium":   "selenium>=4.11",
            "beautifulsoup": "beautifulsoup4>=4.12",
            "scrapy":     "scrapy>=2.10",
            "tensorflow": "tensorflow>=2.13",
            "torch":      "torch>=2.0",
            "pytorch":    "torch>=2.0",
            "transformers": "transformers>=4.30",
            "websocket":  "websockets>=11.0",
            "cryptography": "cryptography>=41.0",
            "paramiko":   "paramiko>=3.3",
            "yaml":       "pyyaml>=6.0",
            "toml":       "tomli>=2.0",
            "click":      "click>=8.1",
            "typer":      "typer>=0.9",
            "rich":       "rich>=13.0",
            "colorama":   "colorama>=0.4",
            "jinja":      "Jinja2>=3.1",
            "sympy":      "sympy>=1.12",
            "scipy":      "scipy>=1.11",
            "networkx":   "networkx>=3.1",
            "plotly":     "plotly>=5.15",
            "seaborn":    "seaborn>=0.12",
            "streamlit":  "streamlit>=1.25",
            "gradio":     "gradio>=3.40",
            "discord":    "discord.py>=2.3",
            "telegram":   "python-telegram-bot>=20.4",
            "tweepy":     "tweepy>=4.14",
            "stripe":     "stripe>=5.5",
            "openai":     "openai>=1.0",
            "langchain":  "langchain>=0.1",
            "chromadb":   "chromadb>=0.4",
            "pymongo":    "pymongo>=4.5",
            "psycopg":    "psycopg2-binary>=2.9",
            "mysql":      "mysql-connector-python>=8.1",
        }
        for keyword, pkg_line in KNOWN_SAFE.items():
            if keyword in scan_text and pkg_line not in (seed or ""):
                if not pkg_line.startswith("#"):
                    # Avoid duplicate package names
                    pkg_name = pkg_line.split(">=")[0].split(">=")[0].strip()
                    if not any(pkg_name in a for a in additions):
                        additions.append(pkg_line)

        base_content = (seed or "") + "\n".join(additions)

        # LLM fallback: trigger if no content at all, or if very few packages
        # were found (< 2) — the goal may reference libraries not in KNOWN_SAFE
        non_comment_pkgs = [a for a in additions if not a.startswith("#")]
        if not base_content.strip() or len(non_comment_pkgs) < 2:
            prompt = (
                f"Generate a minimal requirements.txt for a Python {framework or 'script'} project.\n"
                f"Goal: {goal}\n"
                f"Already included: {', '.join(additions) if additions else 'none'}\n\n"
                "STRICT RULES:\n"
                "- ONLY include packages you are 100% certain exist on PyPI\n"
                "- Maximum 8 packages\n"
                "- Prefer stdlib over external packages\n"
                "- Include ALL domain-specific libraries the goal requires\n"
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
                llm_content = strip_fences(response).strip()
                # Merge LLM output with already-found packages
                if base_content.strip() and llm_content:
                    base_content = base_content.strip() + "\n" + llm_content
                elif llm_content:
                    base_content = llm_content
            except Exception as exc:
                blog.warning(f"Manifest LLM fallback failed: {exc}")

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

    # Fix: Strip subproject name prefix from skeleton paths to prevent
    # nested folders (e.g. output/backend/backend/main.py)
    skeletons = sanitize_skeleton_paths(skeletons, blueprint)

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

    # System 1: Load pre-written contract stubs into workspace and disk.
    # These were generated deterministically from the OpenAPI spec and must
    # NOT be regenerated by the Coder — only their business logic gets filled in.
    _pre_written = blueprint.get("pre_written_files", {})
    if _pre_written:
        blog.info(f"Loading {len(_pre_written)} pre-written contract stub(s) into workspace")
        for _stub_path, _stub_code in _pre_written.items():
            _stub_full_path = os.path.join(output_dir, _stub_path)
            write_file(_stub_full_path, _stub_code)
            skeletons[_stub_path] = _stub_code

    # Phase 1.5: Validate manifest and install deps
    blog.phase("manifest_validation", "Validating manifest and installing dependencies")
    validate_manifest(output_dir, language, coder_model, skeletons)

    # Phase 2: Fill in each file
    written_files: dict[str, str] = {}

    # System 1: Pre-populate written_files with contract stubs so the Coder
    # treats them as already-implemented files (shows them as context,
    # skips them in the generation loop).
    for _stub_path, _stub_code in _pre_written.items():
        written_files[_stub_path] = _stub_code

    file_index = 0

    for file_path in dep_order:
        file_spec = next((f for f in files_list if f["path"] == file_path), None)
        if not file_spec:
            blog.warning(f"File {file_path} in dep order but not in plan, skipping")
            continue

        # System 1: Skip files that are pre-written contract stubs.
        # The Coder must not regenerate these — they are contract-enforced.
        if file_path in blueprint.get("pre_written_files", {}):
            blog.info(f"Skipping pre-written stub: {file_path}")
            continue

        file_index += 1
        blog.file_start(file_path, file_index, files_total)

        code = fill_in_file(file_spec, skeletons, written_files, blueprint, coder_model, ctx_tokens)

        # System 2: Contract verification against OpenAPI spec (NO LLM in verification)
        if ws.get("openapi_spec") and file_path.endswith((".py", ".js", ".ts", ".jsx", ".tsx")):
            try:
                from skills.builder.engine.contract_verifier import run_contract_verification_loop
                code = run_contract_verification_loop(
                    ws, file_path, code,
                    max_repair_attempts=2,
                    llm_call_fn=llm_call,
                    coder_model=coder_model,
                )
            except Exception as _cv_exc:
                blog.warning(f"Contract verification skipped for {file_path}: {_cv_exc}")

        full_path = os.path.join(output_dir, file_path)
        write_file(full_path, code)
        written_files[file_path] = code
        skeletons[file_path] = code  # Fix 1: Replace skeleton with finished code for subsequent files
        blog.file_done(file_path, len(code))

        # System 2: Update symbol table after each file
        try:
            from skills.builder.engine.contract_verifier import build_ast_symbol_table
            ws["symbol_table"] = build_ast_symbol_table(written_files)
        except Exception:
            pass

        # Quick compile check on just-written file
        success, all_errors = compile_check(output_dir, language)
        file_basename = os.path.basename(file_path)
        errors = [e for e in all_errors if file_basename in e or file_path in e]

        if not errors:
            continue

        # Track errors for KG/KB recording (System 3+4)
        for e in errors:
            ws.setdefault("all_errors", []).append({
                "type": "compile_error", "message": e, "file": file_path,
            })

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
        seen_hashes = set()  # Fix 3: stagnation detection

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

            # Fix 3: stagnation detection - abort if code hasn't changed
            h = _code_hash(repaired)
            if h in seen_hashes:
                blog.warning(f"Repair für {file_path} stagniert nach {repair_attempt} Versuchen – abbrechen")
                break
            seen_hashes.add(h)

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
                # Track repair for KG/KB (System 3+4)
                ws.setdefault("all_repairs", []).append({
                    "description": f"Repaired {file_path} on attempt {repair_attempt}",
                    "file": file_path,
                    "worked": True,
                })
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
    # Skip for languages with a real compiler — it already catches undefined refs.
    COMPILER_CHECKED_LANGUAGES = {"rust", "go", "typescript"}
    if language in COMPILER_CHECKED_LANGUAGES:
        blog.info(
            f"Skipping coherence LLM check for {language} "
            f"(compiler handles undefined reference detection)"
        )
    elif language in ("javascript", "python") and written_files:
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

            # Safety check 1: patched file shouldn't be drastically smaller
            if len(fixed_code) < len(current_code) * 0.85:
                blog.warning(f"Coherence fix for {target_file} was too small, skipping")
                continue

            # Safety check 2: ensure all existing function/class definitions survive
            import re as _def_re
            if language == "python":
                existing_defs = set(_def_re.findall(r'(?:def|class)\s+(\w+)', current_code))
            else:
                existing_defs = set(_def_re.findall(r'(?:function|class)\s+(\w+)', current_code))
            if existing_defs:
                if language == "python":
                    fixed_defs = set(_def_re.findall(r'(?:def|class)\s+(\w+)', fixed_code))
                else:
                    fixed_defs = set(_def_re.findall(r'(?:function|class)\s+(\w+)', fixed_code))
                lost_defs = existing_defs - fixed_defs
                if lost_defs:
                    blog.warning(
                        f"Coherence fix for {target_file} would lose definitions: "
                        f"{', '.join(sorted(lost_defs))} — skipping"
                    )
                    continue

            full_path = os.path.join(output_dir, target_file)
            write_file(full_path, fixed_code)
            written_files[target_file] = fixed_code
            blog.info(f"Patched {target_file}: added {', '.join(missing_names)}")
        except Exception as exc:
            blog.warning(f"Coherence fix failed for {target_file}: {exc}")

    blog.verify(True, "coherence", f"Patched {len(fixes_by_file)} file(s) for undefined references")
    return written_files


# ---------------------------------------------------------------------------
# Project Startup & Consistency Analysis
# ---------------------------------------------------------------------------
def _project_startup_analysis(
    output_dir: str,
    written_files: dict,
    blueprint: dict,
    language: str,
    coder_model: str,
    ctx_tokens: int,
) -> dict:
    """Comprehensive project startup and consistency analysis.

    Runs static checks + LLM analysis to catch:
    1. App initialization (init/initApp defined AND called)
    2. External library loading order in HTML (<script> order)
    3. Duplicate/contradictory implementations across files
    4. index.html cleanliness (entry point only, no duplicate logic)
    5. README consistency with actual project files
    """
    import re as _re
    from collections import Counter

    blog.phase("startup_analysis", "Project startup & consistency analysis", model=coder_model)
    issues: list[str] = []
    fixes_applied = 0

    # ── Static Check 1: HTML script loading order ─────────────────────────
    html_files = {p: c for p, c in written_files.items() if p.endswith(('.html', '.htm'))}
    for html_path, html_content in html_files.items():
        # Extract all <script src="..."> tags in order
        script_srcs = _re.findall(
            r'<script[^>]+src=["\']([^"\']+)["\']', html_content, _re.IGNORECASE
        )
        # Check that external CDN scripts come before local scripts
        seen_local = False
        for src in script_srcs:
            is_external = src.startswith(('http://', 'https://', '//'))
            if is_external and seen_local:
                issues.append(
                    f"[SCRIPT_ORDER] {html_path}: external library '{src}' loaded AFTER "
                    f"local scripts — libraries must be loaded first"
                )
            if not is_external:
                seen_local = True

        # Check that referenced scripts actually exist
        html_dir = os.path.dirname(html_path)
        for src in script_srcs:
            if src.startswith(('http://', 'https://', '//', 'data:')):
                continue
            ref_path = os.path.normpath(os.path.join(html_dir, src)).replace("\\", "/")
            if ref_path not in written_files and not os.path.exists(os.path.join(output_dir, ref_path)):
                issues.append(
                    f"[MISSING_SCRIPT] {html_path}: references '{src}' but file does not exist"
                )

    # ── Static Check 2: Duplicate function definitions ────────────────────
    if language in ("javascript", "html", "typescript"):
        func_locations: dict[str, list[str]] = {}
        for fpath, content in written_files.items():
            if not fpath.endswith(('.js', '.mjs', '.jsx', '.ts', '.tsx')):
                # Also check inline scripts in HTML
                if fpath.endswith(('.html', '.htm')):
                    # Extract inline script content
                    inline_scripts = _re.findall(
                        r'<script(?:\s[^>]*)?>(.+?)</script>',
                        content, _re.DOTALL | _re.IGNORECASE,
                    )
                    inline_code = "\n".join(inline_scripts)
                    if not inline_code.strip():
                        continue
                    for m in _re.finditer(r'function\s+(\w+)\s*\(', inline_code):
                        fname = m.group(1)
                        func_locations.setdefault(fname, []).append(f"{fpath} (inline)")
                    continue
                else:
                    continue
            for m in _re.finditer(r'function\s+(\w+)\s*\(', content):
                fname = m.group(1)
                func_locations.setdefault(fname, []).append(fpath)

        for fname, locations in func_locations.items():
            if len(locations) > 1:
                issues.append(
                    f"[DUPLICATE_FUNC] Function '{fname}' is defined in multiple places: "
                    + ", ".join(locations)
                )

    # ── Static Check 3: index.html bloat (inline logic > 100 lines) ──────
    index_html = written_files.get("index.html", "")
    if index_html:
        inline_scripts = _re.findall(
            r'<script(?:\s[^>]*)?>(.+?)</script>',
            index_html, _re.DOTALL | _re.IGNORECASE,
        )
        total_inline_lines = sum(s.count('\n') + 1 for s in inline_scripts if s.strip())
        if total_inline_lines > 100:
            issues.append(
                f"[INDEX_BLOAT] index.html contains {total_inline_lines} lines of inline "
                f"JavaScript — logic should be in separate .js files"
            )

    # ── Static Check 4: Init function defined but never called ────────────
    if language in ("javascript", "html", "typescript"):
        for fpath, content in written_files.items():
            if not fpath.endswith(('.js', '.mjs')):
                continue
            # Check if init() is defined
            has_init_def = bool(_re.search(r'function\s+init\s*\(', content))
            if not has_init_def:
                continue
            # Check if init() is called anywhere
            has_init_call = bool(_re.search(r'\binit\s*\(', content))
            has_dom_ready = 'DOMContentLoaded' in content or 'readyState' in content
            if not has_init_call and not has_dom_ready:
                # Check if it's called from HTML
                called_from_html = any(
                    f'init()' in html_content or f'init(' in html_content
                    for html_content in html_files.values()
                )
                if not called_from_html:
                    issues.append(
                        f"[INIT_NOT_CALLED] {fpath}: defines init() but never calls it — "
                        f"app will not initialize"
                    )

    # ── Static Check 5: Python entry points without __main__ ──────────────
    if language == "python":
        for fpath, content in written_files.items():
            if not fpath.endswith('.py'):
                continue
            uses_web = any(kw in content for kw in [
                'FastAPI(', 'Flask(', 'Starlette(', 'uvicorn', 'app.run',
            ])
            if uses_web and 'if __name__' not in content:
                issues.append(
                    f"[NO_MAIN_BLOCK] {fpath}: Web app without if __name__ == '__main__' block"
                )

            # Check for CORS
            uses_fastapi = 'FastAPI(' in content
            has_cors = 'CORSMiddleware' in content or 'CORS(' in content
            if uses_fastapi and not has_cors:
                issues.append(
                    f"[NO_CORS] {fpath}: FastAPI app without CORS middleware — "
                    f"frontend requests will fail silently"
                )

    # ── Log static issues ─────────────────────────────────────────────────
    if issues:
        blog.warning(f"Startup analysis found {len(issues)} static issue(s):")
        for issue in issues:
            blog.warning(f"  {issue}")

    # ── LLM-based deep analysis ──────────────────────────────────────────
    code_parts = []
    for fpath in sorted(written_files.keys()):
        content = written_files[fpath]
        preview = content[:3000] if len(content) > 3000 else content
        code_parts.append(f"=== {fpath} ({len(content)} chars) ===\n{preview}")
    all_code = "\n\n".join(code_parts)

    max_chars = int(ctx_tokens * 2.0)
    if len(all_code) > max_chars:
        all_code = all_code[:max_chars] + "\n... (truncated)"

    static_issues_text = ""
    if issues:
        static_issues_text = (
            "\n\nSTATIC ISSUES ALREADY FOUND (verify these and add any others):\n"
            + "\n".join(f"  - {i}" for i in issues)
        )

    analysis_prompt = f"""You are a senior code reviewer performing a STARTUP READINESS ANALYSIS.
A {language} project was just generated for this goal:

GOAL: "{blueprint.get('_goal', '')}"

PROJECT FILES:
{all_code}
{static_issues_text}

ANALYZE THE PROJECT FOR THESE SPECIFIC ISSUES:

1. INITIALIZATION CHAIN: Is the app actually initialized?
   - For JS: Is there an init()/initApp()/main() function that is DEFINED AND CALLED?
   - For Python: Is there an if __name__ == "__main__" block?
   - Are ALL event listeners properly connected?

2. LIBRARY LOADING ORDER: In HTML files:
   - Are external CDN libraries (Chess.js, jQuery, etc.) loaded BEFORE local scripts that use them?
   - Are script tags in the correct dependency order?

3. DUPLICATE LOGIC: Check for contradictions:
   - Is the same logic implemented BOTH inline in index.html AND in separate .js files?
   - Are there conflicting function definitions across files?
   - Is there duplicate state management (e.g., game state in both app.js and index.html)?

4. INDEX.HTML CLEANLINESS:
   - Does index.html serve ONLY as entry point (structure + script loading)?
   - Or does it contain substantial application logic that should be in .js files?

5. CROSS-FILE CONSISTENCY:
   - Do frontend API calls match backend endpoints?
   - Are function signatures consistent between caller and definition?
   - Are all imported modules/files actually present?

For EACH issue found, specify:
- The exact file(s) affected
- What's wrong
- The specific fix needed (be concrete: add line X, remove function Y, move code from A to B)

Output ONLY this JSON:
""" + """{
  "startup_ready": true/false,
  "issues": [
    {
      "file": "<primary file affected>",
      "category": "<INIT_CHAIN|LIBRARY_ORDER|DUPLICATE_LOGIC|INDEX_BLOAT|CROSS_FILE>",
      "problem": "<1-sentence description>",
      "fix": "<concrete fix description>",
      "severity": "<critical|warning>"
    }
  ]
}

If the project looks ready to start, return {"startup_ready": true, "issues": []}.
Output ONLY JSON."""

    llm_issues: list[dict] = []
    try:
        response = llm_call(
            model=coder_model,
            prompt=analysis_prompt,
            system="Senior code reviewer. Analyze for startup readiness. Output ONLY valid JSON.",
            max_tokens=4096,
            temperature=0.05,
        )
        raw = json.loads(clean_json(response))
        if isinstance(raw, dict):
            llm_issues = raw.get("issues", [])
            if raw.get("startup_ready", True) and not llm_issues:
                blog.verify(True, "startup_analysis", "Project passes startup readiness check")
    except Exception as exc:
        blog.warning(f"LLM startup analysis failed ({exc}), relying on static checks only")

    if not llm_issues and not issues:
        blog.verify(True, "startup_analysis", "No startup issues found")
        return written_files

    # ── Auto-fix critical issues ──────────────────────────────────────────
    critical_issues = [i for i in llm_issues if i.get("severity") == "critical"]

    if critical_issues:
        blog.warning(f"Found {len(critical_issues)} critical startup issue(s), attempting auto-fix")

        for issue in critical_issues[:5]:
            target_file = issue.get("file", "")
            category = issue.get("category", "")
            problem = issue.get("problem", "")
            fix_desc = issue.get("fix", "")

            if not target_file or target_file not in written_files:
                # Try to match by basename
                for fpath in written_files:
                    if os.path.basename(fpath) == os.path.basename(target_file):
                        target_file = fpath
                        break
                else:
                    blog.warning(f"Cannot fix '{problem}': file '{target_file}' not found")
                    continue

            current_code = written_files[target_file]
            blog.info(f"Auto-fixing [{category}] in {target_file}: {problem}")

            # For DUPLICATE_LOGIC in index.html: strip inline scripts
            if category == "DUPLICATE_LOGIC" and target_file.endswith(('.html', '.htm')):
                # Remove large inline <script> blocks (keep small ones < 5 lines)
                def _strip_large_inline_scripts(html: str) -> str:
                    def _replace(m):
                        script_body = m.group(1)
                        if script_body.strip().count('\n') < 5:
                            return m.group(0)  # Keep small scripts
                        return ''  # Remove large inline scripts
                    return _re.sub(
                        r'<script(?:\s[^>]*)?>(.+?)</script>',
                        _replace,
                        html, flags=_re.DOTALL | _re.IGNORECASE,
                    )

                fixed_html = _strip_large_inline_scripts(current_code)
                if fixed_html != current_code:
                    full_path = os.path.join(output_dir, target_file)
                    write_file(full_path, fixed_html)
                    written_files[target_file] = fixed_html
                    fixes_applied += 1
                    blog.info(f"Stripped duplicate inline scripts from {target_file}")
                continue

            # For other issues: use LLM to apply the fix
            fix_prompt = f"""Fix this issue in the file below.

ISSUE: {problem}
FIX NEEDED: {fix_desc}

FILE: {target_file}

CURRENT CODE:
{current_code[:12000]}

RULES:
  1. Output the COMPLETE file with the fix applied
  2. Do NOT break existing functionality
  3. Do NOT remove working code unless it's duplicate/contradictory
  4. Do NOT add new external dependencies
  5. Output ONLY code, no markdown fences, no explanation"""

            try:
                fix_response = llm_call(
                    model=coder_model,
                    prompt=fix_prompt,
                    system=f"Expert {language} developer. Apply the fix cleanly. Output ONLY code.",
                    max_tokens=14336,
                    temperature=0.1,
                )
                fixed_code = strip_fences(fix_response)

                # Safety: don't accept drastically smaller files
                if len(fixed_code) >= len(current_code) * 0.5:
                    full_path = os.path.join(output_dir, target_file)
                    write_file(full_path, fixed_code)
                    written_files[target_file] = fixed_code
                    fixes_applied += 1
                    blog.info(f"Applied startup fix to {target_file}: {fix_desc[:80]}")
                else:
                    blog.warning(
                        f"Fix for {target_file} shrank code too much "
                        f"({len(fixed_code)} vs {len(current_code)}), skipping"
                    )
            except Exception as exc:
                blog.warning(f"Auto-fix failed for {target_file}: {exc}")

    total_issues = len(issues) + len(llm_issues)
    blog.verify(
        fixes_applied > 0 or total_issues == 0,
        "startup_analysis",
        f"Found {total_issues} issue(s), applied {fixes_applied} fix(es)"
    )

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

    # Ensure JavaScript files have DOMContentLoaded init pattern
    if language in ("javascript", "html", "typescript"):
        _ensure_js_init_pattern(output_dir, ws.get("written_files", {}))

    # Ensure frontend JS files use API_BASE_URL for API calls
    if ws.get("blueprint"):
        _ensure_frontend_api_base_url(output_dir, ws.get("written_files", {}), ws["blueprint"])

    # Comprehensive startup & consistency analysis (static + LLM)
    if ws.get("written_files") and ws.get("blueprint"):
        ws["written_files"] = _project_startup_analysis(
            output_dir=output_dir,
            written_files=ws["written_files"],
            blueprint=ws["blueprint"],
            language=language,
            coder_model=ws.get("coder_model", ws.get("manager_model", "")),
            ctx_tokens=ws.get("ctx_tokens", DEFAULT_CTX_TOKENS),
        )

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

    # Compile-repair: fix compile errors even when sandbox passed (or was skipped)
    if not ws.get("compile_ok", True) and written_files:
        blog.phase("critic_compile_repair", "Critic: repairing remaining compile errors", model=coder_model)
        compile_repair_ok = False
        for compile_attempt in range(MAX_SANDBOX_RETRIES):
            success, all_errors = compile_check(output_dir, language)
            if success:
                ws["compile_ok"] = True
                compile_repair_ok = True
                blog.verify(True, "critic_compile", f"Compile errors fixed on attempt {compile_attempt + 1}")
                break

            # Find the file with the most errors and repair it
            from collections import Counter
            file_error_counts: Counter = Counter()
            for e in all_errors:
                for fpath in written_files:
                    fname = os.path.basename(fpath)
                    if fname in e or fpath in e:
                        file_error_counts[fpath] += 1
                        break

            if not file_error_counts:
                blog.warning("Compile errors exist but cannot identify affected files")
                break

            worst_file = file_error_counts.most_common(1)[0][0]
            file_errors = [e for e in all_errors if os.path.basename(worst_file) in e or worst_file in e]
            code = written_files[worst_file]

            blog.repair(worst_file, compile_attempt + 1, MAX_SANDBOX_RETRIES)

            repaired = repair_file(
                worst_file, code, file_errors, language, coder_model,
                written_files=written_files,
            ) if compile_attempt == 0 else patch_repair_file(
                worst_file, code, file_errors, language, coder_model,
                written_files=written_files,
            )

            full_path = os.path.join(output_dir, worst_file)
            write_file(full_path, repaired)
            written_files[worst_file] = repaired
            format_code(output_dir, language)

        if not compile_repair_ok:
            blog.warning("Critic could not resolve all compile errors")

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
                    except Exception as _e:
                        blog.warning(f"[non-critical] Diagnosis parse failed: {_e}")

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
    skip_polish = os.environ.get("BUILDER_SKIP_POLISH", "").lower() in ("1", "true", "yes")

    if not ws["sandbox_ok"]:
        blog.info("Skipping polish phase: build has unresolved errors")
    elif skip_polish:
        blog.info("Skipping polish phase: BUILDER_SKIP_POLISH=1")
    elif written_files:
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
