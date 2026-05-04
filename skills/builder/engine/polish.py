"""
skills/builder/engine/polish.py - Human polish phase
====================================================
"""

from __future__ import annotations

import json
import os

from skills.builder.engine.context import blog, llm_call, clean_json, strip_fences, write_file
from skills.builder.engine.compile_checks import compile_check


def human_polish_phase(
    goal: str,
    blueprint: dict,
    files: dict,
    coder_model: str,
    manager_model: str,
    output_dir: str,
    language: str,
) -> dict:
    """Suggest and apply small UX improvements after generation."""
    blog.phase("human_polish", "Thinking about what a human would also expect", model=manager_model)

    file_summaries = []
    for fpath, content in sorted(files.items()):
        lines = content.splitlines()[:60]
        preview = "\n".join(lines)
        file_summaries.append(f"=== {fpath} ({len(content)} chars) ===\n{preview}")

    project_context = "\n\n".join(file_summaries)

    if len(project_context) > 12000:
        project_context = project_context[:12000] + "\n\n... (truncated)"

    suggest_prompt = f"""You are a senior UX reviewer. A {language} project was just built for this goal:

GOAL: "{goal}"

The project is COMPLETE and WORKING. Your job is to suggest 1-3 small, high-impact
improvements that a human user would expect but weren't explicitly requested.

CURRENT PROJECT FILES:
{project_context}

Think about:
  - First-time user experience (start screens, instructions, help text)
  - Error handling the user will encounter (friendly messages, not crashes)
  - Visual polish (colors, formatting, layout)
  - Missing standard features (pause in games, exit option, keyboard shortcuts)
  - Accessibility (clear labels, tooltips, contrast)
  - For full-stack projects (frontend + backend), also consider:
    * Missing error handling in frontend API calls (try-catch with user-friendly messages)
    * Missing loading indicators while waiting for API responses
    * Missing CORS configuration in backend (causes silent failures)
    * Inconsistent API endpoint URLs between frontend and backend
    * Missing response validation before rendering data (prevents blank screens)
    * Missing console.log for API debugging during development

RULES:
  1. Only suggest things that can be done by MODIFYING existing files
  2. Do NOT suggest adding new dependencies or libraries
  3. Do NOT suggest major architectural changes
  4. Each suggestion must be implementable in under 50 lines of code changes
  5. Focus on the #1 most impactful improvement first
  6. Be specific: name the exact file and what to change

Output ONLY this JSON:
""" + """{
  "suggestions": [
    {
      "file": "<file path to modify>",
      "what": "<1-sentence description of the improvement>",
      "why": "<why a human user would expect this>",
      "priority": <1-3, 1=highest>
    }
  ]
}

If the project already looks polished, return {"suggestions": []}.
Output ONLY JSON."""

    try:
        suggest_response = llm_call(
            model=manager_model,
            prompt=suggest_prompt,
            system="Senior UX reviewer. Output only valid JSON. Be practical, not theoretical.",
            max_tokens=2048,
            temperature=0.2,
        )

        raw = json.loads(clean_json(suggest_response))
        if not isinstance(raw, dict):
            blog.warning("Polish phase returned non-dict, skipping")
            return files

        suggestions = raw.get("suggestions", [])
        if not suggestions:
            blog.info("Polish phase: project already looks good, no suggestions")
            return files

        suggestions.sort(key=lambda s: s.get("priority", 99))

        for s in suggestions[:3]:
            blog.polish_suggestion(
                file=s.get("file", "?"),
                what=s.get("what", "?"),
                why=s.get("why", ""),
                priority=s.get("priority", 1),
            )

    except Exception as exc:
        blog.warning(f"Polish suggestion phase failed ({exc}), skipping")
        return files

    applied_count = 0
    for suggestion in suggestions[:2]:
        target_file = suggestion.get("file", "")
        what = suggestion.get("what", "")
        why = suggestion.get("why", "")

        if not target_file or target_file not in files:
            for fpath in files:
                if os.path.basename(fpath) == os.path.basename(target_file):
                    target_file = fpath
                    break
            else:
                blog.warning(f"Polish: file '{target_file}' not in project, skipping")
                continue

        current_code = files[target_file]
        blog.info(f"Polish: applying '{what}' to {target_file}")

        apply_prompt = f"""Apply this improvement to the file below.

IMPROVEMENT: {what}
REASON: {why}

FILE: {target_file}

CURRENT CODE:
{current_code[:10000]}

RULES:
  1. Output the COMPLETE file with the improvement applied
  2. Do NOT break existing functionality
  3. Do NOT add new imports for external packages
  4. Keep changes minimal and focused
  5. Output ONLY code, no fences, no explanation"""

        try:
            apply_response = llm_call(
                model=coder_model,
                prompt=apply_prompt,
                system=f"Expert {language} developer. Apply the improvement cleanly. Output ONLY code.",
                max_tokens=14336,
                temperature=0.1,
            )

            polished_code = strip_fences(apply_response)

            if len(polished_code) < len(current_code) * 0.5:
                blog.warning(
                    f"Polish: code shrank too much ({len(polished_code)} vs {len(current_code)}), skipping"
                )
                continue

            full_path = os.path.join(output_dir, target_file)
            write_file(full_path, polished_code)
            files[target_file] = polished_code
            applied_count += 1

            blog.polish_applied(file=target_file, what=what, chars=len(polished_code))

        except Exception as exc:
            blog.warning(f"Polish: failed to apply '{what}' ({exc}), skipping")
            continue

    if applied_count > 0:
        blog.verify(True, "human_polish", f"Applied {applied_count} UX improvement(s)")

        success, errors = compile_check(output_dir, language)
        if not success:
            blog.warning(f"Polish introduced {len(errors)} compile errors, reverting...")
            for err in errors[:5]:
                blog.error(err, severity="polish")
    else:
        blog.info("Polish phase: no improvements applied")

    return files
