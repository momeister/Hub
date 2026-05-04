"""
skills/builder/engine/knowledge_graph.py - Persistent Project Knowledge Graph
===============================================================================
System 3: Embedded graph store (Kuzu) that records build results, patterns,
errors, and fixes across projects. Queried before each new build to provide
context about similar past projects.

All operations are No-Ops if kuzu is not installed.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from skills.builder.engine.context import blog

# ---------------------------------------------------------------------------
# Graceful import — everything is a No-Op if kuzu is not installed
# ---------------------------------------------------------------------------
_kuzu_available = False
_kuzu = None

try:
    import kuzu as _kuzu
    _kuzu_available = True
except ImportError:
    pass

KG_DB_PATH = str(Path(os.path.expanduser("~/.builder_knowledge/kg")).resolve())

# Module-level singleton
_db_instance = None
_conn_instance = None


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------
def init_kg():
    """Initialize the Kuzu graph database. Idempotent. Returns (db, conn) or (None, None)."""
    global _db_instance, _conn_instance

    if not _kuzu_available:
        blog.info("Knowledge Graph: kuzu not installed, KG features disabled")
        return None, None

    if _db_instance is not None:
        return _db_instance, _conn_instance

    try:
        os.makedirs(KG_DB_PATH, exist_ok=True)
        db = _kuzu.Database(KG_DB_PATH)
        conn = _kuzu.Connection(db)

        # Create schema (idempotent — CREATE ... IF NOT EXISTS)
        _create_schema(conn)

        _db_instance = db
        _conn_instance = conn
        blog.info(f"Knowledge Graph initialized at {KG_DB_PATH}")
        return db, conn

    except Exception as exc:
        blog.warning(f"Knowledge Graph init failed: {exc}")
        return None, None


def _create_schema(conn) -> None:
    """Create node/relationship tables if they don't exist."""
    # Node tables
    conn.execute("""
        CREATE NODE TABLE IF NOT EXISTS Project (
            project_id STRING,
            goal_text STRING,
            language STRING,
            framework STRING,
            success BOOLEAN,
            build_duration_seconds INT64,
            file_count INT64,
            created_at STRING,
            PRIMARY KEY (project_id)
        )
    """)

    conn.execute("""
        CREATE NODE TABLE IF NOT EXISTS Pattern (
            pattern_id STRING,
            pattern_type STRING,
            description STRING,
            code_example STRING,
            language STRING,
            framework STRING,
            success_count INT64,
            failure_count INT64,
            PRIMARY KEY (pattern_id)
        )
    """)

    conn.execute("""
        CREATE NODE TABLE IF NOT EXISTS Error (
            error_id STRING,
            error_type STRING,
            error_message STRING,
            language STRING,
            file_context STRING,
            PRIMARY KEY (error_id)
        )
    """)

    conn.execute("""
        CREATE NODE TABLE IF NOT EXISTS Fix (
            fix_id STRING,
            fix_description STRING,
            fix_code_delta STRING,
            worked BOOLEAN,
            PRIMARY KEY (fix_id)
        )
    """)

    # Relationship tables
    conn.execute("""
        CREATE REL TABLE IF NOT EXISTS USED_PATTERN (
            FROM Project TO Pattern
        )
    """)

    conn.execute("""
        CREATE REL TABLE IF NOT EXISTS HAD_ERROR (
            FROM Project TO Error
        )
    """)

    conn.execute("""
        CREATE REL TABLE IF NOT EXISTS RESOLVED_BY (
            FROM Error TO Fix
        )
    """)

    conn.execute("""
        CREATE REL TABLE IF NOT EXISTS LEADS_TO_ERROR (
            FROM Pattern TO Error
        )
    """)


# ---------------------------------------------------------------------------
# Record Build Result
# ---------------------------------------------------------------------------
def record_build_result(
    goal: str,
    blueprint: dict,
    written_files: dict,
    errors_encountered: list[dict],
    repairs_applied: list[dict],
    success: bool,
    build_duration: float,
) -> None:
    """Store the result of a build in the Knowledge Graph."""
    if not _kuzu_available:
        return

    db, conn = init_kg()
    if conn is None:
        return

    try:
        project_id = str(uuid.uuid4())[:12]
        language = blueprint.get("language", "")
        framework = blueprint.get("framework", "")
        file_count = len(written_files)
        created_at = datetime.now(timezone.utc).isoformat()

        # Insert project node
        conn.execute(
            "CREATE (p:Project {"
            "  project_id: $pid, goal_text: $goal, language: $lang,"
            "  framework: $fw, success: $suc, build_duration_seconds: $dur,"
            "  file_count: $fc, created_at: $cat"
            "})",
            parameters={
                "pid": project_id,
                "goal": goal[:500],
                "lang": language,
                "fw": framework,
                "suc": success,
                "dur": int(build_duration),
                "fc": file_count,
                "cat": created_at,
            },
        )

        # Extract and store patterns
        _store_patterns(conn, project_id, blueprint, written_files, success)

        # Store errors
        _store_errors(conn, project_id, errors_encountered, language)

        # Store fixes
        _store_fixes(conn, errors_encountered, repairs_applied)

        blog.info(
            f"KG: Recorded build result (project={project_id}, "
            f"success={success}, errors={len(errors_encountered)}, "
            f"fixes={len(repairs_applied)})"
        )

    except Exception as exc:
        blog.warning(f"KG: Failed to record build result: {exc}")


def _store_patterns(
    conn, project_id: str, blueprint: dict, written_files: dict, success: bool,
) -> None:
    """Extract and store patterns from a build."""
    language = blueprint.get("language", "")
    framework = blueprint.get("framework", "")

    # Pattern 1: Framework combination
    combo_id = f"combo_{language}_{framework}".lower().replace(" ", "_")
    _upsert_pattern(
        conn, project_id,
        pattern_id=combo_id,
        pattern_type="framework_combo",
        description=f"{language}/{framework}",
        code_example="",
        language=language,
        framework=framework,
        success=success,
    )

    # Pattern 2: File layout
    file_paths = sorted(f.get("path", "") for f in blueprint.get("files", []))
    if file_paths:
        layout_desc = ", ".join(file_paths[:10])
        layout_id = f"layout_{language}_{hash(layout_desc) % 10000}"
        _upsert_pattern(
            conn, project_id,
            pattern_id=layout_id,
            pattern_type="file_layout",
            description=layout_desc[:500],
            code_example="",
            language=language,
            framework=framework,
            success=success,
        )

    # Pattern 3: API structure (for full-stack projects)
    api_endpoints = blueprint.get("api_endpoints", [])
    if api_endpoints:
        api_desc = "; ".join(
            f"{ep.get('method','?')} {ep.get('path','?')}"
            for ep in api_endpoints[:10]
        )
        api_id = f"api_{hash(api_desc) % 10000}"
        _upsert_pattern(
            conn, project_id,
            pattern_id=api_id,
            pattern_type="api_structure",
            description=api_desc[:500],
            code_example="",
            language=language,
            framework=framework,
            success=success,
        )


def _upsert_pattern(
    conn, project_id: str,
    pattern_id: str,
    pattern_type: str,
    description: str,
    code_example: str,
    language: str,
    framework: str,
    success: bool,
) -> None:
    """Insert or update a pattern and link it to the project."""
    try:
        # Check if pattern exists
        result = conn.execute(
            "MATCH (p:Pattern {pattern_id: $pid}) RETURN p.success_count, p.failure_count",
            parameters={"pid": pattern_id},
        )
        rows = result.get_as_df() if hasattr(result, 'get_as_df') else None

        if rows is not None and len(rows) > 0:
            # Update counts
            if success:
                conn.execute(
                    "MATCH (p:Pattern {pattern_id: $pid}) "
                    "SET p.success_count = p.success_count + 1",
                    parameters={"pid": pattern_id},
                )
            else:
                conn.execute(
                    "MATCH (p:Pattern {pattern_id: $pid}) "
                    "SET p.failure_count = p.failure_count + 1",
                    parameters={"pid": pattern_id},
                )
        else:
            # Create new pattern
            sc = 1 if success else 0
            fc = 0 if success else 1
            conn.execute(
                "CREATE (p:Pattern {"
                "  pattern_id: $pid, pattern_type: $pt, description: $desc,"
                "  code_example: $ce, language: $lang, framework: $fw,"
                "  success_count: $sc, failure_count: $fc"
                "})",
                parameters={
                    "pid": pattern_id, "pt": pattern_type, "desc": description,
                    "ce": code_example[:500], "lang": language, "fw": framework,
                    "sc": sc, "fc": fc,
                },
            )

        # Link project -> pattern
        conn.execute(
            "MATCH (proj:Project {project_id: $projid}), "
            "(pat:Pattern {pattern_id: $patid}) "
            "CREATE (proj)-[:USED_PATTERN]->(pat)",
            parameters={"projid": project_id, "patid": pattern_id},
        )
    except Exception as exc:
        blog.warning(f"KG: Pattern upsert failed for {pattern_id}: {exc}")


def _store_errors(
    conn, project_id: str, errors: list[dict], language: str,
) -> None:
    """Store error records and link them to the project."""
    for err in errors[:20]:  # Limit to 20 errors per build
        error_id = str(uuid.uuid4())[:12]
        error_type = err.get("type", "unknown")
        error_msg = str(err.get("message", err.get("error", "")))[:500]
        file_ctx = err.get("file", "")

        try:
            conn.execute(
                "CREATE (e:Error {"
                "  error_id: $eid, error_type: $et, error_message: $em,"
                "  language: $lang, file_context: $fc"
                "})",
                parameters={
                    "eid": error_id, "et": error_type, "em": error_msg,
                    "lang": language, "fc": file_ctx,
                },
            )

            conn.execute(
                "MATCH (p:Project {project_id: $pid}), "
                "(e:Error {error_id: $eid}) "
                "CREATE (p)-[:HAD_ERROR]->(e)",
                parameters={"pid": project_id, "eid": error_id},
            )
        except Exception as exc:
            blog.warning(f"KG: Error storage failed: {exc}")


def _store_fixes(conn, errors: list[dict], repairs: list[dict]) -> None:
    """Store fix records and link them to errors."""
    for repair in repairs[:20]:
        fix_id = str(uuid.uuid4())[:12]
        fix_desc = str(repair.get("description", ""))[:500]
        fix_delta = str(repair.get("code_delta", ""))[:500]
        worked = repair.get("worked", False)

        try:
            conn.execute(
                "CREATE (f:Fix {"
                "  fix_id: $fid, fix_description: $fd,"
                "  fix_code_delta: $fcd, worked: $w"
                "})",
                parameters={
                    "fid": fix_id, "fd": fix_desc,
                    "fcd": fix_delta, "w": worked,
                },
            )
        except Exception as exc:
            blog.warning(f"KG: Fix storage failed: {exc}")


# ---------------------------------------------------------------------------
# Query Similar Projects
# ---------------------------------------------------------------------------
def query_similar_projects(
    goal: str,
    language: str = "",
    framework: str = "",
) -> dict:
    """Query the KG for similar past projects and relevant patterns.

    Returns a dict with recommended patterns, known pitfalls, and a
    formatted summary for the LLM prompt.
    """
    if not _kuzu_available:
        return {"summary_for_prompt": ""}

    db, conn = init_kg()
    if conn is None:
        return {"summary_for_prompt": ""}

    result: dict[str, Any] = {
        "similar_projects_found": 0,
        "recommended_patterns": [],
        "known_pitfalls": [],
        "framework_success_rates": {},
        "summary_for_prompt": "",
    }

    try:
        # Find projects with matching language/framework
        query_params: dict[str, Any] = {}
        where_clauses = []

        if language:
            where_clauses.append("p.language = $lang")
            query_params["lang"] = language
        if framework:
            where_clauses.append("p.framework = $fw")
            query_params["fw"] = framework

        where_str = " AND ".join(where_clauses) if where_clauses else "TRUE"

        # Get similar projects
        projects_result = conn.execute(
            f"MATCH (p:Project) WHERE {where_str} "
            f"RETURN p.goal_text, p.language, p.framework, p.success, "
            f"p.build_duration_seconds, p.file_count "
            f"ORDER BY p.created_at DESC LIMIT 10",
            parameters=query_params,
        )

        projects_df = projects_result.get_as_df() if hasattr(projects_result, 'get_as_df') else None
        if projects_df is not None and len(projects_df) > 0:
            result["similar_projects_found"] = len(projects_df)

        # Get successful patterns
        patterns_result = conn.execute(
            "MATCH (pat:Pattern) "
            "WHERE pat.success_count > 0 "
            "RETURN pat.pattern_type, pat.description, "
            "pat.success_count, pat.failure_count, pat.language, pat.framework "
            "ORDER BY pat.success_count DESC LIMIT 10",
        )

        patterns_df = patterns_result.get_as_df() if hasattr(patterns_result, 'get_as_df') else None
        if patterns_df is not None and len(patterns_df) > 0:
            for _, row in patterns_df.iterrows():
                sc = row.get("pat.success_count", 0) or 0
                fc = row.get("pat.failure_count", 0) or 0
                total = sc + fc
                rate = sc / total if total > 0 else 0
                result["recommended_patterns"].append({
                    "pattern_type": row.get("pat.pattern_type", ""),
                    "description": row.get("pat.description", ""),
                    "success_rate": round(rate, 2),
                    "language": row.get("pat.language", ""),
                })

        # Get common errors for this technology
        if language:
            errors_result = conn.execute(
                "MATCH (e:Error) WHERE e.language = $lang "
                "RETURN e.error_type, e.error_message, e.file_context "
                "LIMIT 10",
                parameters={"lang": language},
            )
            errors_df = errors_result.get_as_df() if hasattr(errors_result, 'get_as_df') else None
            if errors_df is not None and len(errors_df) > 0:
                for _, row in errors_df.iterrows():
                    result["known_pitfalls"].append({
                        "error_type": row.get("e.error_type", ""),
                        "description": row.get("e.error_message", ""),
                        "language": language,
                    })

        # Get framework success rates
        rates_result = conn.execute(
            "MATCH (pat:Pattern {pattern_type: 'framework_combo'}) "
            "RETURN pat.description, pat.success_count, pat.failure_count",
        )
        rates_df = rates_result.get_as_df() if hasattr(rates_result, 'get_as_df') else None
        if rates_df is not None and len(rates_df) > 0:
            for _, row in rates_df.iterrows():
                desc = row.get("pat.description", "")
                sc = row.get("pat.success_count", 0) or 0
                fc = row.get("pat.failure_count", 0) or 0
                total = sc + fc
                if total > 0:
                    result["framework_success_rates"][desc] = round(sc / total, 2)

        # Build summary for prompt
        result["summary_for_prompt"] = _build_kg_summary(result)

    except Exception as exc:
        blog.warning(f"KG query failed: {exc}")

    return result


def _build_kg_summary(result: dict) -> str:
    """Build a formatted summary for the LLM prompt."""
    parts: list[str] = []

    if result["similar_projects_found"] > 0:
        parts.append(
            f"=== KNOWLEDGE GRAPH: {result['similar_projects_found']} similar past projects found ==="
        )

    if result["recommended_patterns"]:
        parts.append("\nSUCCESSFUL PATTERNS from past builds:")
        for pat in result["recommended_patterns"][:5]:
            parts.append(
                f"  [{pat['pattern_type']}] {pat['description']} "
                f"(success rate: {pat['success_rate']:.0%})"
            )

    if result["known_pitfalls"]:
        parts.append("\nKNOWN PITFALLS (avoid these):")
        for pit in result["known_pitfalls"][:5]:
            parts.append(f"  [{pit['error_type']}] {pit['description'][:100]}")

    if result["framework_success_rates"]:
        parts.append("\nFRAMEWORK SUCCESS RATES:")
        for fw, rate in sorted(result["framework_success_rates"].items(), key=lambda x: -x[1]):
            parts.append(f"  {fw}: {rate:.0%}")

    return "\n".join(parts) if parts else ""


# ---------------------------------------------------------------------------
# Anti-patterns query
# ---------------------------------------------------------------------------
def get_anti_patterns_for_language(language: str, framework: str = "") -> list[dict]:
    """Return known anti-patterns for a technology combination.

    Based on collected errors from past builds.
    Injected into the Planner prompt as warnings.
    """
    if not _kuzu_available:
        return []

    db, conn = init_kg()
    if conn is None:
        return []

    anti_patterns: list[dict] = []

    try:
        # Find patterns with high failure rates
        result = conn.execute(
            "MATCH (pat:Pattern)-[:LEADS_TO_ERROR]->(e:Error) "
            "WHERE pat.language = $lang "
            "RETURN pat.description, e.error_type, e.error_message, "
            "pat.failure_count "
            "ORDER BY pat.failure_count DESC LIMIT 10",
            parameters={"lang": language},
        )

        df = result.get_as_df() if hasattr(result, 'get_as_df') else None
        if df is not None and len(df) > 0:
            for _, row in df.iterrows():
                anti_patterns.append({
                    "pattern": row.get("pat.description", ""),
                    "error_type": row.get("e.error_type", ""),
                    "error_description": row.get("e.error_message", ""),
                    "failure_count": row.get("pat.failure_count", 0),
                })

        # Also find standalone common errors
        error_result = conn.execute(
            "MATCH (e:Error) WHERE e.language = $lang "
            "RETURN e.error_type, e.error_message, COUNT(*) AS freq "
            "ORDER BY freq DESC LIMIT 5",
            parameters={"lang": language},
        )

        error_df = error_result.get_as_df() if hasattr(error_result, 'get_as_df') else None
        if error_df is not None and len(error_df) > 0:
            for _, row in error_df.iterrows():
                anti_patterns.append({
                    "pattern": "common_error",
                    "error_type": row.get("e.error_type", ""),
                    "error_description": row.get("e.error_message", ""),
                    "failure_count": row.get("freq", 1),
                })

    except Exception as exc:
        blog.warning(f"KG: Anti-pattern query failed: {exc}")

    return anti_patterns


def format_anti_patterns_for_prompt(anti_patterns: list[dict]) -> str:
    """Format anti-patterns as context for the Planner prompt."""
    if not anti_patterns:
        return ""

    lines = ["=== KNOWN ANTI-PATTERNS (from past builds) ==="]
    for ap in anti_patterns[:8]:
        lines.append(
            f"  - [{ap.get('error_type', '?')}] {ap.get('error_description', '')[:120]} "
            f"(occurred {ap.get('failure_count', '?')} times)"
        )
    return "\n".join(lines)
