"""
entropy.py - Generador CSPRNG y distribución normal (Box-Muller) con 4 decimales.
"""
from __future__ import annotations

import math
import secrets

_SYS = secrets.SystemRandom()
PRECISION = 4


def uniform(a: float, b: float) -> float:
    """Uniforme criptográficamente segura en [a, b], redondeada a 4 decimales."""
    return round(_SYS.uniform(a, b), PRECISION)


def box_muller(mu: float = 0.0, sigma: float = 1.0) -> float:
    """Muestra N(mu, sigma) usando la transformada de Box-Muller sobre un CSPRNG."""
    u1 = 0.0
    while u1 <= 1e-12:  # evitar log(0)
        u1 = _SYS.random()
    u2 = _SYS.random()
    z0 = math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)
    return round(mu + z0 * sigma, PRECISION)


def gaussian_clamped(mu: float, sigma: float, low: float, high: float) -> float:
    """Normal truncada por recorte a [low, high]."""
    return round(min(max(box_muller(mu, sigma), low), high), PRECISION)


def jitter_point(x: int, y: int, sigma_px: float = 2.0) -> tuple[int, int]:
    """Desplaza un punto con ruido gaussiano (en píxeles)."""
    return int(round(x + box_muller(0, sigma_px))), int(round(y + box_muller(0, sigma_px)))
