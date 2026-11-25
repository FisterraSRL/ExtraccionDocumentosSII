"""Identidad del pedido actual, separada del certificado global de compatibilidad."""
from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    id: int | None
    email: str
    rol: str
    perfil_id: str | None

    @property
    def administrador(self) -> bool:
        return self.rol == "administrador"


_actual: ContextVar[Principal | None] = ContextVar("principal_portal", default=None)


def actual() -> Principal | None:
    return _actual.get()


def establecer(principal: Principal | None) -> Token:
    return _actual.set(principal)


def restaurar(token: Token) -> None:
    _actual.reset(token)
