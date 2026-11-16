"""Paginación por número de página (independiente de la base de datos)."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil


@dataclass(frozen=True)
class PageParams:
    page: int = 1
    page_size: int = 10

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


@dataclass(frozen=True)
class PageMeta:
    total: int
    page: int
    page_size: int
    total_pages: int
    has_next: bool
    has_prev: bool

    @classmethod
    def build(cls, total: int, params: PageParams) -> PageMeta:
        total_pages = ceil(total / params.page_size) if total > 0 else 0
        return cls(
            total=total,
            page=params.page,
            page_size=params.page_size,
            total_pages=total_pages,
            has_next=params.page < total_pages,
            has_prev=params.page > 1,
        )


@dataclass(frozen=True)
class Page[T]:
    items: list[T]
    meta: PageMeta
