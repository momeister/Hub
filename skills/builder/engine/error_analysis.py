"""
skills/builder/engine/error_analysis.py - Compile error analysis
=================================================================
"""

from __future__ import annotations

import re

ERROR_PATTERNS = {
    "rust": [
        {"pattern": r"cannot find type `(\w+)` in", "hint": "Missing import: use crate::module::{Type};"},
        {"pattern": r"`\.await`.+(?:async|`async`)", "hint": "Async mismatch: remove .await OR make fn async"},
        {"pattern": r"mismatched types.*expected.*found", "hint": "Type cast needed: use 'as Type' or .into()"},
        {"pattern": r"no method named", "hint": "Missing trait import or wrong type"},
        {"pattern": r"the trait .* is not implemented", "hint": "Implement trait or use different type"},
        {"pattern": r"cannot borrow .* as mutable", "hint": "Borrow checker: use &mut, clone(), or refactor to avoid double borrow"},
        {"pattern": r"does not live long enough", "hint": "Lifetime issue: add lifetime annotation, clone(), or use owned type"},
        {"pattern": r"use of moved value", "hint": "Ownership: clone() the value before move, or use references"},
        {"pattern": r"cannot move out of", "hint": "Cannot move from borrowed context: use .clone() or ref pattern"},
        {"pattern": r"unresolved import", "hint": "Module path wrong: check mod.rs, use crate:: prefix, or add mod declaration"},
        {"pattern": r"the trait bound .* is not satisfied", "hint": "Missing trait impl: add #[derive()] or manual impl block"},
        {"pattern": r"cannot find macro", "hint": "Missing macro import: add #[macro_use] or use full path"},
        {"pattern": r"expected .*, found \(\)", "hint": "Function returns () but caller expects a value: add return expression"},
        {"pattern": r"unused variable", "hint": "Prefix with underscore: _varname"},
        {"pattern": r"value used .* after move", "hint": "Value moved earlier: clone() before first use or restructure"},
    ],
    "go": [
        {"pattern": r"undefined:", "hint": "Missing import or typo"},
        {"pattern": r"cannot use .* as type", "hint": "Type conversion: Type(value)"},
        {"pattern": r"declared and not used", "hint": "Remove unused variable (Go is strict)"},
    ],
    "typescript": [
        {"pattern": r"Cannot find module", "hint": "Import path: use ./ for relative, check npm install"},
        {"pattern": r"Property .* does not exist", "hint": "Check spelling or add to interface"},
        {"pattern": r"not assignable to type", "hint": "Type assertion needed: (value as Type)"},
    ],
    "python": [
        {"pattern": r"ModuleNotFoundError", "hint": "Add to requirements.txt or fix import path"},
        {"pattern": r"NameError:", "hint": "Variable not defined or typo"},
        {"pattern": r"TypeError:.*argument", "hint": "Wrong number/type of arguments"},
    ],
}


def analyze_errors(errors: list[str], language: str) -> dict:
    """Analyze compile errors and categorize them."""
    patterns_for_lang = ERROR_PATTERNS.get(language, [])
    found = []
    categories = set()

    for error in errors:
        for pat in patterns_for_lang:
            if re.search(pat["pattern"], error, re.IGNORECASE):
                found.append(pat["hint"])
                if "import" in pat["hint"].lower() or "module" in pat["hint"].lower():
                    categories.add("import_error")
                elif "async" in pat["hint"].lower():
                    categories.add("async_error")
                elif "type" in pat["hint"].lower() or "cast" in pat["hint"].lower():
                    categories.add("type_error")
                else:
                    categories.add("logic_error")

    if "async_error" in categories:
        llm_hint = (
            "ASYNC/SYNC MISMATCH detected.\n"
            "Fix: Either make function async OR use sync alternatives."
        )
    elif "import_error" in categories:
        llm_hint = "IMPORT/MODULE errors. Check all imports at file top."
    elif "type_error" in categories:
        llm_hint = "TYPE MISMATCH. Add explicit casts or conversions."
    else:
        llm_hint = "Multiple error types. Review each carefully."

    return {
        "category": list(categories)[0] if categories else "unknown",
        "hints": found,
        "llm_hint": llm_hint,
    }
