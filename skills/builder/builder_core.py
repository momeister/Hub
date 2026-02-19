"""
skills/builder/builder_core.py - Thin facade over builder engine
=================================================================
This module keeps backward-compatible imports while the engine lives in
skills/builder/engine/*.
"""

from __future__ import annotations

from skills.builder.engine.cli import ask, main
from skills.builder.engine.context import (
    BASE_URL_V1,
    DEFAULT_CTX_TOKENS,
    IN_DOCKER,
    LLM_RETRIES,
    LLM_TIMEOUT,
    MAX_REPAIR_ATTEMPTS,
    MAX_SANDBOX_RETRIES,
    USE_INTERNET,
    blog,
    clean_json,
    llm_call,
    read_file,
    strip_fences,
    validate_blueprint,
    write_file,
)
from skills.builder.engine.agents import (
    make_workspace,
    run_agent_pipeline,
    agent_planner,
    agent_retriever,
    agent_coder,
    agent_executor,
    agent_critic,
    AGENT_SEQUENCE,
)
from skills.builder.engine.compile_checks import compile_check
from skills.builder.engine.deps import install_deps
from skills.builder.engine.pipeline import build_project, build_single_language_project
from skills.builder.engine.projects import edit_project, list_projects
from skills.builder.engine.utils import (
    detect_language_from_ext,
    generate_project_name,
    parse_multi_file_output,
)

__all__ = [
    "ask",
    "main",
    "build_project",
    "build_single_language_project",
    "list_projects",
    "edit_project",
    "compile_check",
    "install_deps",
    "generate_project_name",
    "detect_language_from_ext",
    "parse_multi_file_output",
    "blog",
    "llm_call",
    "BASE_URL_V1",
    "strip_fences",
    "clean_json",
    "write_file",
    "read_file",
    "validate_blueprint",
    "IN_DOCKER",
    "USE_INTERNET",
    "DEFAULT_CTX_TOKENS",
    "LLM_TIMEOUT",
    "LLM_RETRIES",
    "MAX_REPAIR_ATTEMPTS",
    "MAX_SANDBOX_RETRIES",
    "make_workspace",
    "run_agent_pipeline",
    "agent_planner",
    "agent_retriever",
    "agent_coder",
    "agent_executor",
    "agent_critic",
    "AGENT_SEQUENCE",
]

if __name__ == "__main__":
    main()
