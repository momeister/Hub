"""
core/skill_registry.py — Skill discovery and definitions
=========================================================
Loads skill.json files and exposes tool definitions.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from core.config import SKILLS_DIR

log = logging.getLogger("ai-hub.skill_registry")


def iter_skill_dirs() -> list[Path]:
    """Return skill directories that contain a skill.json."""
    if not SKILLS_DIR.exists():
        return []
    return [
        d for d in sorted(SKILLS_DIR.iterdir())
        if d.is_dir() and (d / "skill.json").exists()
    ]


def load_skill_definitions() -> list[dict]:
    """Load all skill.json files and convert to tool definitions."""
    tools = []
    for skill_dir in iter_skill_dirs():
        skill_json = skill_dir / "skill.json"
        try:
            with open(skill_json, encoding="utf-8") as f:
                skill_def = json.load(f)
            tools.append({
                "type": "function",
                "function": {
                    "name": skill_def["name"],
                    "description": skill_def["description"],
                    "parameters": skill_def.get(
                        "parameters",
                        {
                            "type": "object",
                            "properties": {
                                "request": {
                                    "type": "string",
                                    "description": "The full user request",
                                }
                            },
                            "required": ["request"],
                        },
                    ),
                },
            })
            log.debug("Skill loaded: %s", skill_def["name"])
        except Exception as exc:
            log.warning("Skill definition invalid (%s): %s", skill_dir.name, exc)
    return tools


def get_available_skills() -> list[str]:
    return [d.name for d in iter_skill_dirs()]


def get_skill_descriptions() -> list[dict]:
    result = []
    for skill_dir in iter_skill_dirs():
        skill_json = skill_dir / "skill.json"
        try:
            with open(skill_json, encoding="utf-8") as f:
                skill_def = json.load(f)
            result.append({
                "name": skill_def.get("name", skill_dir.name),
                "description": skill_def.get("description", "No description"),
            })
        except Exception:
            continue
    return result
