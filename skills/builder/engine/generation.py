"""
skills/builder/engine/generation.py - Skeleton-fill-in generation
=================================================================
"""

from __future__ import annotations

import os

from skills.builder.engine.compile_checks import compile_check
from skills.builder.engine.context import blog, MAX_REPAIR_ATTEMPTS
from skills.builder.engine.formatters import format_code
from skills.builder.engine.manifest import validate_manifest
from skills.builder.engine.repair import repair_file
from skills.builder.engine.skeletons import generate_all_skeletons, fill_in_file
from skills.builder.engine.context import write_file
from core.llm_client import call_builder_session
from core.utils import estimate_tokens, strip_code_fences


def _compress_session(
    session_messages: list[dict],
    language: str,
    written_files: dict,
) -> list[dict]:
    """
    Baut eine neue Session auf mit geschriebenem Code als Kontext
    statt dem vollständigen Gesprächsverlauf.
    Bewahrt den System-Prompt, verwirft den alten Chat-Verlauf,
    fügt alle bereits geschriebenen Dateien als eine kompakte
    user-Message ein.
    """
    system_msg = session_messages[0]  # System-Prompt behalten
    code_summary = "\n\n".join(
        f"=== {path} (already written) ===\n{code}"
        for path, code in written_files.items()
    )
    return [
        system_msg,
        {"role": "user", "content":
            f"Context from previous session — these files are already written:\n\n"
            f"{code_summary}\n\nContinue with the next file."},
        {"role": "assistant", "content":
            "Understood. I have the context of all written files. "
            "Ready for the next file."},
    ]


def _build_fill_in_prompt(
    file_spec: dict,
    all_skeletons: dict,
    written_files: dict,
    blueprint: dict,
) -> str:
    """Build the fill-in prompt for a file (without LLM call)."""
    path = file_spec["path"]
    purpose = file_spec.get("purpose", "")
    language = blueprint["language"]

    ctx_parts = []

    pending = {p: c for p, c in all_skeletons.items() if p not in written_files}
    if pending:
        ctx_parts.append("=== PENDING FILES (skeleton only, not yet implemented) ===")
        for sk_path, sk_code in pending.items():
            ctx_parts.append(f"\n=== {sk_path} ===\n{sk_code}")

    deps = blueprint.get("dependencies", {})
    if deps.get("content"):
        ctx_parts.append(f"\n\n=== DEPENDENCIES ({deps.get('type', '')}) ===\n{deps['content']}")

    context = "\n".join(ctx_parts)

    return f"""Implement the file: {path}

Purpose: {purpose}

{context}

OUTPUT:
Complete implementation for {path}. Output ONLY the code, no fences, no explanation.

RULES:
  1. Use EXACT imports from skeletons
  2. Full implementation, no placeholders
  3. Follow {language} idioms
  4. Handle errors properly
  5. Add brief comments for complex logic"""


def skeleton_fill_in_generate(
    goal: str,
    blueprint: dict,
    coder_model: str,
    output_dir: str,
    ctx_tokens: int,
) -> dict:
    """Generate all files using skeleton-fill-in strategy."""
    language = blueprint["language"]
    files = blueprint["files"]
    dep_order = blueprint.get("dependency_order", [f["path"] for f in files])

    files_total = len(files)
    blog.phase("fill_in", f"Generating {files_total} files (skeleton-fill-in)", model=coder_model)

    skeletons = generate_all_skeletons(blueprint, coder_model)

    if not skeletons:
        blog.warning("Zero skeletons parsed from LLM output, retrying generation...")
        skeletons = generate_all_skeletons(blueprint, coder_model)

    if not skeletons:
        blog.error("Skeleton generation failed after 2 attempts", severity="fatal")
        raise RuntimeError("Could not generate any file skeletons from LLM output")

    expected = len(files)
    actual = len(skeletons)
    if actual < expected:
        blog.warning(f"Only {actual}/{expected} skeletons generated - some files may be missing")

    for path, skeleton in skeletons.items():
        full_path = os.path.join(output_dir, path)
        write_file(full_path, skeleton)

    blog.phase("manifest_validation", "Validating manifest and installing dependencies")
    validate_manifest(output_dir, language, coder_model, skeletons)

    # NOTE: We intentionally skip compile-checking skeletons here.
    # Skeletons are empty stubs (pass/...) so mypy will always flag
    # "Missing return statement [empty-body]". Those are expected and
    # would only trigger pointless repair loops.

    written_files: dict[str, str] = {}
    file_index = 0

    # Persistent builder session
    session_messages = [
        {"role": "system", "content": f"Expert {language} developer. "
         "Output ONLY code, no markdown fences, no explanation."}
    ]

    for file_path in dep_order:
        file_spec = next((f for f in files if f["path"] == file_path), None)
        if not file_spec:
            blog.warning(f"File {file_path} in dep order but not in plan, skipping")
            continue

        file_index += 1
        blog.file_start(file_path, file_index, files_total)

        # Token-Budget prüfen vor jedem Call
        estimated = estimate_tokens(str(session_messages))
        max_session_tokens = int(ctx_tokens * 0.75)  # 75% für Input, 25% für Output
        if estimated > max_session_tokens:
            blog.warning("Session context near limit — compressing history")
            session_messages = _compress_session(session_messages, language, written_files)

        fill_in_prompt = _build_fill_in_prompt(file_spec, skeletons, written_files, blueprint)
        session_messages.append({"role": "user", "content": fill_in_prompt})

        raw_code, session_messages = call_builder_session(
            model=coder_model,
            session_messages=session_messages,
            max_tokens=14336,
            temperature=0.1,
        )
        code = strip_code_fences(raw_code)

        full_path = os.path.join(output_dir, file_path)
        write_file(full_path, code)
        written_files[file_path] = code

        blog.file_done(file_path, len(code))

        success, all_errors = compile_check(output_dir, language)

        # Filter: only act on errors in the file we just wrote.
        # Other files are still skeletons (empty stubs) so they will
        # always have errors — we must not repair the current file
        # because of skeleton errors in OTHER files.
        file_basename = os.path.basename(file_path)
        errors = [
            e for e in all_errors
            if file_basename in e or file_path in e
        ]

        if not errors:
            # No errors in THIS file — skip repair even if skeletons elsewhere fail
            continue

        if errors:
            blog.warning(f"Compile errors after {file_path}: {len(errors)}")
            for err in errors[:3]:
                blog.error(err, file=file_path, severity="compile")

            max_repairs = MAX_REPAIR_ATTEMPTS.get(language, 3)
            repaired_ok = False

            for repair_attempt in range(1, max_repairs + 1):
                blog.repair(file_path, repair_attempt, max_repairs)

                repaired = repair_file(
                    file_path,
                    code,
                    errors,
                    language,
                    coder_model,
                    written_files=written_files,
                )

                write_file(full_path, repaired)
                written_files[file_path] = repaired
                code = repaired
                format_code(output_dir, language)

                success2, all_errors2 = compile_check(output_dir, language)
                # Filter to current file only
                errors2 = [
                    e for e in all_errors2
                    if file_basename in e or file_path in e
                ]
                if not errors2:
                    blog.verify(True, "compile", f"{file_path} repaired")
                    repaired_ok = True
                    break
                errors = errors2
                if repair_attempt == max_repairs:
                    blog.warning(f"Could not fix {file_path} after {max_repairs} repair attempts")

            if not repaired_ok:
                blog.phase("nuclear_regen", f"Fresh regeneration for {file_path}")
                file_spec_extra = dict(file_spec)
                error_summary = "\n".join(errors[:10])
                file_spec_extra["purpose"] = (
                    f"{file_spec.get('purpose', '')} "
                    f"[PREVIOUS ATTEMPT HAD ERRORS - avoid these: {error_summary[:500]}]"
                )
                fresh_code = fill_in_file(
                    file_spec_extra,
                    skeletons,
                    written_files,
                    blueprint,
                    coder_model,
                    ctx_tokens,
                )
                write_file(full_path, fresh_code)
                written_files[file_path] = fresh_code
                code = fresh_code
                format_code(output_dir, language)

                success3, all_errors3 = compile_check(output_dir, language)
                # Filter to current file only
                errors3 = [
                    e for e in all_errors3
                    if file_basename in e or file_path in e
                ]
                if not errors3:
                    blog.verify(True, "compile", f"{file_path} fixed via nuclear regen")
                else:
                    blog.error(
                        f"Nuclear regen also failed for {file_path} ({len(errors3)} errors) - continuing",
                        file=file_path,
                        severity="warning",
                    )

    return written_files
