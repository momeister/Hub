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

    # Extract the project goal for context
    goal = blueprint.get("project_name", "").replace("_", " ")

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
  11. For Python web apps (FastAPI/Flask), the entry point MUST include:
      if __name__ == "__main__": with uvicorn.run() or app.run()"""

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
    project_goal = blueprint.get("project_name", "").replace("_", " ")

    ctx_parts = []

    # Include the overall project goal so the coder understands the full picture
    goal_text = blueprint.get("_goal", "")
    if goal_text:
        ctx_parts.append(f"=== PROJECT GOAL ===\n{goal_text}\n")

    if written_files:
        ctx_parts.append("=== ALREADY IMPLEMENTED FILES (full code) ===")
        for w_path, w_code in written_files.items():
            ctx_parts.append(f"\n=== {w_path} (fully implemented) ===\n{w_code}")

    pending = {p: c for p, c in all_skeletons.items() if p not in written_files}
    if pending:
        ctx_parts.append("=== PENDING FILES (skeleton only, not yet implemented) ===")
        for sk_path, sk_code in pending.items():
            ctx_parts.append(f"\n=== {sk_path} ===\n{sk_code}")
    if written_files:
        ctx_parts.append(
            f"\n=== ALREADY IMPLEMENTED: {', '.join(written_files.keys())} "
            f"(full code shown above) ==="
        )

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
  5. Add brief comments for complex logic{entry_point_rule}"""

    system = f"Expert {language} developer. Output ONLY code, no markdown fences."

    response = llm_call(
        model=coder_model,
        prompt=prompt,
        system=system,
        max_tokens=14336,
        temperature=0.1,
    )

    return strip_fences(response)
