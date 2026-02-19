"""
core/safe_stack.py — Safe Stack Policy Enforcement
====================================================
Defines "blessed" dependency sets per language.
Validates architect blueprints against known-safe libraries.
Non-standard deps are flagged (not blocked) unless explicitly justified.
"""

# ============================================================================
# BLESSED STACKS (well-known, maintained, production-ready)
# ============================================================================

SAFE_STACKS = {
    "rust": {
        "blessed": {
            # Async runtime
            "tokio", "async-std",
            # Serialization
            "serde", "serde_json", "serde_yaml", "toml",
            # CLI
            "clap", "structopt",
            # Web frameworks
            "axum", "actix-web", "warp", "rocket", "hyper",
            # HTTP client
            "reqwest", "hyper",
            # Error handling
            "anyhow", "thiserror", "eyre",
            # Logging
            "tracing", "log", "env_logger", "tracing-subscriber",
            # Database
            "sqlx", "diesel", "sea-orm",
            # Utils
            "rand", "chrono", "uuid", "regex", "once_cell", "lazy_static",
            "crossterm", "ratatui", "colored",
            # Games
            "macroquad", "ggez", "bevy",
        },
    },
    "python": {
        "blessed": {
            # Web frameworks
            "fastapi", "flask", "django", "starlette", "uvicorn",
            # HTTP
            "requests", "httpx", "aiohttp",
            # Data validation
            "pydantic",
            # Database
            "sqlalchemy", "alembic", "peewee", "tortoise-orm",
            # Testing
            "pytest", "unittest",
            # CLI
            "click", "typer", "argparse",
            # Data science
            "numpy", "pandas", "matplotlib", "scipy", "scikit-learn",
            # Image
            "pillow",
            # Utils
            "rich", "colorama", "python-dotenv", "jinja2",
            # Async
            "asyncio", "celery",
            # Games
            "pygame",
        },
    },
    "go": {
        "blessed": {
            # Web
            "gin-gonic/gin", "gorilla/mux", "go-chi/chi", "labstack/echo",
            "fiber", "net/http",
            # Database
            "gorm.io/gorm", "jmoiron/sqlx", "jackc/pgx",
            # CLI
            "cobra", "urfave/cli",
            # Config
            "viper", "godotenv",
            # Logging
            "zerolog", "zap", "logrus",
            # Utils
            "uuid", "validator",
        },
    },
    "typescript": {
        "blessed": {
            # Frameworks
            "react", "next", "vue", "nuxt", "svelte", "angular",
            "express", "fastify", "nest",
            # State
            "zustand", "redux", "jotai", "valtio",
            # HTTP
            "axios", "fetch",
            # Database
            "prisma", "typeorm", "drizzle-orm", "mongoose",
            # Validation
            "zod", "yup", "joi",
            # Testing
            "jest", "vitest", "playwright", "cypress",
            # Utils
            "lodash", "dayjs", "date-fns",
            # Build
            "vite", "webpack", "esbuild", "tsup",
        },
    },
    "javascript": {
        "blessed": {
            # Frameworks
            "react", "vue", "svelte", "express", "fastify", "koa",
            # HTTP
            "axios", "node-fetch",
            # Database
            "mongoose", "knex", "sequelize",
            # Testing
            "jest", "mocha", "chai",
            # Utils
            "lodash", "moment", "dayjs",
        },
    },
    "java": {
        "blessed": {
            # Frameworks
            "spring-boot", "spring-web", "spring-data",
            "quarkus", "micronaut",
            # Build
            "maven", "gradle",
            # Utils
            "lombok", "guava", "jackson", "slf4j", "logback",
            # Testing
            "junit", "mockito",
        },
    },
    "csharp": {
        "blessed": {
            # Frameworks
            "aspnetcore", "entityframework",
            # Testing
            "xunit", "nunit", "moq",
            # Utils
            "newtonsoft.json", "serilog", "automapper",
        },
    },
}


def validate_blueprint(blueprint: dict) -> list[str]:
    """
    Validate a blueprint's dependencies against the safe stack.
    Returns a list of warning strings for any non-blessed dependencies.
    Warnings are informational, not blocking.
    """
    warnings = []
    language = blueprint.get("language", "").lower()

    if language not in SAFE_STACKS:
        return []  # Unknown language, no validation possible

    blessed = SAFE_STACKS[language]["blessed"]

    # Extract dependency names from blueprint
    deps = blueprint.get("dependencies", {})
    dep_content = deps.get("content", "")
    dep_type = deps.get("type", "")

    if not dep_content:
        return []

    # Parse dependency names based on type
    dep_names = _extract_dep_names(dep_content, dep_type, language)

    for dep_name in dep_names:
        dep_lower = dep_name.lower().strip()
        if not dep_lower:
            continue

        # Check if it's blessed (partial match for namespaced deps)
        is_blessed = any(
            dep_lower == b or dep_lower.startswith(b + "-") or dep_lower.startswith(b + "_")
            or b in dep_lower
            for b in blessed
        )

        # Check architecture_decisions for justification
        justified = False
        for decision in blueprint.get("architecture_decisions", []):
            if dep_lower in decision.get("decision", "").lower() or \
               dep_lower in decision.get("reasoning", "").lower():
                justified = True
                break

        if not is_blessed and not justified:
            warnings.append(
                f"Non-standard dependency '{dep_name}' for {language}. "
                f"Consider using a well-known alternative or justify in architecture_decisions."
            )

    return warnings


def _extract_dep_names(content: str, dep_type: str, language: str) -> list[str]:
    """Extract dependency names from dependency file content."""
    import re
    names = []

    if dep_type in ("cargo_toml", "") and language == "rust":
        # Match: name = "version" or name = { version = "..." }
        for match in re.finditer(r'^(\w[\w-]*)\s*=', content, re.MULTILINE):
            name = match.group(1)
            if name not in ("version", "edition", "name", "authors", "description",
                           "features", "default-features", "path", "workspace",
                           "dependencies", "dev-dependencies", "build-dependencies",
                           "package", "lib", "bin", "profile"):
                names.append(name)

    elif dep_type in ("requirements_txt", "") and language == "python":
        for line in content.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("-"):
                name = re.split(r'[>=<!\[\s]', line)[0]
                if name:
                    names.append(name)

    elif dep_type in ("package_json", "") and language in ("typescript", "javascript"):
        try:
            import json
            pkg = json.loads(content) if content.strip().startswith("{") else {}
            for section in ("dependencies", "devDependencies"):
                if section in pkg:
                    names.extend(pkg[section].keys())
        except Exception:
            # Fallback: regex
            for match in re.finditer(r'"([@\w/-]+)":\s*"', content):
                names.append(match.group(1))

    elif dep_type in ("go_mod", "") and language == "go":
        for match in re.finditer(r'^\s*([\w./]+)\s+v', content, re.MULTILINE):
            names.append(match.group(1))

    return names
