"""
skills/builder/engine/cli.py - CLI entrypoint
=============================================
Hardware-optimized for RTX 5070Ti (12GB VRAM) + 64GB RAM.
Models are loaded sequentially (one at a time) to fit in VRAM.
Context window: 262144 tokens (qwen3-coder-next supports 256k+).
"""

from __future__ import annotations

import os
import sys

from skills.builder.engine.context import blog
import skills.builder.engine.context as builder_context
from skills.builder.engine.pipeline import build_project
from skills.builder.engine.projects import edit_project, list_projects
from skills.builder.engine.utils import generate_project_name


def ask(prompt: str, default: str = "") -> str:
    try:
        response = input().strip()
        return response if response else default
    except EOFError:
        return default


def main() -> None:
    print("\n+================================================================+")
    print("|        AI BUILDER v3 - Agent Pipeline Architecture             |")
    print("|  Planner -> Retriever -> Coder -> Executor -> Critic           |")
    print("|  Sequential agents | 256k context | venv isolation             |")
    print("+================================================================+\n")

    print("--- MODUS ---")
    print("  [1] FAST       (deepseek-r1:8b / qwen2.5-coder:7b)       64k ctx")
    print("  [2] AVERAGE    (deepseek-r1:32b / qwen2.5-coder:14b)     64k ctx")
    print("  [3] GOD MODE   (gpt-oss:120b / qwen3-coder-next)        256k ctx  <- RECOMMENDED")
    print("  [4] UNCENSORED (huihui_ai/qwen3-coder-next-abliterated) 256k ctx")
    print("  [5] Custom")

    mode = ask(">> Modus (1-5): ", "3").strip()

    if mode == "5":
        print("\nCustom-Modus")
        manager_model = ask("Manager-Modell: ", "gpt-oss:120b")
        coder_model = ask("Coder-Modell: ", "qwen3-coder-next")
        ctx_input = ask("Context Tokens (leer = 256k): ", "262144")
        ctx_tokens = int(ctx_input) if ctx_input.isdigit() else 262144
    else:
        mode_configs = {
            "1": ("deepseek-r1:8b", "qwen2.5-coder:7b", 65536),
            "2": ("deepseek-r1:32b", "qwen2.5-coder:14b", 65536),
            "3": ("gpt-oss:120b", "qwen3-coder-next", 262144),
            "4": (
                "huihui_ai/qwen3-coder-next-abliterated",
                "huihui_ai/qwen3-coder-next-abliterated",
                262144,
            ),
        }
        manager_model, coder_model, ctx_tokens = mode_configs.get(mode, mode_configs["3"])

    print("\n--- INTERNET ZUGRIFF ---")
    use_internet_input = ask(">> Internet nutzen? (y/n): ", "n")

    builder_context.USE_INTERNET = use_internet_input.lower() == "y"
    blog.info(f"Internet: {'enabled' if builder_context.USE_INTERNET else 'disabled'}")

    print("\n--- OPERATION MODE ---")
    print("  [1] Neues Projekt")
    print("  [2] Bestehendes Projekt bearbeiten")

    op_mode = ask(">> Modus (1/2): ", "1").strip()

    if os.environ.get("TRIGGERED_BY") == "telegram":
        output_base = "/app/output"
    else:
        output_base = "./output"

    if op_mode == "2":
        project_name = ask(">> Project: ", "")
        if not project_name:
            print("Kein Projekt angegeben. Abbruch.")
            return

        project_dir = os.path.join(output_base, project_name)
        if not os.path.isdir(project_dir):
            print(f"Projekt nicht gefunden: {project_dir}")
            projects = list_projects(output_base)
            if projects:
                print("\nVerfuegbare Projekte:")
                for p in projects[:15]:
                    print(f"  {p['name']}  ({p['language']}, {p['files_count']} files)")
            return

        goal = ask(">> Was soll geaendert werden: ", "")
        if not goal:
            print("Kein Auftrag. Abbruch.")
            return

        print(f"\n{'=' * 60}")
        print("EDIT MODE")
        print(f"Manager : {manager_model}")
        print(f"Coder   : {coder_model}")
        print(f"Project : {project_dir}/")
        print(f"Goal    : {goal[:80]}")
        print(f"{'=' * 60}\n")

        try:
            edit_project(
                goal=goal,
                project_dir=project_dir,
                manager_model=manager_model,
                coder_model=coder_model,
                ctx_tokens=ctx_tokens,
            )
        except KeyboardInterrupt:
            print("\n\nEdit abgebrochen (Ctrl+C)")
            blog.complete(success=False, files_written=0, elapsed_sec=0)
            sys.exit(1)
        except Exception as exc:
            blog.error(f"EDIT FAILED: {exc}", severity="fatal")
            blog.complete(success=False, files_written=0, elapsed_sec=0)
            sys.exit(1)
        return

    print("\n--- PROJEKTAUFTRAG ---")
    print("Beschreibe was du bauen moechtest.")

    goal = ask("\n>> Projektauftrag: ", "")
    if not goal:
        print("Kein Auftrag. Abbruch.")
        return

    print("\n--- SCOPE ---")
    print("  [1] Auto  [2] Kompakt  [3] Voll")
    _scope = ask(">> Scope: ", "1")

    print("\n--- TESTS ---")
    print("  [1] Keine Tests  [2] Mit Unit-Tests")
    _tests = ask(">> Tests: ", "1")

    project_name = generate_project_name(goal)
    output_dir = os.path.join(output_base, project_name)

    if os.path.exists(output_dir):
        counter = 1
        while os.path.exists(f"{output_dir}_{counter}"):
            counter += 1
        output_dir = f"{output_dir}_{counter}"

    print(f"\n{'=' * 60}")
    print(f"Manager : {manager_model}")
    print(f"Coder   : {coder_model}")
    print(f"Context : {ctx_tokens:,} tokens")
    print(f"Agents  : Planner -> Retriever -> Coder -> Executor -> Critic")
    print(f"Output  : {output_dir}/")
    print(f"{'=' * 60}\n")

    try:
        build_project(
            goal=goal,
            manager_model=manager_model,
            coder_model=coder_model,
            output_dir=output_dir,
            ctx_tokens=ctx_tokens,
        )
    except KeyboardInterrupt:
        print("\n\nBuild abgebrochen (Ctrl+C)")
        blog.complete(success=False, files_written=0, elapsed_sec=0)
        sys.exit(1)
    except Exception as exc:
        blog.error(f"BUILD FAILED: {exc}", severity="fatal")
        blog.complete(success=False, files_written=0, elapsed_sec=0)
        sys.exit(1)
