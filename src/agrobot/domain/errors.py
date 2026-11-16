"""Errores del dominio puro.

Las capas superiores (application/api) capturan estos errores y los traducen
a respuestas HTTP RFC 9457 (problem+json) o códigos de estado específicos.
"""


class DomainError(Exception):
    """Clase base para todas las excepciones del dominio."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class AlertNotFound(DomainError):
    """La alerta no existe o pertenece a otro usuario."""


class DuplicateAlert(DomainError):
    """Ya existe una alerta viva para el mismo campo, evento y umbral."""


class VersionConflict(DomainError):
    """Conflicto de concurrencia optimista (ETag / If-Match)."""


class FieldNotFound(DomainError):
    """El campo no existe o no pertenece al usuario solicitante."""


class InvalidThreshold(DomainError):
    """El umbral debe cumplir 0 < umbral <= 100."""


class InvalidTimezone(DomainError):
    """La zona horaria del campo no es un identificador IANA válido."""


class IdempotencyConflict(DomainError):
    """Misma Idempotency-Key pero con un cuerpo de solicitud diferente (422)."""


class InvalidCursor(DomainError):
    """El cursor de paginación es inválido o está malformado."""
