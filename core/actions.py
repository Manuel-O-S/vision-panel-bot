"""
actions.py - Ejecutor de acciones que disparan las reglas.

Una acción es un dict serializable (va tal cual en el .tarea):

    {"tipo": "click",   "zona": "Zona 1", "x": 0.5, "y": 0.5, "boton": "left", "jitter": 2}
    {"tipo": "click",   "zona": "Zona 2", "plantilla": "Plantilla B"}   # centro del match
    {"tipo": "click",   "x": 0.25, "y": 0.80}                           # relativo a la ventana
    {"tipo": "click",   "raton": True}            # donde esté el ratón (no lo mueve)
    {"tipo": "mantener",  "zona": "Zona 1", "boton": "left", "min": 0.5, "max": 1.0}  # mantener pulsado
    {"tipo": "autoclick", "zona": "Zona 1", "boton": "right", "cps": 10, "min": 2, "max": 3}
    {"tipo": "tecla",   "tecla": "space"}
    {"tipo": "texto",   "texto": "hola"}
    {"tipo": "esperar", "min": 0.2, "max": 0.6}                         # delay entrópico
    {"tipo": "macro",   "archivo": "macros/login.macro", "velocidad": 1.0}  # macro grabada
    {"tipo": "accion",  "nombre": "A1"}            # invoca una acción de la biblioteca
    {"tipo": "codigo",  "codigo": "tecla('enter')"}  # código Python del usuario
    {"tipo": "exe",     "ruta": "C:/ruta/programa.exe"}                 # reservado (V2)

El motor llama a ``runner(accion, contexto)`` desde su hilo de trabajo. Se
puede inyectar cualquier callable con esa firma (p. ej. un ejecutor "en seco"
para pruebas o el futuro módulo de workflows).
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.entropy import dormir_entropico, generar_delay_entropico
from core.geometry import Rect, to_pixel
from core.rules import ZoneObservation

log = logging.getLogger(__name__)

__all__ = ["ActionContext", "ActionRunner", "ActionError", "DefaultActionRunner", "DryRunActionRunner",
           "expand_actions", "MAX_ACTION_DEPTH"]

MAX_ACTION_DEPTH = 8
MAX_CPS = 30.0
_BUTTONS = ("left", "right", "middle")


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
    log: Callable[[str], None] = field(default=lambda _m: None)  # -> pestaña Registro


ActionRunner = Callable[[Mapping[str, Any], ActionContext], None]


def expand_actions(actions: Sequence[Mapping[str, Any]],
                   library: Mapping[str, Sequence[Mapping[str, Any]]],
                   _stack: tuple[str, ...] = ()) -> Iterator[Mapping[str, Any]]:
    """Sustituye cada {"tipo": "accion", "nombre": "A1"} por las acciones de A1.

    Detecta referencias circulares (A1 -> A2 -> A1) y anidamiento excesivo.
    """
    for accion in actions:
        if str(accion.get("tipo", "")).casefold() != "accion":
            yield accion
            continue
        nombre = accion.get("nombre")
        if nombre not in library:
            raise ActionError(f"La acción {nombre!r} no existe en la biblioteca")
        if nombre in _stack:
            raise ActionError("Referencia circular: " + " → ".join((*_stack, nombre)))
        if len(_stack) >= MAX_ACTION_DEPTH:
            raise ActionError(f"Demasiado anidamiento de acciones (máx. {MAX_ACTION_DEPTH})")
        yield from expand_actions(library[nombre], library, (*_stack, nombre))


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
        if zona:
            obs = ctx.observations.get(zona)
            loc = obs.locations.get(plantilla) if obs else None
            if loc is None:
                raise ActionError(f"Sin coincidencia de {plantilla!r} en {zona!r}")
        else:
            # Sin zona: usar la zona donde la plantilla se detectó en este ciclo.
            candidatas = [o for o in ctx.observations.values() if plantilla in o.locations]
            mejor = max(candidatas, key=lambda o: o.scores.get(plantilla, -1.0), default=None)
            loc = mejor.locations[plantilla] if mejor else None
            if loc is None:
                raise ActionError(f"No se ve {plantilla!r} en ninguna zona ahora mismo "
                                  "(¿alguna regla la usa en su condición?)")
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
            "mantener": self._hold,
            "autoclick": self._autoclick,
            "tecla": self._key,
            "texto": self._text,
            "esperar": self._wait,
            "macro": self._macro,
            "codigo": self._code,
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

    @staticmethod
    def _button(action: Mapping[str, Any]) -> str:
        boton = str(action.get("boton", "left"))
        if boton not in _BUTTONS:
            raise ActionError(f"Botón inválido: {boton!r}")
        return boton

    @staticmethod
    def _range(action: Mapping[str, Any], lo: float, hi: float) -> tuple[float, float]:
        a, b = float(action.get("min", lo)), float(action.get("max", hi))
        if not 0 <= a <= b:
            raise ActionError(f"Rango de duración inválido: mín {a} / máx {b}")
        return a, b

    def _aim(self, action: Mapping[str, Any], ctx: ActionContext) -> str:
        """Lleva el ratón al punto de la acción, salvo con ``"raton": True`` (se queda donde está).

        Devuelve dónde actuó, para el registro.
        """
        if action.get("raton"):
            return "donde está el ratón"
        x, y = resolve_point(action, ctx)
        entrada = self._input()
        entrada.move(x, y, float(action.get("jitter", 2.0)))
        entrada.human_delay()
        return f"en ({x}, {y})"

    def _click(self, action: Mapping[str, Any], ctx: ActionContext) -> None:
        boton = self._button(action)
        if action.get("raton"):
            self._input().click_here(boton)
            return
        x, y = resolve_point(action, ctx)
        self._input().click(x, y, button=boton, jitter_px=float(action.get("jitter", 2.0)))

    def _hold(self, action: Mapping[str, Any], ctx: ActionContext) -> None:
        """Mantiene pulsado el botón un tiempo entrópico entre ``min`` y ``max`` segundos."""
        boton = self._button(action)
        lo, hi = self._range(action, 0.5, 1.0)
        donde = self._aim(action, ctx)
        entrada = self._input()
        inicio = time.perf_counter()
        entrada.button_down(boton)
        try:
            dormir_entropico(lo, hi, ctx.stop_event)  # el pánico lo corta al instante
        finally:
            entrada.button_up(boton)  # nunca dejar el botón pegado
        ctx.log(f"Mantener {boton} {time.perf_counter() - inicio:.2f} s {donde}")

    def _autoclick(self, action: Mapping[str, Any], ctx: ActionContext) -> None:
        """Ráfaga de clics a ~``cps`` por segundo durante ``min``-``max`` segundos.

        Cada intervalo y cada pulsación son entrópicos (±30 % alrededor de 1/cps),
        así que el ritmo medio es ``cps`` pero nunca hay dos clics iguales.
        """
        boton = self._button(action)
        cps = float(action.get("cps", 10))
        if not 0 < cps <= MAX_CPS:
            raise ActionError(f"'cps' debe estar entre 1 y {MAX_CPS:g}: {cps}")
        lo, hi = self._range(action, 1.0, 2.0)
        duracion = generar_delay_entropico(lo, hi)
        base = 1.0 / cps
        donde = self._aim(action, ctx)
        entrada = self._input()

        clics = 0
        inicio = proximo = time.perf_counter()
        fin = inicio + duracion
        while not ctx.stop_event.is_set() and time.perf_counter() < fin:
            intervalo = generar_delay_entropico(0.7 * base, 1.3 * base)
            pulsado = generar_delay_entropico(min(0.012, 0.3 * intervalo), min(0.04, 0.5 * intervalo))
            entrada.button_down(boton)
            try:
                time.sleep(pulsado)
            finally:
                entrada.button_up(boton)
            clics += 1
            proximo += intervalo
            espera = proximo - time.perf_counter()
            if espera > 0:
                ctx.stop_event.wait(espera)
            else:
                proximo = time.perf_counter()  # si vamos tarde, no "recuperar" con ráfagas
        real = time.perf_counter() - inicio
        ctx.log(f"Autoclick {boton}: {clics} clics en {real:.2f} s (~{clics / real if real else 0:.1f} cps) {donde}")

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

    def _code(self, action: Mapping[str, Any], ctx: ActionContext) -> None:
        """Código Python del usuario, aislado en try-except (ver core/user_code.py)."""
        from core.user_code import UserCodeError, build_namespace, run_user_code
        entrada = self._input()
        ns = build_namespace(
            ctx,
            click=lambda x, y, b: entrada.click(x, y, button=b, jitter_px=0),
            key=entrada.press,
            write=entrada.type_text,
            log=ctx.log,
        )
        try:
            run_user_code(str(action.get("codigo", "")), ns)
        except UserCodeError as exc:
            raise ActionError(str(exc)) from None

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
