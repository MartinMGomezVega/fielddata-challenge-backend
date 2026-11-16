"""Utilidades de paginación por cursor opaco (keyset pagination)."""

from __future__ import annotations

import base64
import uuid
from datetime import datetime

from agrobot.domain.errors import InvalidCursor


def encode_cursor(created_at: datetime, item_id: uuid.UUID) -> str:
    """Genera un cursor opaco codificado en base64 para keyset pagination."""
    raw = f"{created_at.isoformat()}_{item_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor_str: str) -> tuple[datetime, uuid.UUID]:
    """Decodifica un cursor opaco.

    Lanza InvalidCursor si el cursor no tiene formato válido.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor_str.encode()).decode()
        dt_str, id_str = raw.split("_", 1)
        return datetime.fromisoformat(dt_str), uuid.UUID(id_str)
    except Exception as exc:
        raise InvalidCursor("Cursor de paginación inválido o corrupto") from exc
