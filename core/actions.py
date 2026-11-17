"""
actions.py - Ejecutor de acciones que disparan las reglas.

Una acción es un dict serializable (va tal cual en el .tarea):

    {"tipo": "click",   "zona": "Zona 1", "x": 0.5, "y": 0.5, "boton": "left", "jitter": 2}
    {"tipo": "click",   "zona": "Zona 2", "plantilla": "Plantilla B"}   # centro del match
    {"tipo": "click",   "x": 0.25, "y": 0.80}                           # relativo a la ventana
    {"tipo": "tecla",   "tecla": "space"}
    {"tipo": "texto",   "texto": "hola"}
    {"tipo": "esperar", "min": 0.2, "max": 0.6}                         # delay entrópico
    {"tipo": "macro",   "archivo": "macros/login.macro", "velocidad": 1.0}  # macro grabada
    {"tipo": "exe",     "ruta": "C:/ruta/programa.exe"}                 # reservado (V2)

El motor llama a ``runner(accion, contexto)`` desde su hilo de trabajo. Se
puede inyectar cualquier callable con esa firma (p. ej. un ejecutor "en seco"
para pruebas o el futuro módulo de workflows).
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.entropy import dormir_entropico
from core.geometry import Rect, to_pixel
from core.rules import ZoneObservation

log = logging.getLogger(__name__)

__all__ = ["ActionContext", "ActionRunner", "ActionError", "DefaultActionRunner", "DryRunActionRunner"]


class ActionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ActionContext:
    """Estado del ciclo en el que se disparó la regla."""

    window: Rect                                 # área cliente actual
    zones: Mapping[str, Rect]                    # zona -> Rect absoluto actual
    observations: Mapping[str, ZoneObservation]  # lo que se vio en el ciclo
    stop_event: threading.Event                  # activo => abortar cuanto antes
    rule: str                                    # regla que disparó la acción


ActionRunner = Callable[[Mapping[str, Any], ActionContext], None]


def _rel(action: Mapping[str, Any], key: str, default: float) -> float:
    v = float(action.get(key, default))
    if not 0.0 <= v <= 1.0:
        raise ActionError(f"'{key}' debe estar entre 0.0 y 1.0: {v}")
    return v


def resolve_point(action: Mapping[str, Any], ctx: ActionContext) -> tuple[int, int]:
    """Punto absoluto en píxeles para una acción de ratón."""
    zona = action.get("zona")
    plantilla = action.get("plantilla")

    if plantilla:
        if not zona:
            raise ActionError("Un click sobre 'plantilla' necesita 'zona'")
        obs = ctx.observations.get(zona)
        loc = obs.locations.get(plantilla) if obs else None
        if loc is None:
            raise ActionError(f"Sin coincidencia de {plantilla!r} en {zona!r}")
        x, y, w, h = loc
        return x + w // 2, y + h // 2

    if zona:
        r = ctx.zones.get(zona)
        if r is None:
            raise ActionError(f"Zona desconocida: {zona!r}")
    else:
        r = ctx.window
    return (r.left + to_pixel(_rel(action, "x", 0.5), r.width),
            r.top + to_pixel(_rel(action, "y", 0.5), r.height))


class DefaultActionRunner:
    """Ejecuta acciones con ``core.input_sim`` (SendInput vía pydirectinput)."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        self.base_dir = Path(base_dir) if base_dir else Path.cwd()  # raíz de rutas relativas
        self._handlers: dict[str, Callable[[Mapping[str, Any], ActionContext], None]] = {
            "click": self._click,
            "tecla": self._key,
            "texto": self._text,
            "esperar": self._wait,
            "macro": self._macro,
            "exe": self._exe,
        }

    def __call__(self, action: Mapping[str, Any], ctx: ActionContext) -> None:
        if ctx.stop_event.is_set():
            return
        tipo = str(action.get("tipo", "")).strip().casefold()
        handler = self._handlers.get(tipo)
        if handler is None:
            raise ActionError(f"Tipo de acción desconocido: {action.get('tipo')!r}")
        handler(action, ctx)

    # El import es perezoso: pydirectinput sólo se carga si de verdad se usa
    # (permite ejecutar el motor en seco y los tests sin esa dependencia).
    @staticmethod
    def _input():  # noqa: ANN205
        from core import input_sim
        return input_sim

    def _click(self, action: Mapping[str, Any], ctx: ActionContext) -> None:
        x, y = resolve_point(action, ctx)
        boton = str(action.get("boton", "left"))
        if boton not in ("left", "right", "middle"):
            raise ActionError(f"Botón inválido: {boton!r}")
        self._input().click(x, y, button=boton, jitter_px=float(action.get("jitter", 2.0)))

    def _key(self, action: Mapping[str, Any], ctx: ActionContext) -> None:
        tecla = action.get("tecla")
        if not tecla:
            raise ActionError("Acción 'tecla' sin 'tecla'")
        self._input().press(str(tecla))

    def _text(self, action: Mapping[str, Any], ctx: ActionContext) -> None:
        self._input().type_text(str(action.get("texto", "")))

    def _wait(self, action: Mapping[str, Any], ctx: ActionContext) -> None:
        dormir_entropico(float(action.get("min", 0.0)), float(action.get("max", 0.0)), ctx.stop_event)

    def _macro(self, action: Mapping[str, Any], ctx: ActionContext) -> None:
        from actions.macro_recorder import Macro, play_macro
        archivo = action.get("archivo")
        if not archivo:
            raise ActionError("Acción 'macro' sin 'archivo'")
        ruta = Path(archivo)
        if not ruta.is_absolute():
            ruta = self.base_dir / ruta
        macro = Macro.load(ruta)
        play_macro(macro, ctx.window, ctx.stop_event, speed=float(action.get("velocidad", 1.0)))

    def _exe(self, action: Mapping[str, Any], ctx: ActionContext) -> None:
        # Contenedor reservado para acciones basadas en ejecutables externos.
        # Aislado: cualquier fallo aquí se registra y NUNCA tumba el motor.
        try:
            raise NotImplementedError("Las acciones .exe llegarán en una versión futura")
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] Acción .exe ignorada (%s): %s", ctx.rule, action.get("ruta"), exc)


class DryRunActionRunner:
    """No toca el ratón ni el teclado: sólo registra. Útil para probar reglas."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, action: Mapping[str, Any], ctx: ActionContext) -> None:
        self.executed.append((ctx.rule, dict(action)))
        log.info("[seco] %s -> %s", ctx.rule, dict(action))
