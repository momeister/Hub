"""
skills/builder/engine/openapi_contract.py - Contract-First Code Generation
===========================================================================
System 1: Generates an OpenAPI 3.0 spec from the blueprint, then
deterministically produces backend route stubs and frontend API client stubs.
The LLM fills business logic into these stubs — no free invention of API paths.
"""

from __future__ import annotations

import json
import re
from typing import Any

from skills.builder.engine.context import blog


# ---------------------------------------------------------------------------
# OpenAPI Spec Generation (LLM-assisted)
# ---------------------------------------------------------------------------
def generate_openapi_spec(
    blueprint: dict,
    llm_call_fn,
    manager_model: str,
) -> dict:
    """Let the LLM generate a complete OpenAPI 3.0 spec from the blueprint.

    Returns a validated OpenAPI 3.0 dict stored in blueprint["openapi_spec"].
    Retries up to 2 times on validation failure.
    """
    blog.phase("openapi_spec", "Generating OpenAPI 3.0 specification", model=manager_model)

    goal = blueprint.get("_goal", "") or blueprint.get("project_name", "")
    language = blueprint.get("language", "")
    framework = blueprint.get("framework", "")
    subprojects = blueprint.get("subprojects", [])
    api_endpoints = blueprint.get("api_endpoints", [])
    data_contracts = blueprint.get("data_contracts", [])

    # Build existing endpoint context
    ep_context = ""
    if api_endpoints:
        ep_lines = [
            f"  {ep.get('method','?')} {ep.get('path','?')}: {ep.get('description','')}\n"
            f"    Request: {json.dumps(ep.get('request_body', {}))}\n"
            f"    Response: {json.dumps(ep.get('response_body', {}))}"
            for ep in api_endpoints
        ]
        ep_context = "\nEXISTING API ENDPOINT DEFINITIONS (use these as basis):\n" + "\n".join(ep_lines)

    contracts_context = ""
    if data_contracts:
        ct_lines = [
            f"  {c.get('name','?')}: {c.get('structure','')}"
            for c in data_contracts
        ]
        contracts_context = "\nDATA CONTRACTS:\n" + "\n".join(ct_lines)

    sub_context = ""
    if subprojects:
        sub_lines = [
            f"  {s.get('name','?')} ({s.get('language','?')}/{s.get('framework','')})"
            for s in subprojects
        ]
        sub_context = "\nSUBPROJECTS:\n" + "\n".join(sub_lines)

    prompt = f"""Generate a complete OpenAPI 3.0 specification for this project as JSON.

PROJECT GOAL: {goal}
LANGUAGE: {language}
FRAMEWORK: {framework}
{sub_context}
{ep_context}
{contracts_context}

REQUIREMENTS:
1. Output a VALID OpenAPI 3.0 JSON object (openapi: "3.0.3")
2. Every endpoint MUST have:
   - operationId in snake_case (will be used as function name)
   - Complete requestBody with application/json schema (all fields with types)
   - Complete responses/200 with application/json schema (all fields with types)
   - parameters for path and query parameters with correct "in" field
3. All schemas must use JSON Schema types (string, integer, number, boolean, array, object)
4. Use snake_case for ALL field names consistently
5. Include a "components/schemas" section for shared data models
6. The "info" section must have title, version, description
7. basePath/servers: use http://localhost:8000

Output ONLY the JSON object, no explanation, no markdown fences."""

    from skills.builder.engine.context import clean_json

    spec = None
    last_error = ""

    for attempt in range(3):
        retry_hint = ""
        if attempt > 0 and last_error:
            retry_hint = f"\n\nPREVIOUS ATTEMPT FAILED VALIDATION:\n{last_error}\nFix the issues and output corrected JSON."

        try:
            response = llm_call_fn(
                model=manager_model,
                prompt=prompt + retry_hint,
                system="OpenAPI 3.0 expert. Output ONLY valid JSON. No markdown fences.",
                max_tokens=8192,
                temperature=0.05,
            )
            raw = json.loads(clean_json(response))
            if not isinstance(raw, dict):
                last_error = "Response is not a JSON object"
                continue

            # Basic structural validation
            validation_errors = _validate_openapi_structure(raw)
            if validation_errors:
                last_error = "; ".join(validation_errors[:5])
                blog.warning(f"OpenAPI spec attempt {attempt + 1} invalid: {last_error}")
                continue

            spec = raw
            blog.info(f"OpenAPI spec generated: {len(raw.get('paths', {}))} paths, attempt {attempt + 1}")
            break

        except (json.JSONDecodeError, Exception) as exc:
            last_error = str(exc)
            blog.warning(f"OpenAPI spec attempt {attempt + 1} failed: {exc}")

    if spec is None:
        blog.warning("Could not generate valid OpenAPI spec after 3 attempts, using fallback")
        spec = _build_fallback_spec(blueprint)

    blueprint["openapi_spec"] = spec
    return spec


def _validate_openapi_structure(spec: dict) -> list[str]:
    """Basic structural validation of an OpenAPI 3.0 spec. Returns list of errors."""
    errors = []

    if "openapi" not in spec:
        errors.append("Missing 'openapi' version field")
    if "info" not in spec:
        errors.append("Missing 'info' section")
    if "paths" not in spec:
        errors.append("Missing 'paths' section")
    elif not isinstance(spec["paths"], dict):
        errors.append("'paths' must be an object")

    # Validate each path has valid methods and operationIds
    for path, methods in (spec.get("paths") or {}).items():
        if not isinstance(methods, dict):
            errors.append(f"Path '{path}' value must be an object")
            continue
        valid_methods = {"get", "post", "put", "delete", "patch", "options", "head"}
        for method, operation in methods.items():
            if method.lower() not in valid_methods:
                continue
            if not isinstance(operation, dict):
                errors.append(f"{method.upper()} {path}: operation must be an object")
                continue
            if "operationId" not in operation:
                errors.append(f"{method.upper()} {path}: missing operationId")

    return errors


def _build_fallback_spec(blueprint: dict) -> dict:
    """Build a minimal OpenAPI spec from existing api_endpoints in blueprint."""
    spec: dict[str, Any] = {
        "openapi": "3.0.3",
        "info": {
            "title": blueprint.get("project_name", "API"),
            "version": "1.0.0",
            "description": blueprint.get("_goal", ""),
        },
        "servers": [{"url": "http://localhost:8000"}],
        "paths": {},
    }

    for ep in blueprint.get("api_endpoints", []):
        path = ep.get("path", "")
        method = ep.get("method", "GET").lower()
        if not path:
            continue

        operation_id = _path_to_operation_id(path, method)
        operation: dict[str, Any] = {
            "operationId": operation_id,
            "summary": ep.get("description", ""),
            "responses": {
                "200": {
                    "description": "Successful response",
                    "content": {
                        "application/json": {
                            "schema": _dict_to_schema(ep.get("response_body", {}))
                        }
                    },
                }
            },
        }

        # Path parameters
        params = re.findall(r'\{(\w+)\}', path)
        if params:
            operation["parameters"] = [
                {"name": p, "in": "path", "required": True, "schema": {"type": "string"}}
                for p in params
            ]

        # Request body
        req_body = ep.get("request_body", {})
        if req_body and method in ("post", "put", "patch"):
            operation["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _dict_to_schema(req_body)
                    }
                },
            }

        spec["paths"].setdefault(path, {})[method] = operation

    return spec


def _path_to_operation_id(path: str, method: str) -> str:
    """Convert /api/games/{game_id}/move + POST -> post_game_move"""
    parts = path.strip("/").split("/")
    cleaned = []
    for part in parts:
        if part.startswith("{") and part.endswith("}"):
            continue  # Skip path params
        if part in ("api", "v1", "v2"):
            continue  # Skip prefix segments
        cleaned.append(part.rstrip("s") if len(part) > 3 else part)
    name = "_".join(cleaned) if cleaned else "root"
    return f"{method}_{name}"


def _dict_to_schema(d: dict) -> dict:
    """Convert a flat dict of field names to a JSON Schema object."""
    if not d or not isinstance(d, dict):
        return {"type": "object", "properties": {}}

    properties = {}
    for key, val in d.items():
        if isinstance(val, str):
            properties[key] = {"type": "string"}
        elif isinstance(val, bool):
            properties[key] = {"type": "boolean"}
        elif isinstance(val, int):
            properties[key] = {"type": "integer"}
        elif isinstance(val, float):
            properties[key] = {"type": "number"}
        elif isinstance(val, list):
            properties[key] = {"type": "array", "items": {"type": "string"}}
        elif isinstance(val, dict):
            properties[key] = _dict_to_schema(val)
        else:
            properties[key] = {"type": "string"}

    return {"type": "object", "properties": properties}


# ---------------------------------------------------------------------------
# Deterministic Backend Stub Generation (NO LLM)
# ---------------------------------------------------------------------------
def generate_backend_stubs(
    openapi_spec: dict,
    language: str,
    framework: str,
) -> dict[str, str]:
    """Generate backend route stubs deterministically from the OpenAPI spec.

    Returns dict of filepath -> stub code. NO LLM call.
    """
    language = language.lower()
    framework = framework.lower() if framework else ""

    if language == "python" and "fastapi" in framework:
        return _generate_fastapi_stubs(openapi_spec)
    elif language == "python" and "flask" in framework:
        return _generate_flask_stubs(openapi_spec)
    elif language in ("javascript", "typescript") and "express" in framework:
        is_ts = language == "typescript"
        return _generate_express_stubs(openapi_spec, typescript=is_ts)
    else:
        return _generate_generic_backend_stubs(openapi_spec, language, framework)


def _generate_fastapi_stubs(spec: dict) -> dict[str, str]:
    """Generate FastAPI route stubs + Pydantic models from OpenAPI spec."""
    models_code = _generate_pydantic_models(spec)
    routes_code = _generate_fastapi_routes(spec)

    return {
        "models_stub.py": models_code,
        "routes_stub.py": routes_code,
    }


def _generate_pydantic_models(spec: dict) -> str:
    """Generate Pydantic v2 models from OpenAPI schemas."""
    lines = [
        '"""Auto-generated Pydantic models from OpenAPI spec. DO NOT modify signatures."""',
        "",
        "from __future__ import annotations",
        "from typing import Any, Optional",
        "from pydantic import BaseModel",
        "",
    ]

    # Collect all schemas from components and inline
    schemas: dict[str, dict] = {}

    # From components/schemas
    for name, schema in (spec.get("components", {}).get("schemas", {})).items():
        schemas[name] = schema

    # From inline request/response bodies
    for path, methods in spec.get("paths", {}).items():
        for method, operation in methods.items():
            if not isinstance(operation, dict):
                continue
            op_id = operation.get("operationId", _path_to_operation_id(path, method))

            # Request body schema
            req_body = operation.get("requestBody", {})
            req_schema = (
                req_body.get("content", {})
                .get("application/json", {})
                .get("schema", {})
            )
            if req_schema and req_schema.get("properties"):
                req_name = _snake_to_pascal(op_id) + "Request"
                if req_name not in schemas:
                    schemas[req_name] = req_schema

            # Response body schema
            resp_schema = (
                operation.get("responses", {})
                .get("200", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema", {})
            )
            if resp_schema and resp_schema.get("properties"):
                resp_name = _snake_to_pascal(op_id) + "Response"
                if resp_name not in schemas:
                    schemas[resp_name] = resp_schema

    # Generate model classes
    for model_name, schema in schemas.items():
        lines.append("")
        lines.append(f"class {model_name}(BaseModel):")
        props = schema.get("properties", {})
        required = set(schema.get("required", []))

        if not props:
            lines.append("    pass")
            continue

        for field_name, field_schema in props.items():
            py_type = _json_schema_to_python_type(field_schema)
            if field_name not in required:
                py_type = f"Optional[{py_type}]"
                lines.append(f"    {field_name}: {py_type} = None")
            else:
                lines.append(f"    {field_name}: {py_type}")

    lines.append("")
    return "\n".join(lines)


def _generate_fastapi_routes(spec: dict) -> str:
    """Generate FastAPI router with exact route decorators from spec."""
    lines = [
        '"""Auto-generated FastAPI routes from OpenAPI spec.',
        'DO NOT modify function signatures, decorators, or URL paths.',
        'Only implement the business logic inside each function body."""',
        "",
        "from __future__ import annotations",
        "",
        "from fastapi import APIRouter, HTTPException",
        "from models_stub import *  # noqa: F403 — all generated models",
        "",
        "router = APIRouter()",
        "",
    ]

    method_map = {
        "get": "router.get",
        "post": "router.post",
        "put": "router.put",
        "delete": "router.delete",
        "patch": "router.patch",
    }

    for path, methods in spec.get("paths", {}).items():
        for method, operation in methods.items():
            method_lower = method.lower()
            if method_lower not in method_map:
                continue
            if not isinstance(operation, dict):
                continue

            op_id = operation.get("operationId", _path_to_operation_id(path, method_lower))
            summary = operation.get("summary", "")
            decorator = method_map[method_lower]

            # Build parameter list
            params = []
            # Path parameters
            for param in operation.get("parameters", []):
                if param.get("in") == "path":
                    p_name = param["name"]
                    p_type = _json_schema_to_python_type(param.get("schema", {"type": "string"}))
                    params.append(f"{p_name}: {p_type}")
                elif param.get("in") == "query":
                    p_name = param["name"]
                    p_type = _json_schema_to_python_type(param.get("schema", {"type": "string"}))
                    required = param.get("required", False)
                    if required:
                        params.append(f"{p_name}: {p_type}")
                    else:
                        params.append(f"{p_name}: {p_type} = None")

            # Request body parameter
            req_body = operation.get("requestBody", {})
            req_schema = (
                req_body.get("content", {})
                .get("application/json", {})
                .get("schema", {})
            )
            if req_schema and req_schema.get("properties"):
                req_model_name = _snake_to_pascal(op_id) + "Request"
                params.append(f"body: {req_model_name}")

            # Response model
            resp_schema = (
                operation.get("responses", {})
                .get("200", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema", {})
            )
            resp_model = ""
            if resp_schema and resp_schema.get("properties"):
                resp_model_name = _snake_to_pascal(op_id) + "Response"
                resp_model = f", response_model={resp_model_name}"

            params_str = ", ".join(params)
            lines.append(f'@{decorator}("{path}"{resp_model})')
            lines.append(f"def {op_id}({params_str}):")
            if summary:
                lines.append(f'    """{summary}"""')
            lines.append(f"    # TODO: implement business logic for {method.upper()} {path}")
            lines.append(f"    pass")
            lines.append("")

    return "\n".join(lines)


def _generate_flask_stubs(spec: dict) -> dict[str, str]:
    """Generate Flask route stubs from OpenAPI spec."""
    lines = [
        '"""Auto-generated Flask routes from OpenAPI spec.',
        'DO NOT modify function signatures, decorators, or URL paths.',
        'Only implement the business logic inside each function body."""',
        "",
        "from flask import Blueprint, request, jsonify",
        "",
        "api = Blueprint('api', __name__)",
        "",
    ]

    for path, methods in spec.get("paths", {}).items():
        for method, operation in methods.items():
            method_lower = method.lower()
            if method_lower not in ("get", "post", "put", "delete", "patch"):
                continue
            if not isinstance(operation, dict):
                continue

            op_id = operation.get("operationId", _path_to_operation_id(path, method_lower))
            # Convert OpenAPI path params {id} to Flask <id>
            flask_path = re.sub(r'\{(\w+)\}', r'<\1>', path)
            path_params_list = re.findall(r'<(\w+)>', flask_path)
            params_str = ', '.join(path_params_list)

            lines.append(f'@api.route("{flask_path}", methods=["{method.upper()}"])')
            lines.append(f"def {op_id}({params_str}):")
            lines.append(f"    # TODO: implement business logic for {method.upper()} {path}")
            lines.append(f"    pass")
            lines.append("")

    return {"routes_stub.py": "\n".join(lines)}


def _generate_express_stubs(spec: dict, typescript: bool = False) -> dict[str, str]:
    """Generate Express.js route stubs from OpenAPI spec."""
    ext = "ts" if typescript else "js"
    lines = [
        f'// Auto-generated Express routes from OpenAPI spec.',
        f'// DO NOT modify function signatures, route paths, or HTTP methods.',
        f'// Only implement the business logic inside each handler.',
        "",
        "const express = require('express');",
        "const router = express.Router();",
        "",
    ]

    for path, methods in spec.get("paths", {}).items():
        for method, operation in methods.items():
            method_lower = method.lower()
            if method_lower not in ("get", "post", "put", "delete", "patch"):
                continue
            if not isinstance(operation, dict):
                continue

            op_id = operation.get("operationId", _path_to_operation_id(path, method_lower))
            # Convert {param} to :param for Express
            express_path = re.sub(r'\{(\w+)\}', r':\1', path)

            lines.append(f"// {op_id}: {method.upper()} {path}")
            lines.append(f"router.{method_lower}('{express_path}', (req, res) => {{")
            lines.append(f"  // TODO: implement business logic")
            lines.append(f"  res.status(501).json({{ detail: 'Not implemented' }});")
            lines.append(f"}});")
            lines.append("")

    lines.append("module.exports = router;")
    lines.append("")
    return {f"routes_stub.{ext}": "\n".join(lines)}


def _generate_generic_backend_stubs(
    spec: dict, language: str, framework: str,
) -> dict[str, str]:
    """Generate comment-based stubs for unsupported frameworks."""
    lines = [
        f"# Auto-generated API stubs from OpenAPI spec",
        f"# Language: {language}, Framework: {framework}",
        f"# DO NOT modify endpoint paths or function signatures.",
        "",
    ]

    for path, methods in spec.get("paths", {}).items():
        for method, operation in methods.items():
            if not isinstance(operation, dict):
                continue
            op_id = operation.get("operationId", _path_to_operation_id(path, method))
            lines.append(f"# {method.upper()} {path} -> {op_id}")
            lines.append(f"#   Request: {json.dumps(operation.get('requestBody', {}), default=str)[:200]}")
            resp = operation.get("responses", {}).get("200", {})
            lines.append(f"#   Response 200: {json.dumps(resp, default=str)[:200]}")
            lines.append("")

    return {"api_stubs.txt": "\n".join(lines)}


# ---------------------------------------------------------------------------
# Deterministic Frontend Client Stub Generation (NO LLM)
# ---------------------------------------------------------------------------
def generate_frontend_client_stubs(
    openapi_spec: dict,
    framework: str,
) -> dict[str, str]:
    """Generate frontend API client stub deterministically from OpenAPI spec.

    Returns dict of filepath -> client code. NO LLM call.
    """
    framework_lower = (framework or "").lower()

    if "typescript" in framework_lower or "react" in framework_lower:
        return _generate_ts_client(openapi_spec)
    else:
        return _generate_js_client(openapi_spec)


def _generate_js_client(spec: dict) -> dict[str, str]:
    """Generate a JavaScript API client with fetch() from OpenAPI spec."""
    lines = [
        "// Auto-generated API client from OpenAPI spec.",
        "// DO NOT modify function names, URL paths, or HTTP methods.",
        "// These match the backend exactly.",
        "",
        "const API_BASE_URL = 'http://localhost:8000';",
        "",
    ]

    for path, methods in spec.get("paths", {}).items():
        for method, operation in methods.items():
            method_lower = method.lower()
            if method_lower not in ("get", "post", "put", "delete", "patch"):
                continue
            if not isinstance(operation, dict):
                continue

            op_id = operation.get("operationId", _path_to_operation_id(path, method_lower))
            func_name = _snake_to_camel(op_id)
            summary = operation.get("summary", "")

            # Collect parameters
            path_params = []
            query_params = []
            has_body = False

            for param in operation.get("parameters", []):
                if param.get("in") == "path":
                    path_params.append(param["name"])
                elif param.get("in") == "query":
                    query_params.append(param["name"])

            req_body = operation.get("requestBody", {})
            req_schema = (
                req_body.get("content", {})
                .get("application/json", {})
                .get("schema", {})
            )
            if req_schema and req_schema.get("properties"):
                has_body = True

            # Build function signature
            params = list(path_params) + list(query_params)
            if has_body:
                params.append("body")
            params_str = ", ".join(params)

            # Build URL with template literals for path params
            url_path = path
            for pp in path_params:
                url_path = url_path.replace(
                    "{" + pp + "}", "${" + _snake_to_camel(pp) + "}"
                )

            # If path params exist, need camelCase param names in signature too
            camel_params = [_snake_to_camel(p) for p in path_params]
            sig_params = camel_params + [_snake_to_camel(p) for p in query_params]
            if has_body:
                sig_params.append("body")
            sig_str = ", ".join(sig_params)

            # Build JSDoc
            lines.append(f"/**")
            lines.append(f" * {summary or op_id}")
            lines.append(f" * {method.upper()} {path}")
            for pp in path_params:
                lines.append(f" * @param {{{_json_schema_to_jsdoc_type({})}}} {_snake_to_camel(pp)}")
            if has_body:
                lines.append(f" * @param {{Object}} body - Request body")
            lines.append(f" * @returns {{Promise<Object>}}")
            lines.append(f" */")

            lines.append(f"async function {func_name}({sig_str}) {{")

            # Build query string
            if query_params:
                lines.append(f"  const params = new URLSearchParams();")
                for qp in query_params:
                    camel_qp = _snake_to_camel(qp)
                    lines.append(f"  if ({camel_qp} !== undefined) params.append('{qp}', {camel_qp});")
                lines.append(f"  const queryString = params.toString() ? '?' + params.toString() : '';")
                url_suffix = "${queryString}"
            else:
                url_suffix = ""

            # Build fetch call
            url_expr = f"`${{API_BASE_URL}}{url_path}{url_suffix}`"
            lines.append(f"  const response = await fetch({url_expr}, {{")
            lines.append(f"    method: '{method.upper()}',")
            if has_body:
                lines.append(f"    headers: {{ 'Content-Type': 'application/json' }},")
                lines.append(f"    body: JSON.stringify(body),")
            lines.append(f"  }});")
            lines.append(f"  if (!response.ok) {{")
            lines.append(f"    const err = await response.json().catch(() => ({{ detail: 'Request failed' }}));")
            lines.append(f"    throw new Error(err.detail || `HTTP ${{response.status}}`);")
            lines.append(f"  }}")
            lines.append(f"  return response.json();")
            lines.append(f"}}")
            lines.append("")

    return {"api_client.js": "\n".join(lines)}


def _generate_ts_client(spec: dict) -> dict[str, str]:
    """Generate a TypeScript API client from OpenAPI spec."""
    lines = [
        "// Auto-generated TypeScript API client from OpenAPI spec.",
        "// DO NOT modify function names, URL paths, or HTTP methods.",
        "",
        "const API_BASE_URL = 'http://localhost:8000';",
        "",
    ]

    # Generate interfaces from schemas
    for name, schema in (spec.get("components", {}).get("schemas", {})).items():
        lines.append(f"export interface {name} {{")
        for field_name, field_schema in schema.get("properties", {}).items():
            ts_type = _json_schema_to_ts_type(field_schema)
            required = field_name in schema.get("required", [])
            opt = "" if required else "?"
            lines.append(f"  {field_name}{opt}: {ts_type};")
        lines.append(f"}}")
        lines.append("")

    # Generate functions (same structure as JS but with types)
    for path, methods in spec.get("paths", {}).items():
        for method, operation in methods.items():
            method_lower = method.lower()
            if method_lower not in ("get", "post", "put", "delete", "patch"):
                continue
            if not isinstance(operation, dict):
                continue

            op_id = operation.get("operationId", _path_to_operation_id(path, method_lower))
            func_name = _snake_to_camel(op_id)

            path_params = []
            for param in operation.get("parameters", []):
                if param.get("in") == "path":
                    path_params.append(param["name"])

            has_body = bool(
                operation.get("requestBody", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema", {})
                .get("properties")
            )

            sig_params = [f"{_snake_to_camel(p)}: string" for p in path_params]
            if has_body:
                sig_params.append("body: Record<string, any>")
            sig_str = ", ".join(sig_params)

            url_path = path
            for pp in path_params:
                url_path = url_path.replace(
                    "{" + pp + "}", "${" + _snake_to_camel(pp) + "}"
                )

            lines.append(f"export async function {func_name}({sig_str}): Promise<any> {{")
            lines.append(f"  const response = await fetch(`${{API_BASE_URL}}{url_path}`, {{")
            lines.append(f"    method: '{method.upper()}',")
            if has_body:
                lines.append(f"    headers: {{ 'Content-Type': 'application/json' }},")
                lines.append(f"    body: JSON.stringify(body),")
            lines.append(f"  }});")
            lines.append(f"  if (!response.ok) {{")
            lines.append(f"    const err = await response.json().catch(() => ({{ detail: 'Request failed' }}));")
            lines.append(f"    throw new Error(err.detail || `HTTP ${{response.status}}`);")
            lines.append(f"  }}")
            lines.append(f"  return response.json();")
            lines.append(f"}}")
            lines.append("")

    return {"api_client.ts": "\n".join(lines)}


# ---------------------------------------------------------------------------
# Blueprint injection
# ---------------------------------------------------------------------------
def inject_stubs_into_blueprint(
    blueprint: dict,
    backend_stubs: dict[str, str],
    frontend_stubs: dict[str, str],
) -> dict:
    """Inject generated stubs as pre_written_files into the blueprint.

    The Coder agent will see these stubs and fill in the business logic
    without changing function signatures or API paths.
    """
    pre_written = {}
    stub_instructions = []

    subprojects = blueprint.get("subprojects", [])
    backend_name = ""
    frontend_name = ""

    for sp in subprojects:
        name_lower = sp.get("name", "").lower()
        if name_lower in ("backend", "server", "api"):
            backend_name = sp["name"]
        elif name_lower in ("frontend", "client", "web"):
            frontend_name = sp["name"]

    # Add backend stubs
    for filename, code in backend_stubs.items():
        key = f"{backend_name}/{filename}" if backend_name else filename
        pre_written[key] = code
        stub_instructions.append(
            f"FILE '{key}' is a CONTRACT STUB. Implement the business logic "
            f"inside each function. DO NOT change function signatures, "
            f"decorators, URL paths, or model field names."
        )

    # Add frontend stubs
    for filename, code in frontend_stubs.items():
        key = f"{frontend_name}/{filename}" if frontend_name else filename
        pre_written[key] = code
        stub_instructions.append(
            f"FILE '{key}' is a CONTRACT STUB (API client). DO NOT modify "
            f"function names, URL paths, HTTP methods, or request/response structures. "
            f"You may import and use these functions in other frontend files."
        )

    blueprint["pre_written_files"] = pre_written
    blueprint["stub_instructions"] = stub_instructions

    blog.info(
        f"Injected {len(backend_stubs)} backend + {len(frontend_stubs)} frontend stubs "
        f"into blueprint"
    )
    return blueprint


# ---------------------------------------------------------------------------
# Type conversion helpers
# ---------------------------------------------------------------------------
def _json_schema_to_python_type(schema: dict) -> str:
    """Convert a JSON Schema type to a Python type annotation."""
    t = schema.get("type", "Any")
    mapping = {
        "string": "str",
        "integer": "int",
        "number": "float",
        "boolean": "bool",
        "array": "list",
        "object": "dict",
    }
    py_type = mapping.get(t, "Any")
    if t == "array" and "items" in schema:
        item_type = _json_schema_to_python_type(schema["items"])
        py_type = f"list[{item_type}]"
    return py_type


def _json_schema_to_ts_type(schema: dict) -> str:
    """Convert a JSON Schema type to a TypeScript type."""
    t = schema.get("type", "any")
    mapping = {
        "string": "string",
        "integer": "number",
        "number": "number",
        "boolean": "boolean",
        "array": "any[]",
        "object": "Record<string, any>",
    }
    return mapping.get(t, "any")


def _json_schema_to_jsdoc_type(schema: dict) -> str:
    """Convert a JSON Schema type to a JSDoc type."""
    t = schema.get("type", "any")
    mapping = {
        "string": "string",
        "integer": "number",
        "number": "number",
        "boolean": "boolean",
        "array": "Array",
        "object": "Object",
    }
    return mapping.get(t, "any")


def _snake_to_pascal(name: str) -> str:
    """Convert snake_case to PascalCase."""
    return "".join(word.capitalize() for word in name.split("_"))


def _snake_to_camel(name: str) -> str:
    """Convert snake_case to camelCase."""
    parts = name.split("_")
    return parts[0] + "".join(word.capitalize() for word in parts[1:])
