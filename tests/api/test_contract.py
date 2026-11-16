"""Test de contrato OpenAPI: compara app.openapi() contra docs/openapi.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from agrobot.api.main import create_app


def _resolve_param(spec_schema: dict[str, Any], param_dict: dict[str, Any]) -> dict[str, Any]:
    """Resuelve referencias $ref para parámetros del esquema OpenAPI."""
    if "$ref" in param_dict:
        parts = param_dict["$ref"].split("/")[1:]
        resolved: Any = spec_schema
        for part in parts:
            resolved = resolved[part]
        return resolved  # type: ignore[no-any-return]
    return param_dict


def test_openapi_contract_matches_spec() -> None:
    app = create_app()
    app_schema = app.openapi()

    spec_path = Path("docs/openapi.yaml")
    assert spec_path.exists(), "docs/openapi.yaml debe existir como fuente de verdad"

    with spec_path.open("r", encoding="utf-8") as f:
        spec_schema = yaml.safe_load(f)

    # 1. Validar que todos los paths de la especificación existan en la app
    for path, path_item in spec_schema["paths"].items():
        assert path in app_schema["paths"], f"Falta el path {path} en la API generada"

        path_level_params = path_item.get("parameters", [])

        for method, operation in path_item.items():
            if method in ("parameters", "$ref"):
                continue

            assert method in app_schema["paths"][path], (
                f"Falta el método {method.upper()} en {path}"
            )

            app_operation = app_schema["paths"][path][method]

            # 2. Validar que las respuestas esperadas estén declaradas
            for status_code in operation.get("responses", {}):
                assert status_code in app_operation.get("responses", {}), (
                    f"Falta el código de respuesta {status_code} en {method.upper()} {path}"
                )

            # 3. Validar que todos los parámetros esperados (query, header, path) existan
            op_params = path_level_params + operation.get("parameters", [])
            expected_params: list[tuple[str, str]] = []
            for raw_p in op_params:
                resolved = _resolve_param(spec_schema, raw_p)
                expected_params.append((resolved["name"], resolved["in"]))

            actual_params = [(p["name"], p["in"]) for p in app_operation.get("parameters", [])]

            for exp_name, exp_in in expected_params:
                assert (exp_name, exp_in) in actual_params, (
                    f"Falta el parámetro '{exp_name}' (in: {exp_in}) en {method.upper()} {path}. "
                    f"Parámetros encontrados: {actual_params}"
                )

    # 4. Validar componentes de esquemas clave
    for schema_name, schema_spec in spec_schema.get("components", {}).get("schemas", {}).items():
        if "properties" in schema_spec and "required" in schema_spec:
            app_schemas = app_schema.get("components", {}).get("schemas", {})
            matching_key = None
            for k in app_schemas:
                if (
                    k.lower() == schema_name.lower()
                    or k.lower() == f"{schema_name.lower()}response"
                ):
                    matching_key = k
                    break

            if matching_key:
                app_props = app_schemas[matching_key].get("properties", {})
                for req_prop in schema_spec["required"]:
                    assert req_prop in app_props, (
                        f"Falta el campo requerido {req_prop} en el esquema {matching_key}"
                    )
