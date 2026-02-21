"""
skills/builder/engine/skeletons.py - Skeleton generation
========================================================
"""

from __future__ import annotations

import os

from core.utils import estimate_tokens

from skills.builder.engine.context import blog, llm_call, strip_fences
from skills.builder.engine.utils import parse_multi_file_output


def _build_prioritized_context(
    goal_text: str,
    contracts: list,
    written_files: dict,
    pending_skeletons: dict,
    deps: dict,
    ctx_tokens: int,
    current_file_path: str,
) -> str:
    """
    Baut den Kontext in Prioritätsreihenfolge auf und schneidet nur
    die am wenigsten wichtigen Teile ab, wenn das Limit erreicht wird.

    Priorität (höchste zuerst):
    1. PROJECT GOAL + DATA CONTRACTS  — immer vollständig
    2. PENDING SKELETONS              — immer vollständig (klein, kritisch)
    3. DEPENDENCIES                   — immer vollständig (klein, kritisch)
    4. DIRECTLY IMPORTED files        — vollständig (meist 1-2 Dateien)
    5. OTHER IMPLEMENTED files        — gekürzt auf je 50 Zeilen
    """
    max_chars = int(ctx_tokens * 0.8 * 3.5)  # Token → char estimate

    parts_critical = []   # Immer drin, nie gekürzt
    parts_important = []  # Vollständig, wenn Platz vorhanden
    parts_optional = []   # Gekürzt, wenn nötig

    # Prio 1: Goal + Contracts (immer)
    if goal_text:
        parts_critical.append(f"=== PROJECT GOAL ===\n{goal_text}")
    if contracts:
        contract_lines = [
            f"  {c['name']}: {c['structure']}\n  Example: {c['example']}"
            for c in contracts
        ]
        parts_critical.append(
            "=== SHARED DATA CONTRACTS (MANDATORY) ===\n" + "\n\n".join(contract_lines)
        )

    # Prio 2: Pending Skeletons (immer — der Coder braucht die Interfaces)
    if pending_skeletons:
        sk_lines = ["=== PENDING FILES (skeleton only, not yet implemented) ==="]
        for sk_path, sk_code in pending_skeletons.items():
            sk_lines.append(f"\n=== {sk_path} ===\n{sk_code}")
        parts_critical.append("\n".join(sk_lines))

    # Prio 3: Dependencies (immer)
    if deps.get("content"):
        parts_critical.append(
            f"=== DEPENDENCIES ({deps.get('type', '')}) ===\n{deps['content']}"
        )

    # Prio 4: Direkt importierte Dateien (vollständig)
    # Heuristik: Dateien deren Name im current file skeleton vorkommt
    directly_imported = {}
    other_implemented = {}
    current_skeleton = pending_skeletons.get(current_file_path, "")
    for w_path, w_code in written_files.items():
        stem = os.path.splitext(os.path.basename(w_path))[0]
        if stem.lower() in current_skeleton.lower():
            directly_imported[w_path] = w_code
        else:
            other_implemented[w_path] = w_code

    if directly_imported:
        di_lines = ["=== DIRECTLY IMPORTED FILES (full code) ==="]
        for w_path, w_code in directly_imported.items():
            di_lines.append(f"\n=== {w_path} (fully implemented) ===\n{w_code}")
        parts_important.append("\n".join(di_lines))

    # Prio 5: Andere implementierte Dateien (gekürzt auf 50 Zeilen)
    if other_implemented:
        oi_lines = ["=== OTHER IMPLEMENTED FILES (first 50 lines each) ==="]
        for w_path, w_code in other_implemented.items():
            preview = "\n".join(w_code.splitlines()[:50])
            oi_lines.append(f"\n=== {w_path} (implemented, preview) ===\n{preview}")
        parts_optional.append("\n".join(oi_lines))

    # Zusammenbauen mit Budgetüberwachung
    critical_str = "\n\n".join(parts_critical)
    important_str = "\n\n".join(parts_important)
    optional_str = "\n\n".join(parts_optional)

    result = critical_str
    remaining = max_chars - len(result)

    if remaining > 0 and important_str:
        if len(important_str) <= remaining:
            result += "\n\n" + important_str
            remaining -= len(important_str)
        else:
            result += "\n\n" + important_str[:remaining]
            remaining = 0

    if remaining > 0 and optional_str:
        if len(optional_str) <= remaining:
            result += "\n\n" + optional_str
        else:
            result += "\n\n" + optional_str[:remaining]

    return result


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

    # Extract the FULL project goal for context (not just the name)
    goal = blueprint.get("_goal", "") or blueprint.get("project_name", "").replace("_", " ")

    # Build data contracts section
    contracts = blueprint.get("data_contracts", [])
    contracts_section = ""
    if contracts:
        contract_lines = []
        for c in contracts:
            contract_lines.append(
                f"  {c['name']}: {c['description']}\n"
                f"    Structure: {c['structure']}\n"
                f"    Example: {c['example']}\n"
                f"    Used in: {', '.join(c.get('consumed_by', []) + c.get('produced_by', []))}"
            )
        contracts_section = (
            "\nSHARED DATA CONTRACTS (CRITICAL — use these EXACT structures, do NOT deviate):\n"
            + "\n".join(contract_lines)
            + "\n"
        )

    prompt = f"""Generate SKELETONS (signatures only, NO implementations) for ALL files in this {language} project.

PROJECT: "{blueprint.get('project_name', '')}"
FULL GOAL: "{goal}"
{contracts_section}
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


def _generate_single_skeleton(file_spec: dict, blueprint: dict, coder_model: str) -> str:
    """Generate the skeleton for exactly one file as a targeted fallback."""
    path = file_spec["path"]
    purpose = file_spec.get("purpose", "")
    language = blueprint.get("language", "unknown")
    exports = file_spec.get("exports", [])
    imports_needed = file_spec.get("imports", [])

    prompt = f"""Generate a skeleton (stubs only, no implementation) for this {language} file.

FILE: {path}
PURPOSE: {purpose}
MUST EXPORT: {exports}
IMPORTS FROM OTHER PROJECT FILES: {imports_needed}

Rules:
- Function/class/variable stubs only (pass / ... / empty body)
- Correct import statements at the top
- NO implementation logic
- Output ONLY the file content, no fences, no explanation."""

    response = llm_call(
        model=coder_model,
        prompt=prompt,
        system=f"Expert {language} developer. Output ONLY skeleton code.",
        max_tokens=2048,
        temperature=0.05,
    )
    return strip_fences(response).strip()


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

    # Build context with prioritized truncation
    pending = {p: c for p, c in all_skeletons.items() if p not in written_files}
    contracts = blueprint.get("data_contracts", [])
    deps = blueprint.get("dependencies", {})

    context = _build_prioritized_context(
        goal_text=blueprint.get("_goal", ""),
        contracts=contracts,
        written_files=written_files,
        pending_skeletons=pending,
        deps=deps,
        ctx_tokens=ctx_tokens,
        current_file_path=path,
    )

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

    # Build a list of all function/class names from skeletons that other files
    # might depend on — the coder MUST implement every one of them.
    skeleton_for_this = all_skeletons.get(path, "")
    exports_hint = ""
    if skeleton_for_this:
        import re as _skel_re
        skel_funcs = _skel_re.findall(
            r'(?:function\s+|def\s+|class\s+|export\s+(?:default\s+)?(?:function\s+|class\s+)?)([A-Za-z_][A-Za-z0-9_]*)\s*[({]',
            skeleton_for_this,
        )
        if skel_funcs:
            exports_hint = (
                f"\n  6. CRITICAL: Your skeleton defines these names: {', '.join(skel_funcs)}\n"
                f"     You MUST implement ALL of them. Other files depend on these.\n"
                f"     Do NOT rename, omit, or reorganize them.\n"
                f"     Every function/class from the skeleton MUST appear in your output."
            )

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
  5. Add brief comments for complex logic{exports_hint}{entry_point_rule}"""

    system = f"Expert {language} developer. Output ONLY code, no markdown fences."

    response = llm_call(
        model=coder_model,
        prompt=prompt,
        system=system,
        max_tokens=14336,
        temperature=0.1,
    )

    return strip_fences(response)
