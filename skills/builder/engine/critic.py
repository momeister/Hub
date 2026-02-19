"""
skills/builder/engine/critic.py - Blueprint review and diagnosis
================================================================
"""

from __future__ import annotations

import json
import os

from skills.builder.engine.context import blog, llm_call, clean_json


def critic_review_blueprint(goal: str, blueprint: dict, manager_model: str) -> dict:
    blog.phase("critic", "Reviewing blueprint before code generation", model=manager_model)

    language = blueprint.get("language", "unknown")
    files = blueprint.get("files", [])
    dep_order = blueprint.get("dependency_order", [])

    file_summary = "\n".join(
        f"  - {f['path']}: {f.get('purpose', '?')}" for f in files[:25]
    )
    dep_content = blueprint.get("dependencies", {}).get("content", "")[:500]

    prompt = f"""You are a code project reviewer. A blueprint was generated for this goal:

GOAL: "{goal}"

BLUEPRINT SUMMARY:
  Language: {language}
  Framework: {blueprint.get('framework', 'none')}
  Files ({len(files)}):
{file_summary}
  Dependency order: {dep_order[:20]}
  Dependencies: {dep_content[:300]}

Review for these issues ONLY:
1. WRONG LANGUAGE: Does the language fit the goal? (e.g. game -> HTML+JS, CLI -> python/go)
2. MISSING FILES: Are critical files missing? (entry point, config, core logic)
3. EXCESS FILES: Too many files for a simple project? (>8 files for simple, >15 for medium)
4. DEP ORDER: Will the build order cause import failures?
5. GOAL MISMATCH: Does the file plan actually implement what was asked?

Output ONLY this JSON:
""" + """{
  "approved": true/false,
  "issues": ["issue 1", "issue 2"],
  "patches": [
    {"action": "add_file", "path": "...", "purpose": "..."},
    {"action": "remove_file", "path": "..."},
    {"action": "reorder", "dependency_order": ["..."]},
    {"action": "change_language", "language": "...", "reason": "..."}
  ]
}

If the blueprint looks good, return {"approved": true, "issues": [], "patches": []}.
Be strict but practical. Output ONLY JSON."""

    try:
        response = llm_call(
            model=manager_model,
            prompt=prompt,
            system="You are a strict code reviewer. Output only valid JSON. No explanation.",
            max_tokens=2048,
            temperature=0.05,
        )

        raw = json.loads(clean_json(response))

        if not isinstance(raw, dict):
            blog.warning("Critic returned non-dict, skipping review")
            return blueprint

        approved = raw.get("approved", True)
        issues = raw.get("issues", [])
        patches = raw.get("patches", [])

        if approved:
            blog.info("Critic approved blueprint")
            return blueprint

        for issue in issues[:5]:
            blog.warning(f"Critic issue: {issue}")

        patched = False
        for patch in patches[:5]:
            action = patch.get("action", "")

            if action == "add_file":
                path = patch.get("path", "")
                purpose = patch.get("purpose", "")
                if path and path not in {f["path"] for f in blueprint["files"]}:
                    blueprint["files"].append({
                        "path": path,
                        "purpose": purpose,
                        "exports": [],
                        "imports": [],
                        "estimated_lines": 30,
                        "critical": False,
                    })
                    if path not in blueprint.get("dependency_order", []):
                        blueprint["dependency_order"].append(path)
                    blog.info(f"Critic added file: {path}")
                    patched = True

            elif action == "remove_file":
                path = patch.get("path", "")
                if path:
                    before_count = len(blueprint["files"])
                    blueprint["files"] = [f for f in blueprint["files"] if f["path"] != path]
                    if len(blueprint["files"]) < before_count:
                        blueprint["dependency_order"] = [
                            p for p in blueprint.get("dependency_order", []) if p != path
                        ]
                        blog.info(f"Critic removed file: {path}")
                        patched = True

            elif action == "reorder":
                new_order = patch.get("dependency_order", [])
                if new_order and isinstance(new_order, list):
                    valid_paths = {f["path"] for f in blueprint["files"]}
                    if all(p in valid_paths for p in new_order):
                        for p in blueprint.get("dependency_order", []):
                            if p not in new_order:
                                new_order.append(p)
                        blueprint["dependency_order"] = new_order
                        blog.info("Critic reordered dependency chain")
                        patched = True

            elif action == "change_language":
                new_lang = patch.get("language", "")
                reason = patch.get("reason", "")
                blog.warning(
                    f"Critic suggests language change: {language} -> {new_lang} ({reason}). "
                    "Skipping - would require full re-architecture."
                )

        if patched:
            blog.info("Blueprint patched by critic")
        else:
            blog.info("Critic had issues but no applicable patches")

        return blueprint

    except Exception as exc:
        blog.warning(f"Critic review failed ({exc}), proceeding with original blueprint")
        return blueprint


def critic_diagnose_failure(
    error_output: str,
    written_files: dict,
    language: str,
    coder_model: str,
) -> dict:
    blog.phase("critic_diagnose", "Analyzing sandbox failure", model=coder_model)

    file_list = "\n".join(f"  - {p}" for p in sorted(written_files.keys())[:20])

    prompt = f"""A {language} project failed at runtime. Diagnose the failure.

ERROR OUTPUT:
{error_output[:2000]}

PROJECT FILES:
{file_list}

Determine:
1. Which SINGLE file is most likely the root cause
2. What went wrong (1 sentence)
3. How to fix it (1 sentence)

Output ONLY this JSON:
""" + """{
  "file": "<path of file to fix>",
  "root_cause": "<what went wrong>",
  "fix_strategy": "<how to fix it>"
}

RULES:
  - "file" MUST be one of the project files listed above
  - Be specific, not generic
  - Output ONLY JSON"""

    try:
        response = llm_call(
            model=coder_model,
            prompt=prompt,
            system="Expert debugger. Output only valid JSON.",
            max_tokens=1536,
            temperature=0.05,
        )

        raw = json.loads(clean_json(response))

        if not isinstance(raw, dict) or "file" not in raw:
            blog.warning("Critic diagnosis returned invalid format")
            return {}

        file_to_fix = raw.get("file", "")
        root_cause = raw.get("root_cause", "Unknown")
        fix_strategy = raw.get("fix_strategy", "")

        if file_to_fix not in written_files:
            for fpath in written_files:
                if os.path.basename(fpath) == os.path.basename(file_to_fix):
                    file_to_fix = fpath
                    break
            else:
                blog.warning(f"Critic identified {file_to_fix} but it's not in project files")
                return {}

        blog.info(f"Critic diagnosis: {file_to_fix} - {root_cause}")
        return {
            "file": file_to_fix,
            "root_cause": root_cause,
            "fix_strategy": fix_strategy,
        }

    except Exception as exc:
        blog.warning(f"Critic diagnosis failed ({exc}), falling back to heuristics")
        return {}
