"""
skills/builder/engine/manifest.py - Manifest validation
=======================================================
"""

from __future__ import annotations

import json
import os
import re

from skills.builder.engine.context import blog, llm_call, strip_fences, write_file, read_file
from skills.builder.engine.deps import install_deps

MAX_MANIFEST_REPAIR = 3


def _extract_bad_packages(error_msg: str, language: str) -> list[str]:
    """Parse install error output and return list of package names that 404'd / don't exist."""
    bad = []
    if language in ("javascript", "typescript"):
        # npm: "404 Not Found - GET https://registry.npmjs.org/chessboard.js"
        # npm: "ERR! 404  'some-pkg@*' is not in this registry"
        for m in re.finditer(r"404.*?(?:registry\.npmjs\.org/|')([\w@/.-]+)", error_msg):
            pkg = m.group(1).strip("'\"")
            if pkg and pkg not in bad:
                bad.append(pkg)
        # Also: "npm ERR! code E404"  followed by package name
        for m in re.finditer(r"npm ERR! 404\s+'?([\w@/.-]+)", error_msg):
            pkg = m.group(1).strip("'\"")
            if pkg and pkg not in bad:
                bad.append(pkg)
    elif language == "python":
        # pip: "ERROR: No matching distribution found for chessboard-js"
        # pip: "ERROR: Could not find a version that satisfies the requirement foo"
        for m in re.finditer(
            r"No matching distribution found for ([\w.-]+)|"
            r"Could not find a version that satisfies the requirement ([\w.-]+)",
            error_msg,
        ):
            pkg = m.group(1) or m.group(2)
            if pkg and pkg not in bad:
                bad.append(pkg)
    elif language in ("rust",):
        for m in re.finditer(r"no matching package named `([\w_-]+)`", error_msg):
            if m.group(1) not in bad:
                bad.append(m.group(1))
    return bad


def _strip_bad_packages_from_manifest(
    manifest_path: str, bad_packages: list[str], language: str
) -> bool:
    """Remove known-bad packages from a manifest file. Returns True if changed."""
    content = read_file(manifest_path)
    if not content.strip():
        return False

    original = content

    if language == "python":
        # requirements.txt: one package per line
        lines = content.splitlines()
        cleaned = []
        for line in lines:
            stripped = line.strip()
            pkg_name = re.split(r"[><=!~\[]", stripped)[0].strip()
            if pkg_name.lower() in [b.lower() for b in bad_packages]:
                blog.info(f"Stripping bad package from requirements.txt: {pkg_name}")
                continue
            cleaned.append(line)
        content = "\n".join(cleaned) + "\n"

    elif language in ("javascript", "typescript"):
        # package.json: remove from dependencies / devDependencies
        try:
            pkg = json.loads(content)
            for section in ("dependencies", "devDependencies"):
                deps = pkg.get(section, {})
                for bad in bad_packages:
                    if bad in deps:
                        blog.info(f"Stripping bad package from package.json {section}: {bad}")
                        del deps[bad]
            content = json.dumps(pkg, indent=2) + "\n"
        except json.JSONDecodeError:
            return False

    if content != original:
        write_file(manifest_path, content)
        return True
    return False


def validate_manifest(
    output_dir: str,
    language: str,
    coder_model: str,
    skeletons: dict | None = None,
) -> bool:
    """
    Validate manifest files and attempt repair if dependency install fails.
    Retries up to MAX_MANIFEST_REPAIR times, stripping bad packages on 404.
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

    for attempt in range(1, MAX_MANIFEST_REPAIR + 1):
        blog.info(f"Manifest repair attempt {attempt}/{MAX_MANIFEST_REPAIR}")

        # First, try to strip known-bad packages automatically
        bad_pkgs = _extract_bad_packages(dep_msg, language)
        if bad_pkgs:
            blog.info(f"Detected bad packages: {bad_pkgs}")
            stripped = _strip_bad_packages_from_manifest(manifest_path, bad_pkgs, language)
            if stripped:
                dep_ok, dep_msg = install_deps(output_dir, language)
                if dep_ok:
                    blog.verify(True, "manifest_repair",
                                f"{manifest_name} fixed by stripping bad packages")
                    if skeletons and manifest_name in skeletons:
                        skeletons[manifest_name] = read_file(manifest_path)
                    return True

        # LLM-based repair
        manifest_content = read_file(manifest_path)
        if not manifest_content.strip():
            return False

        prompt = f"""The {manifest_name} file for this {language} project has dependency errors.

ERROR:
{dep_msg[:1500]}

{"BAD PACKAGES (confirmed non-existent, do NOT re-add): " + ", ".join(bad_pkgs) if bad_pkgs else ""}

CURRENT {manifest_name}:
{manifest_content}

Fix the {manifest_name} so all dependencies resolve correctly.
Common issues:
  - Hallucinated package names that don't exist on PyPI/npm/crates.io
  - Wrong crate/package names (typos, renamed packages)
  - Nonexistent versions (use latest stable or remove version pin)
  - Missing required fields

IMPORTANT: Only use packages that ACTUALLY EXIST. If you're not sure a
package exists, remove it or replace it with a well-known alternative.

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

            if skeletons and manifest_name in skeletons:
                skeletons[manifest_name] = fixed

            dep_ok, dep_msg = install_deps(output_dir, language)
            if dep_ok:
                blog.verify(True, "manifest_repair",
                            f"{manifest_name} repaired on attempt {attempt}")
                return True
            blog.warning(f"Manifest repair attempt {attempt} failed: {dep_msg}")

        except Exception as exc:
            blog.warning(f"Manifest repair LLM call failed: {exc}")

    blog.error(f"Manifest repair exhausted {MAX_MANIFEST_REPAIR} attempts", severity="deps")
    return False
