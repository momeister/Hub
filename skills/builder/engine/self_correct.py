"""
skills/builder/engine/self_correct.py - Self-correction
=======================================================
"""

from __future__ import annotations

import os
import re

from skills.builder.engine.context import blog, llm_call, strip_fences, write_file
from skills.builder.engine.critic import critic_diagnose_failure


def self_correct(
    output_dir: str,
    language: str,
    coder_model: str,
    written_files: dict,
    error_output: str,
) -> dict:
    """One repair iteration: feed sandbox error back to coder."""
    blog.phase("self_correction", "Feeding sandbox error back to coder for repair", model=coder_model)

    diagnosis = critic_diagnose_failure(error_output, written_files, language, coder_model)
    problem_file = diagnosis.get("file")
    fix_context = ""

    if problem_file and diagnosis.get("root_cause"):
        fix_context = (
            f"\nDIAGNOSIS: {diagnosis['root_cause']}\n"
            f"FIX STRATEGY: {diagnosis.get('fix_strategy', '')}\n"
        )
        blog.info(f"Critic identified: {problem_file}")
    else:
        blog.info("Critic diagnosis unavailable, using regex heuristics")

        path_match = re.search(r"(?:^|\s)([\w./\\]+\.[\w]+)(?::\d+)", error_output, re.MULTILINE)
        if path_match:
            matched = path_match.group(1)
            for fpath in written_files:
                if fpath.endswith(matched) or os.path.basename(fpath) == matched:
                    problem_file = fpath
                    break

        if not problem_file:
            for fpath in written_files:
                escaped = re.escape(os.path.basename(fpath))
                if re.search(rf"\b{escaped}\b", error_output):
                    problem_file = fpath
                    break

        if not problem_file:
            for entry in ["main.py", "src/main.rs", "main.go", "index.js", "index.ts", "app.py"]:
                if entry in written_files:
                    problem_file = entry
                    break

    if not problem_file:
        blog.warning("Cannot identify which file to repair")
        return written_files

    code = written_files[problem_file]
    blog.repair(problem_file, 1, 1)

    prompt = f"""The following {language} project was generated but FAILED at runtime.

ERROR OUTPUT:
{error_output[:3000]}
{fix_context}
FILE TO FIX: {problem_file}

CURRENT CODE:
{code[:10000]}

Fix the runtime error. Output ONLY the corrected complete code for {problem_file}.
No explanation, no fences."""

    system = f"Expert {language} developer. Fix runtime errors. Output ONLY code."

    response = llm_call(
        model=coder_model,
        prompt=prompt,
        system=system,
        max_tokens=14336,
        temperature=0.05,
    )

    repaired = strip_fences(response)
    full_path = os.path.join(output_dir, problem_file)
    write_file(full_path, repaired)
    written_files[problem_file] = repaired

    blog.info(f"Repaired {problem_file} ({len(repaired)} chars)")

    return written_files
