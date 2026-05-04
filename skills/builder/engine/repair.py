"""
skills/builder/engine/repair.py - Repair helpers
================================================
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from skills.builder.engine.context import blog, llm_call, strip_fences, _meaningful_lines
from skills.builder.engine.error_analysis import analyze_errors


def _find_related_files(file_path: str, code: str, written_files: dict, language: str) -> dict:
    """Find project files that the broken file imports from."""
    related = {}
    if not written_files:
        return related

    import_patterns = {
        "rust": [r"use\s+crate::(\w+)", r"mod\s+(\w+)"] ,
        "go": [r'import\s+"[^"]*?/(\w+)"', r'import\s+\(\s*(?:[^)]*?"[^"]*?/(\w+)")+'],
        "python": [r"from\s+([\w.]+)\s+import", r"import\s+([\w.]+)"],
        "typescript": [r"from\s+['\"]\.?\.?/?(\w+)['\"]", r"import\s+.*?from\s+['\"]\.?\.?/?(\w+)['\"]"],
        "javascript": [r"require\(['\"]\.?\.?/?(\w+)['\"]\)", r"from\s+['\"]\.?\.?/?(\w+)['\"]"],
    }

    patterns = import_patterns.get(language, [])
    imported_names = set()
    for pat in patterns:
        for match in re.finditer(pat, code):
            for group in match.groups():
                if group:
                    imported_names.add(group.lower())

    for fpath, content in written_files.items():
        if fpath == file_path:
            continue
        fname = Path(fpath).stem.lower()
        if fname in imported_names or any(name in fpath.lower() for name in imported_names):
            related[fpath] = content[:2000]
            if len(related) >= 5:
                break

    return related


def smart_repair_prompt(
    file_path: str,
    code: str,
    errors: list[str],
    language: str,
    written_files: dict | None = None,
) -> str:
    """Generate repair prompt with error analysis and cross-file context."""
    analysis = analyze_errors(errors, language)
    error_list = "\n".join(f"  {i+1}. {e[:120]}" for i, e in enumerate(errors[:15]))

    hints_section = ""
    if analysis["hints"]:
        hints_section = "\nCOMMON FIXES FOR THESE ERRORS:\n" + "\n".join(
            f"  - {h}" for h in analysis["hints"][:5]
        )

    # System 4: KB solution lookup for similar past errors
    kb_section = ""
    try:
        from skills.builder.engine.knowledge_base import get_knowledge_base
        kb = get_knowledge_base()
        if kb.available:
            error_text = " ".join(errors[:5])
            solutions = kb.retrieve_solutions_for_error(error_text, language=language, n_results=3)
            worked_solutions = [s for s in solutions if s.get("worked")]
            if worked_solutions:
                sol_lines = [
                    f"  - {s['solution'][:150]}" for s in worked_solutions[:3]
                ]
                kb_section = (
                    "\n\nKNOWN SOLUTIONS FOR SIMILAR ERRORS (from past builds):\n"
                    + "\n".join(sol_lines)
                )
    except Exception:
        pass  # KB is optional — fail silently

    related_section = ""
    if written_files:
        related = _find_related_files(file_path, code, written_files, language)
        if related:
            parts = []
            for rpath, rcontent in related.items():
                parts.append(f"--- {rpath} ---\n{rcontent}")
            related_section = (
                "\n\nRELATED PROJECT FILES (for reference — these define types/functions you import):\n"
                + "\n\n".join(parts)
            )

    return f"""Fix ALL {len(errors)} compiler errors in this {language} file.

FILE: {file_path}

ERRORS:
{error_list}

ERROR TYPE: {analysis['category']}
{analysis['llm_hint']}
{hints_section}
{kb_section}
{related_section}

CURRENT CODE:
{code[:12000]}

CRITICAL RULES:
  1. Fix ALL errors (not just first few)
  2. Don't add new errors
  3. Keep same logic, fix syntax/types only
  4. Output ONLY code, no explanation, no fences

OUTPUT: Complete corrected code for {file_path}"""


def repair_file(
    path: str,
    code: str,
    errors: list[str],
    language: str,
    coder_model: str,
    written_files: dict | None = None,
) -> str:
    """Intelligent file repair with error analysis and cross-file context."""
    prompt = smart_repair_prompt(path, code, errors, language, written_files=written_files)
    system = f"Expert {language} developer. Fix errors systematically. Output ONLY code."

    response = llm_call(
        model=coder_model,
        prompt=prompt,
        system=system,
        max_tokens=14336,
        temperature=0.05,
    )

    return strip_fences(response)


def patch_repair_file(
    file_path: str,
    current_code: str,
    errors: list[str],
    language: str,
    coder_model: str,
    written_files: dict | None = None,
) -> str:
    """
    Targeted patch: Only repair the broken function/class,
    not rewrite the entire file. Safer than full-regeneration
    for follow-up repair attempts.
    """
    error_summary = "\n".join(errors[:8])
    # Context: which files import this file?
    caller_ctx = ""
    if written_files:
        callers = [
            f"=== {p} ===\n{c[:1500]}"
            for p, c in written_files.items()
            if file_path.replace("/", ".").split(".")[0] in c and p != file_path
        ]
        caller_ctx = "\n\n".join(callers[:2])

    prompt = f"""Fix ONLY the broken parts of this {language} file. Do NOT rewrite the whole file.

FILE: {file_path}

ERRORS:
{error_summary}

CURRENT CODE:
{current_code}

{"FILES THAT IMPORT THIS FILE (for context):" + chr(10) + caller_ctx if caller_ctx else ""}

RULES:
1. Output the COMPLETE file, but change ONLY what is needed to fix the errors
2. Do NOT remove working functions, classes, or variables
3. Do NOT add new external dependencies
4. Preserve all existing logic that is NOT causing the errors
5. Output ONLY the fixed code, no fences, no explanation"""

    try:
        response = llm_call(
            model=coder_model,
            prompt=prompt,
            system=f"Expert {language} developer. Fix only the broken parts. Output ONLY code.",
            max_tokens=14336,
            temperature=0.05,
        )
        patched = strip_fences(response)
        # Safety: Patch must not drastically reduce meaningful content (Fix 6)
        original_lines = _meaningful_lines(current_code)
        fixed_lines = _meaningful_lines(patched)
        if original_lines == 0 or fixed_lines >= original_lines * 0.75:
            return patched
        else:
            blog.warning(
                f"Patch für {file_path} zu wenig Inhalt "
                f"({fixed_lines} vs {original_lines} meaningful lines), behalte Original"
            )
            return current_code
    except Exception as exc:
        blog.warning(f"Patch-Repair fehlgeschlagen: {exc}")
        return current_code
