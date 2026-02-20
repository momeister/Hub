"""
skills/builder/engine/pipeline.py - Build orchestration
=======================================================
Uses the 5-agent sequential pipeline:
  Planner -> Retriever -> Coder -> Executor -> Critic

Each agent runs one at a time (sequential model loading for VRAM efficiency).
"""

from __future__ import annotations

import os
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
from skills.builder.engine.blueprint import architect_phase
from skills.builder.engine.context import blog, DEFAULT_CTX_TOKENS, IN_DOCKER
from skills.builder.engine.critic import critic_review_blueprint

# File-based approval signal -- shared Docker volume
_APPROVAL_SIGNAL = "/app/output/.build_approval"


def _cleanup_approval_signal():
    """Remove stale approval signal file."""
    try:
        if os.path.exists(_APPROVAL_SIGNAL):
            os.remove(_APPROVAL_SIGNAL)
    except OSError:
        pass


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


def _wait_for_approval(timeout: int = 7200) -> str:
    """Poll for approval signal file written by the gateway.

    Returns 'approved' or 'cancelled'.
    """
    _cleanup_approval_signal()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(_APPROVAL_SIGNAL):
            try:
                with open(_APPROVAL_SIGNAL, "r") as f:
                    result = f.read().strip().lower()
                os.remove(_APPROVAL_SIGNAL)
                return result if result in ("approved", "cancelled") else "cancelled"
            except OSError:
                return "cancelled"
        time.sleep(1)
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


def build_single_language_project(
    goal: str,
    blueprint: dict,
    manager_model: str,
    coder_model: str,
    output_dir: str,
    ctx_tokens: int,
    parent_goal: str = "",
) -> dict:
    """Build a single-language project using the 5-agent pipeline."""
    language = blueprint["language"]
    # Ensure the goal is accessible to coder agents via the blueprint
    blueprint.setdefault("_goal", goal)

    blog.info(f"Language: {language}")
    blog.info("Mode: AGENT PIPELINE (Planner->Retriever->Coder->Executor->Critic)")

    # Create workspace with pre-set blueprint (skips Planner agent)
    ws = make_workspace(goal, manager_model, coder_model, output_dir, ctx_tokens)
    ws["blueprint"] = blueprint
    ws["language"] = language

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

    # Agent 1: Planner (uses manager model)
    ws = make_workspace(goal, manager_model, coder_model, output_dir, ctx_tokens)
    ws = agent_planner(ws)
    blueprint = ws["blueprint"]
    # Critic läuft bereits intern in agent_planner — kein zweiter Call nötig

    if os.environ.get("TRIGGERED_BY") == "telegram":
        file_paths = [f["path"] for f in blueprint.get("files", [])]
        arch_decisions = blueprint.get("architecture_decisions", [])
        blog.approval_needed(
            language=blueprint.get("language", "?"),
            framework=blueprint.get("framework", ""),
            why=blueprint.get("why", ""),
            files_total=len(file_paths),
            file_paths=file_paths,
            complexity=blueprint.get("estimated_complexity", ""),
            architecture_decisions=[d.get("decision", "") for d in arch_decisions],
        )
        blog.info("Waiting for user approval via Telegram...")
        approval = _wait_for_approval()
        if approval != "approved":
            blog.info("Build cancelled by user")
            blog.complete(
                success=False,
                files_written=0,
                elapsed_sec=int(time.time() - start_time),
                output_dir=output_dir,
            )
            return

        blog.phase("approved", "User approved tech stack and plan - continuing build")

    if blueprint.get("is_multi_language") and blueprint.get("subprojects"):
        blog.info("Multi-language project detected")

        subprojects = blueprint["subprojects"]
        master_files = blueprint.get("files", [])

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
                blog.warning(f"No files found for subproject '{sub_name}', running architect")
                sub_blueprint = architect_phase(
                    f"Build the {sub_name} ({sub_lang}/{sub.get('framework','')}) "
                    f"component for: {goal}. "
                    f"Use {sub_lang} with {sub.get('framework','')}. "
                    f"Do NOT change the language or framework.",
                    manager_model,
                )
                sub_blueprint = critic_review_blueprint(
                    f"{sub_name} for: {goal}", sub_blueprint, manager_model
                )
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
                }

                blog.info(
                    f"Derived sub-blueprint for '{sub_name}': {len(sub_files)} files, lang={sub_lang}"
                )

            build_single_language_project(
                goal=f"{sub_name} for: {goal}",
                blueprint=sub_blueprint,
                manager_model=manager_model,
                coder_model=coder_model,
                output_dir=sub_dir,
                ctx_tokens=ctx_tokens,
                parent_goal=goal,
            )

        generate_multi_language_readme(goal, subprojects, output_dir)
        generate_project_start_bat(blueprint, output_dir, {}, subprojects=subprojects)
    else:
        build_single_language_project(goal, blueprint, manager_model, coder_model, output_dir, ctx_tokens)

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
    except Exception:
        pass  # Memory ist optional
