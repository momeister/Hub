"""
skills/builder/engine/manifest.py - Manifest validation
=======================================================
"""

from __future__ import annotations

import os

from skills.builder.engine.context import blog, llm_call, strip_fences, write_file, read_file
from skills.builder.engine.deps import install_deps


def validate_manifest(output_dir: str, language: str, coder_model: str, skeletons: dict) -> bool:
    """
    Validate manifest files and attempt repair if dependency install fails.
    """
    manifest_map = {
        "rust": "Cargo.toml",
        "go": "go.mod",
        "javascript": "package.json",
        "typescript": "package.json",
        "python": "requirements.txt",
    }

    manifest_name = manifest_map.get(language)
    if not manifest_name:
        return True

    manifest_path = os.path.join(output_dir, manifest_name)
    if not os.path.exists(manifest_path):
        return True

    dep_ok, dep_msg = install_deps(output_dir, language)
    if dep_ok:
        return True

    blog.warning(f"Manifest validation failed for {manifest_name}: {dep_msg}")

    manifest_content = read_file(manifest_path)
    if not manifest_content.strip():
        return False

    prompt = f"""The {manifest_name} file for this {language} project has dependency errors.

ERROR:
{dep_msg[:1500]}

CURRENT {manifest_name}:
{manifest_content}

Fix the {manifest_name} so all dependencies resolve correctly.
Common issues:
  - Wrong crate/package names (typos, renamed packages)
  - Nonexistent versions (use latest stable or remove version pin)
  - Missing required fields

Output ONLY the corrected {manifest_name} content. No explanation, no fences."""

    try:
        response = llm_call(
            model=coder_model,
            prompt=prompt,
            system=f"Expert {language} developer. Fix dependency manifest. Output ONLY the file content.",
            max_tokens=4096,
            temperature=0.05,
        )
        fixed = strip_fences(response)
        write_file(manifest_path, fixed)

        if manifest_name in skeletons:
            skeletons[manifest_name] = fixed

        dep_ok2, dep_msg2 = install_deps(output_dir, language)
        if dep_ok2:
            blog.verify(True, "manifest_repair", f"{manifest_name} repaired successfully")
            return True
        blog.warning(f"Manifest repair attempt failed: {dep_msg2}")
        return False

    except Exception as exc:
        blog.warning(f"Manifest repair LLM call failed: {exc}")
        return False
