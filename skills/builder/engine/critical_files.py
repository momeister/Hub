"""
skills/builder/engine/critical_files.py - Critical file validation
==================================================================
"""

from __future__ import annotations

import re

from skills.builder.engine.context import blog

CRITICAL_FILES = {
    "rust": {
        "required": ["Cargo.toml"],
        "entry_point_options": ["src/main.rs", "src/lib.rs"],
        "description": "Rust requires Cargo.toml + entry point (src/main.rs OR src/lib.rs)",
    },
    "python": {
        "required": [],
        "entry_point_options": ["main.py", "__main__.py", "src/main.py", "app.py"],
        "description": "Python requires an entry point (main.py, app.py, etc.)",
    },
    "javascript": {
        "required": ["package.json"],
        "entry_point_options": ["index.js", "main.js", "src/index.js", "app.js"],
        "description": "JavaScript requires package.json + entry point",
    },
    "typescript": {
        "required": ["package.json"],
        "entry_point_options": ["index.ts", "main.ts", "src/index.ts", "app.ts"],
        "description": "TypeScript requires package.json + entry point",
    },
    "go": {
        "required": ["go.mod"],
        "entry_point_options": ["main.go", "cmd/main.go"],
        "description": "Go requires go.mod + main.go",
    },
    "java": {
        "required_options": [["pom.xml"], ["build.gradle"]],
        "entry_point_pattern": r".*Main\.java$",
        "description": "Java requires build tool (pom.xml OR build.gradle) + Main.java",
    },
    "csharp": {
        "required_pattern": r".*\.csproj$",
        "entry_point_options": ["Program.cs", "Main.cs"],
        "description": "C# requires .csproj file + Program.cs",
    },
    "html": {
        "required": ["index.html"],
        "description": "HTML requires index.html",
    },
}


def validate_critical_files(plan: dict, language: str) -> dict:
    """Validate that plan includes all critical files for the language."""
    files = plan.get("files", [])
    file_paths = {f["path"] for f in files}
    missing = []
    warnings = []

    lang_config = CRITICAL_FILES.get(language.lower())
    if not lang_config:
        warnings.append(f"Unknown language '{language}' - can't validate critical files")
        return {"valid": True, "missing": [], "warnings": warnings}

    for required in lang_config.get("required", []):
        if required not in file_paths:
            missing.append(required)

    if "required_options" in lang_config:
        for option_group in lang_config["required_options"]:
            if not any(opt in file_paths for opt in option_group):
                missing.append(f"One of: {', '.join(option_group)}")

    if "entry_point_options" in lang_config:
        entry_points = lang_config["entry_point_options"]
        if not any(ep in file_paths for ep in entry_points):
            missing.append(f"Entry point (one of: {', '.join(entry_points)})")

    if "required_pattern" in lang_config:
        pattern = lang_config["required_pattern"]
        if not any(re.match(pattern, fp) for fp in file_paths):
            missing.append(f"File matching pattern: {pattern}")

    if "entry_point_pattern" in lang_config:
        pattern = lang_config["entry_point_pattern"]
        if not any(re.match(pattern, fp) for fp in file_paths):
            missing.append(f"Entry point matching: {pattern}")

    return {
        "valid": len(missing) == 0,
        "missing": missing,
        "warnings": warnings,
        "description": lang_config.get("description", ""),
    }


def ensure_critical_files(plan: dict, language: str) -> dict:
    """Ensure all critical files are in the plan. Adds missing ones."""
    validation = validate_critical_files(plan, language)

    if validation["valid"]:
        blog.info("All critical files present")
        return plan

    missing = validation["missing"]
    blog.warning(f"Missing critical files: {', '.join(missing)}")

    files = plan.get("files", [])
    lang_config = CRITICAL_FILES.get(language.lower(), {})

    for req in lang_config.get("required", []):
        if req not in {f["path"] for f in files}:
            purpose = "Build manifest"
            if "toml" in req.lower():
                purpose = "Cargo build configuration"
            elif "json" in req.lower():
                purpose = "Package dependencies and scripts"
            elif "mod" in req.lower():
                purpose = "Go module definition"
            files.append({"path": req, "purpose": purpose, "exports": [], "imports": []})
            blog.info(f"Added critical file: {req}")

    entry_options = lang_config.get("entry_point_options", [])
    if entry_options and not any(ep in {f["path"] for f in files} for ep in entry_options):
        entry_point = entry_options[0]
        files.append({
            "path": entry_point,
            "purpose": "Application entry point",
            "exports": ["main"] if language != "rust" else [],
            "imports": [],
        })
        blog.info(f"Added entry point: {entry_point}")

    plan["files"] = files

    dep_order = plan.get("dependency_order", [])
    for f in files:
        if f["path"] not in dep_order:
            if any(ext in f["path"] for ext in [".toml", ".json", ".mod", ".xml", ".gradle", ".csproj"]):
                dep_order.insert(0, f["path"])
            else:
                dep_order.append(f["path"])
    plan["dependency_order"] = dep_order

    return plan
