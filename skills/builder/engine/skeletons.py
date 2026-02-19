"""
skills/builder/engine/skeletons.py - Skeleton generation
========================================================
"""

from __future__ import annotations

from core.utils import estimate_tokens

from skills.builder.engine.context import blog, llm_call, strip_fences
from skills.builder.engine.utils import parse_multi_file_output


def generate_all_skeletons(blueprint: dict, coder_model: str) -> dict:
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

    prompt = f"""Generate SKELETONS (signatures only, NO implementations) for ALL files in this {language} project.

PROJECT: "{blueprint.get('project_name', '')}"
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
  2. Signatures/types/interfaces ONLY
  3. For functions: signature + pass/... (no impl)
  4. For structs/classes: fields + method signatures
  5. Imports must be CORRECT (use the exports list)
  6. NO implementation bodies
  7. Config files (Cargo.toml, package.json, etc.) should be COMPLETE
  8. For dependency manifests (requirements.txt, package.json, Cargo.toml):
     - Do NOT pin exact versions (no ==1.2.3)
     - Use loose version constraints (>=1.0 or just the package name)
     - Only include packages that ACTUALLY EXIST on PyPI/npm/crates.io
     - Prefer well-known, popular packages"""

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

    ctx_parts = []

    ctx_parts.append("=== ALL FILE SKELETONS (reference for imports/types) ===")
    for sk_path, sk_code in all_skeletons.items():
        ctx_parts.append(f"\n=== {sk_path} ===\n{sk_code}")

    if written_files:
        ctx_parts.append("\n\n=== ALREADY IMPLEMENTED FILES ===")
        for w_path, w_code in written_files.items():
            ctx_parts.append(f"\n=== {w_path} ===\n{w_code[:3000]}")

    deps = blueprint.get("dependencies", {})
    if deps.get("content"):
        ctx_parts.append(f"\n\n=== DEPENDENCIES ({deps.get('type', '')}) ===\n{deps['content']}")

    context = "\n".join(ctx_parts)

    ctx_estimated_tokens = estimate_tokens(context)
    max_ctx_tokens = int(ctx_tokens * 0.8)
    if ctx_estimated_tokens > max_ctx_tokens:
        blog.warning(
            f"Context too large ({ctx_estimated_tokens:,} est. tokens > {max_ctx_tokens:,} limit), truncating..."
        )
        max_chars = int(max_ctx_tokens * 3.5)
        context = context[:max_chars]

    prompt = f"""Implement the file: {path}

Purpose: {purpose}

{context}

OUTPUT:
Complete implementation for {path}. Output ONLY the code, no fences, no explanation.

RULES:
  1. Use EXACT imports from skeletons
  2. Full implementation, no placeholders
  3. Follow language idioms
  4. Handle errors properly
  5. Add brief comments for complex logic"""

    system = f"Expert {language} developer. Output ONLY code, no markdown fences."

    response = llm_call(
        model=coder_model,
        prompt=prompt,
        system=system,
        max_tokens=14336,
        temperature=0.1,
    )

    return strip_fences(response)
