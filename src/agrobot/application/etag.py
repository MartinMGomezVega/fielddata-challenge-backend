"""Utilidades para generación y parseo de ETags."""

from __future__ import annotations


def format_etag(version: int) -> str:
    """Genera el ETag débil estándar según especificación."""
    return f'W/"{version}"'


def parse_etag(etag_str: str) -> int | None:
    """Parsea el valor de versión contenido en un encabezado If-Match / ETag."""
    s = etag_str.strip()
    if s.startswith('W/"') and s.endswith('"') and len(s) >= 4:
        s = s[3:-1]
    elif s.startswith('"') and s.endswith('"') and len(s) >= 2:
        s = s[1:-1]
    try:
        return int(s)
    except ValueError:
        return None
