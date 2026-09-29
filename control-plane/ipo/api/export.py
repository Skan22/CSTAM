"""Write the OpenAPI document and a Postman collection generated from it.

    uv run python -m ipo.api.export ../docs/api
"""

import json
import sys
from pathlib import Path
from typing import Any

from ipo.api.app import AppDeps, create_app
from ipo.settings import Settings


def openapi() -> dict[str, Any]:
    def no_db() -> Any:
        raise RuntimeError("the export never touches the database")

    return create_app(AppDeps(no_db, Settings(), jwt_secret="x" * 32)).openapi()


def _example(spec: dict[str, Any], schema: dict[str, Any]) -> Any:
    if "$ref" in schema:
        schema = spec["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]]
    kind = schema.get("type")
    if kind == "object":
        return {k: _example(spec, v) for k, v in schema.get("properties", {}).items()
                if k in schema.get("required", [])}
    for option in schema.get("anyOf", []):
        if option.get("type") != "null":
            return _example(spec, option)
    return {"string": "string", "integer": 1, "boolean": False, "array": []}.get(str(kind))


def postman(spec: dict[str, Any]) -> dict[str, Any]:
    folders: dict[str, list[dict[str, Any]]] = {}
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            url = "{{baseUrl}}" + path.replace("{", ":").replace("}", "")
            headers = [{"key": "Idempotency-Key", "value": "{{$guid}}"}] if (
                path == "/v1/teams" and method == "post") else []
            item: dict[str, Any] = {
                "name": op.get("summary", f"{method.upper()} {path}"),
                "request": {"method": method.upper(), "header": headers, "url": {
                    "raw": url, "host": ["{{baseUrl}}"],
                    "path": [p.replace("{", ":").replace("}", "") for p in
                             path.strip("/").split("/")]}},
            }
            if path == "/v1/auth/login":
                item["request"]["auth"] = {"type": "noauth"}
                item["event"] = [{"listen": "test", "script": {"type": "text/javascript", "exec": [
                    "pm.collectionVariables.set('token', pm.response.json().access_token);"]}}]
            body = op.get("requestBody", {}).get("content", {}).get("application/json")
            if body:
                item["request"]["body"] = {
                    "mode": "raw", "options": {"raw": {"language": "json"}},
                    "raw": json.dumps(_example(spec, body["schema"]), indent=2)}
                item["request"]["header"].append({"key": "Content-Type",
                                                  "value": "application/json"})
            folders.setdefault((op.get("tags") or ["other"])[0], []).append(item)
    return {
        "info": {"name": spec["info"]["title"], "schema":
                 "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
        "auth": {"type": "bearer", "bearer": [{"key": "token", "value": "{{token}}",
                                                "type": "string"}]},
        "variable": [{"key": "baseUrl", "value": "http://localhost:8000"},
                     {"key": "token", "value": ""}],
        "item": [{"name": name, "item": items} for name, items in sorted(folders.items())],
    }


def render() -> dict[str, str]:
    spec = openapi()
    return {"openapi.json": json.dumps(spec, indent=2, sort_keys=True) + "\n",
            "ipo.postman_collection.json": json.dumps(postman(spec), indent=2) + "\n"}


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/api")
    out.mkdir(parents=True, exist_ok=True)
    for name, text in render().items():
        (out / name).write_text(text)
        print(f"wrote {out / name}")


if __name__ == "__main__":
    main()
