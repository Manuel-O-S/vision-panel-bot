"""
entropy.py - Generador de entropía (CSPRNG) y delays "humanos".

Fuente de aleatoriedad
    ``os.urandom`` -> CSPRNG del sistema operativo (BCryptGenRandom en Windows).
    No hay estado interno ni semilla: dos ejecuciones nunca producen la misma
    secuencia y no se puede predecir el siguiente valor observando los anteriores.

Distribución
    Normal (transformada de Box-Muller) **truncada por rechazo** al intervalo
    [min, max]. A diferencia de recortar (clamp), el rechazo no acumula masa de
    probabilidad en los extremos, así que el histograma no muestra "picos"
    artificiales en los límites que delaten a un generador.

Todas las funciones son thread-safe (no comparten estado mutable).
"""
from __future__ import annotations

import math
import os
import threading
import time

__all__ = [
    "DECIMALES_POR_DEFECTO",
    "unidad_aleatoria",
    "normal_estandar",
    "generar_delay_entropico",
    "dormir_entropico",
    "uniform",
    "box_muller",
    "gaussian_clamped",
    "jitter_point",
]

DECIMALES_POR_DEFECTO = 4

# Por defecto el intervalo [min, max] cubre ±3σ alrededor de la media (99.73 %).
SIGMAS_POR_DEFECTO = 3.0

_BITS_MANTISA = 53                     # precisión completa de un float IEEE-754
_ESCALA = 1.0 / (1 << _BITS_MANTISA)
_MAX_REINTENTOS = 64                   # rechazo: prob. de agotarlos ≈ 0.0027^64


# --------------------------------------------------------------------------- #
# Primitivas
# --------------------------------------------------------------------------- #
def unidad_aleatoria() -> float:
    """Float uniforme en el intervalo **abierto** (0, 1) con 53 bits de ``os.urandom``.

    Se excluye el 0 para que ``log(u)`` en Box-Muller nunca falle.
    """
    while True:
        n = int.from_bytes(os.urandom(7), "big") >> 3  # 56 bits -> 53 bits
        if n:
            return n * _ESCALA


def normal_estandar() -> float:
    """Muestra N(0, 1) mediante Box-Muller (forma trigonométrica)."""
    u1 = unidad_aleatoria()
    u2 = unidad_aleatoria()
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


def _validar_rango(min_val: float, max_val: float, decimales: int) -> None:
    if not (math.isfinite(min_val) and math.isfinite(max_val)):
        raise ValueError(f"Rango no finito: [{min_val}, {max_val}]")
    if min_val < 0:
        raise ValueError(f"El mínimo no puede ser negativo: {min_val}")
    if max_val < min_val:
        raise ValueError(f"Rango invertido: [{min_val}, {max_val}]")
    if not 0 <= decimales <= 10:
        raise ValueError(f"decimales debe estar entre 0 y 10: {decimales}")


def _normal_truncada(mu: float, sigma: float, low: float, high: float) -> float:
    """N(mu, sigma) truncada a [low, high] por rechazo (sin picos en los bordes)."""
    for _ in range(_MAX_REINTENTOS):
        v = mu + sigma * normal_estandar()
        if low <= v <= high:
            return v
    # Prácticamente inalcanzable con σ razonable; respaldo uniforme, nunca clamp.
    return low + (high - low) * unidad_aleatoria()


# --------------------------------------------------------------------------- #
# API principal
# --------------------------------------------------------------------------- #
def generar_delay_entropico(
    min_val: float,
    max_val: float,
    decimales: int = DECIMALES_POR_DEFECTO,
    *,
    sigmas: float = SIGMAS_POR_DEFECTO,
) -> float:
    """Devuelve un delay (en segundos) dentro de [min_val, max_val].

    - Media en el centro del intervalo; desviación ``(max - min) / (2 * sigmas)``.
      Con ``sigmas=3`` los valores se concentran en el centro y rara vez tocan
      los bordes, como el tiempo de reacción neuromuscular humano.
    - Resultado redondeado a ``decimales`` (4 por defecto -> 10 000 valores
      posibles por segundo de rango).
    - El resultado redondeado **siempre** queda dentro de [min_val, max_val].

    >>> 0.2 <= generar_delay_entropico(0.2, 0.5) <= 0.5
    True
    """
    min_val, max_val = float(min_val), float(max_val)
    _validar_rango(min_val, max_val, decimales)
    if sigmas <= 0:
        raise ValueError(f"sigmas debe ser > 0: {sigmas}")

    factor = 10 ** decimales
    # Límites representables con `decimales` dentro del rango (evita que el
    # redondeo saque el valor fuera de [min, max]).
    lo = math.ceil(round(min_val * factor, 6)) / factor
    hi = math.floor(round(max_val * factor, 6)) / factor
    if hi <= lo:
        return round((min_val + max_val) / 2.0, decimales)

    mu = (min_val + max_val) / 2.0
    sigma = (max_val - min_val) / (2.0 * sigmas)
    valor = _normal_truncada(mu, sigma, min_val, max_val)
    return round(min(max(round(valor, decimales), lo), hi), decimales)


def dormir_entropico(
    min_val: float,
    max_val: float,
    evento_parada: threading.Event | None = None,
    decimales: int = DECIMALES_POR_DEFECTO,
) -> bool:
    """Duerme un delay entrópico. Devuelve ``False`` si se interrumpió.

    Si se pasa ``evento_parada`` la espera es interrumpible al instante
    (botón de pánico): en cuanto el evento se activa, la función retorna.
    """
    delay = generar_delay_entropico(min_val, max_val, decimales)
    if delay <= 0:
        return not (evento_parada and evento_parada.is_set())
    if evento_parada is None:
        time.sleep(delay)
        return True
    return not evento_parada.wait(delay)


# --------------------------------------------------------------------------- #
# Compatibilidad (usadas por core/input_sim.py)
# --------------------------------------------------------------------------- #
def uniform(a: float, b: float) -> float:
    """Uniforme CSPRNG en [a, b], redondeada a 4 decimales."""
    return round(a + (b - a) * unidad_aleatoria(), DECIMALES_POR_DEFECTO)


def box_muller(mu: float = 0.0, sigma: float = 1.0) -> float:
    """Muestra N(mu, sigma) sin truncar, redondeada a 4 decimales."""
    return round(mu + sigma * normal_estandar(), DECIMALES_POR_DEFECTO)


def gaussian_clamped(mu: float, sigma: float, low: float, high: float) -> float:
    """Normal N(mu, sigma) truncada a [low, high] (por rechazo, no por recorte)."""
    if high < low:
        raise ValueError(f"Rango invertido: [{low}, {high}]")
    if sigma <= 0 or high == low:
        return round(min(max(mu, low), high), DECIMALES_POR_DEFECTO)
    return round(_normal_truncada(mu, sigma, low, high), DECIMALES_POR_DEFECTO)


def jitter_point(x: int, y: int, sigma_px: float = 2.0) -> tuple[int, int]:
    """Desplaza un punto con ruido gaussiano (en píxeles)."""
    return (
        int(round(x + sigma_px * normal_estandar())),
        int(round(y + sigma_px * normal_estandar())),
    )
