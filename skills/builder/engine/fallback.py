"""
skills/builder/engine/fallback.py - Fallback blueprints
=======================================================
"""

from __future__ import annotations

from skills.builder.engine.utils import generate_project_name
from skills.builder.engine.context import blog

FALLBACK_FILES = {
    "python": [
        {"path": "requirements.txt", "purpose": "Dependencies", "exports": [], "imports": [],
         "estimated_lines": 5, "critical": True},
        {"path": "main.py", "purpose": "Entry point", "exports": [], "imports": [],
         "estimated_lines": 50, "critical": True},
    ],
    "rust": [
        {"path": "Cargo.toml", "purpose": "Build configuration", "exports": [], "imports": [],
         "estimated_lines": 15, "critical": True},
        {"path": "src/main.rs", "purpose": "Entry point", "exports": [], "imports": [],
         "estimated_lines": 50, "critical": True},
    ],
    "go": [
        {"path": "go.mod", "purpose": "Module definition", "exports": [], "imports": [],
         "estimated_lines": 5, "critical": True},
        {"path": "main.go", "purpose": "Entry point", "exports": [], "imports": [],
         "estimated_lines": 50, "critical": True},
    ],
    "typescript": [
        {"path": "package.json", "purpose": "Dependencies and scripts", "exports": [], "imports": [],
         "estimated_lines": 20, "critical": True},
        {"path": "tsconfig.json", "purpose": "TypeScript config", "exports": [], "imports": [],
         "estimated_lines": 10, "critical": True},
        {"path": "src/index.ts", "purpose": "Entry point", "exports": [], "imports": [],
         "estimated_lines": 50, "critical": True},
    ],
    "javascript": [
        {"path": "package.json", "purpose": "Dependencies and scripts", "exports": [], "imports": [],
         "estimated_lines": 15, "critical": True},
        {"path": "index.js", "purpose": "Entry point", "exports": [], "imports": [],
         "estimated_lines": 50, "critical": True},
    ],
    "html": [
        {"path": "index.html", "purpose": "Main page", "exports": [], "imports": [],
         "estimated_lines": 40, "critical": True},
        {"path": "style.css", "purpose": "Styles", "exports": [], "imports": [],
         "estimated_lines": 30, "critical": False},
        {"path": "script.js", "purpose": "Client logic", "exports": [], "imports": [],
         "estimated_lines": 50, "critical": False},
    ],
}


def detect_fallback_language(goal: str) -> str:
    """Detect best language from goal text when architect fails."""
    goal_lower = goal.lower()
    if any(kw in goal_lower for kw in ["web", "frontend", "react", "vue", "browser", "html"]):
        return "html" if any(kw in goal_lower for kw in ["browser", "html", "game"]) else "typescript"
    if any(kw in goal_lower for kw in ["performance", "systems", "concurrent", "rust"]):
        return "rust"
    if any(kw in goal_lower for kw in ["api", "backend", "server", "go ", "golang"]):
        return "go"
    if any(kw in goal_lower for kw in ["node", "npm", "javascript", "js "]):
        return "javascript"
    return "python"


def make_fallback_blueprint(goal: str, language: str) -> dict:
    """Create a complete fallback blueprint for a given language."""
    # Warn if the goal looks like a full-stack project
    goal_lower = goal.lower()
    fullstack_keywords = [
        "backend", "frontend", "api", "server", "client",
        "full-stack", "fullstack", "full stack", "rest", "endpoint",
        "database", "web app", "web application",
    ]
    if any(kw in goal_lower for kw in fullstack_keywords):
        blog.warning(
            "WARNUNG: Blueprint-Generierung fehlgeschlagen für ein Full-Stack-Projekt. "
            "Der Fallback erstellt nur ein Single-Language-Projekt. "
            "Bitte den Build mit einem präziseren Prompt neu starten."
        )

    files = FALLBACK_FILES.get(language, FALLBACK_FILES["python"])
    return {
        "project_name": generate_project_name(goal),
        "language": language,
        "framework": "",
        "why": "Fallback selection (architect output could not be parsed)",
        "is_multi_language": False,
        "files": files,
        "dependency_order": [f["path"] for f in files],
        "dependencies": {"type": "none", "content": ""},
        "architecture_decisions": [],
        "safe_stack_violations": [],
        "estimated_complexity": "simple",
        "subprojects": [],
    }
