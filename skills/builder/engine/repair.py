"""
skills/builder/engine/repair.py - Repair helpers
================================================
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from skills.builder.engine.context import blog, llm_call, strip_fences
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
