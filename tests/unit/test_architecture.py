"""Tests para validar la regla de dependencia de Clean Architecture.

Verifica mediante el AST de Python que las capas internas (domain y application)
no dependan de capas externas (infrastructure, api, worker) ni de frameworks
como SQLAlchemy o FastAPI.
"""

from __future__ import annotations

import ast
from pathlib import Path

FORBIDDEN_APPLICATION_IMPORTS = {
    "agrobot.infrastructure",
    "agrobot.api",
    "agrobot.worker",
    "sqlalchemy",
    "fastapi",
}

FORBIDDEN_DOMAIN_IMPORTS = FORBIDDEN_APPLICATION_IMPORTS | {
    "agrobot.application",
}


def _get_imports(file_path: Path) -> list[tuple[int, str]]:
    """Extrae todos los imports (módulo, línea) de un archivo usando AST."""
    tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
    imports: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append((node.lineno, node.module))
    return imports


def test_clean_architecture_domain_dependencies() -> None:
    """Verifica que src/agrobot/domain no importe capas externas ni frameworks."""
    domain_dir = Path("src/agrobot/domain")
    assert domain_dir.exists(), "src/agrobot/domain debe existir"

    violations: list[str] = []
    for py_file in domain_dir.rglob("*.py"):
        if py_file.name == "__init__.py":
            continue
        for lineno, module_name in _get_imports(py_file):
            for forbidden in FORBIDDEN_DOMAIN_IMPORTS:
                if module_name == forbidden or module_name.startswith(f"{forbidden}."):
                    violations.append(
                        f"{py_file}:{lineno} importa '{module_name}' (prohibido en domain)"
                    )

    assert not violations, "Violaciones de Clean Architecture en domain:\n" + "\n".join(violations)


def test_clean_architecture_application_dependencies() -> None:
    """Verifica que src/agrobot/application no importe capas externas ni frameworks."""
    app_dir = Path("src/agrobot/application")
    assert app_dir.exists(), "src/agrobot/application debe existir"

    violations: list[str] = []
    for py_file in app_dir.rglob("*.py"):
        if py_file.name == "__init__.py":
            continue
        for lineno, module_name in _get_imports(py_file):
            for forbidden in FORBIDDEN_APPLICATION_IMPORTS:
                if module_name == forbidden or module_name.startswith(f"{forbidden}."):
                    violations.append(
                        f"{py_file}:{lineno} importa '{module_name}' (prohibido en application)"
                    )

    assert not violations, "Violaciones de Clean Architecture en application:\n" + "\n".join(
        violations
    )
