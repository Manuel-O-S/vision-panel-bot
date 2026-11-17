"""
geometry.py - Rectángulos en píxeles y zonas en coordenadas relativas (0.0 - 1.0).

Independencia de resolución
    Las zonas se guardan normalizadas respecto al área cliente de la ventana
    objetivo y se convierten a píxeles en cada ciclo:

        X_píxel = X_relativo × Ancho      Y_píxel = Y_relativo × Alto

    Así una tarea creada a 1920×1080 funciona igual a 1280×720 o con la ventana
    movida a otra posición.

Módulo puro (sin OpenCV ni dependencias de SO): se puede usar desde la GUI,
la persistencia y los tests sin coste.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

__all__ = ["Rect", "RelativeZone", "to_pixel", "to_relative"]

_EPS = 1e-9


def to_pixel(relativo: float, tamano: int) -> int:
    """Convierte una coordenada relativa (0.0-1.0) a píxeles."""
    return int(round(relativo * tamano))


def to_relative(pixel: float, tamano: int) -> float:
    """Convierte una coordenada en píxeles a relativa (0.0-1.0)."""
    if tamano <= 0:
        raise ValueError(f"Tamaño inválido: {tamano}")
    return pixel / tamano


@dataclass(frozen=True, slots=True)
class Rect:
    """Rectángulo en píxeles absolutos de pantalla (escritorio virtual)."""

    left: int
    top: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"Rect con tamaño no positivo: {self}")

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height

    def as_ltrb(self) -> tuple[int, int, int, int]:
        """(left, top, right, bottom), formato que usa dxcam."""
        return self.left, self.top, self.right, self.bottom

    def contains(self, other: Rect) -> bool:
        return (
            other.left >= self.left and other.top >= self.top
            and other.right <= self.right and other.bottom <= self.bottom
        )

    def intersect(self, other: Rect) -> Rect | None:
        l, t = max(self.left, other.left), max(self.top, other.top)
        r, b = min(self.right, other.right), min(self.bottom, other.bottom)
        if r <= l or b <= t:
            return None
        return Rect(l, t, r - l, b - t)

    def union(self, other: Rect) -> Rect:
        l, t = min(self.left, other.left), min(self.top, other.top)
        r, b = max(self.right, other.right), max(self.bottom, other.bottom)
        return Rect(l, t, r - l, b - t)

    def center(self) -> tuple[int, int]:
        return self.left + self.width // 2, self.top + self.height // 2


@dataclass(frozen=True, slots=True)
class RelativeZone:
    """Zona de interés normalizada respecto al área cliente de la ventana.

    ``x, y`` = esquina superior izquierda; ``w, h`` = ancho y alto. Todo en 0.0-1.0.
    ``tolerance`` = Delta Threshold propio de la zona para comparar con el
    Baseline (``None`` = usar el valor global del motor).
    """

    name: str
    x: float
    y: float
    w: float
    h: float
    tolerance: float | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("La zona necesita un nombre")
        for campo in ("x", "y", "w", "h"):
            v = getattr(self, campo)
            if not math.isfinite(v):
                raise ValueError(f"{self.name}: {campo} no es finito")
        if not (0.0 <= self.x < 1.0 and 0.0 <= self.y < 1.0):
            raise ValueError(f"{self.name}: origen fuera de [0, 1): ({self.x}, {self.y})")
        if self.w <= 0 or self.h <= 0:
            raise ValueError(f"{self.name}: tamaño no positivo ({self.w}, {self.h})")
        if self.x + self.w > 1.0 + _EPS or self.y + self.h > 1.0 + _EPS:
            raise ValueError(f"{self.name}: la zona se sale de la ventana")
        if self.tolerance is not None and not 0.0 <= self.tolerance <= 1.0:
            raise ValueError(f"{self.name}: tolerancia fuera de [0, 1]: {self.tolerance}")

    # --------------------------- conversiones --------------------------- #
    def to_rect(self, window: Rect) -> Rect:
        """Rectángulo absoluto en píxeles para la ventana actual (mínimo 1×1)."""
        l = window.left + to_pixel(self.x, window.width)
        t = window.top + to_pixel(self.y, window.height)
        r = window.left + to_pixel(self.x + self.w, window.width)
        b = window.top + to_pixel(self.y + self.h, window.height)
        return Rect(l, t, max(1, r - l), max(1, b - t))

    def point(self, rx: float, ry: float, window: Rect) -> tuple[int, int]:
        """Punto absoluto a partir de una posición relativa *dentro* de la zona."""
        return (
            window.left + to_pixel(self.x + rx * self.w, window.width),
            window.top + to_pixel(self.y + ry * self.h, window.height),
        )

    @classmethod
    def from_rect(cls, name: str, rect: Rect, window: Rect,
                  tolerance: float | None = None) -> RelativeZone:
        """Crea la zona a partir de un recorte en píxeles (p. ej. el Snipping Tool)."""
        clipped = window.intersect(rect)
        if clipped is None:
            raise ValueError(f"{name}: el recorte está fuera de la ventana")
        return cls(
            name=name,
            x=to_relative(clipped.left - window.left, window.width),
            y=to_relative(clipped.top - window.top, window.height),
            w=to_relative(clipped.width, window.width),
            h=to_relative(clipped.height, window.height),
            tolerance=tolerance,
        )

    # --------------------------- serialización -------------------------- #
    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "nombre": self.name,
            "x": round(self.x, 6), "y": round(self.y, 6),
            "ancho": round(self.w, 6), "alto": round(self.h, 6),
        }
        if self.tolerance is not None:
            data["tolerancia"] = self.tolerance
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RelativeZone:
        return cls(
            name=str(data["nombre"]),
            x=float(data["x"]), y=float(data["y"]),
            w=float(data["ancho"]), h=float(data["alto"]),
            tolerance=None if data.get("tolerancia") is None else float(data["tolerancia"]),
        )
