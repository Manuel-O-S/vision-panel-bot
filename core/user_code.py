"""
user_code.py - Ejecución del código Python que el usuario inserta en una acción.

El código corre en el HILO DEL MOTOR (nunca en el de la GUI) dentro de un
try-except: un error de sintaxis o de ejecución se convierte en
``UserCodeError`` con el número de línea, se registra y el motor sigue.

IMPORTANTE: no es un sandbox. El código tiene los mismos permisos que la
aplicación (igual que un script propio). No pegues código de terceros que no
entiendas.

API disponible dentro del código
    ventana               Rect del área cliente (left, top, width, height)
    zonas                 {"Zona 1": Rect, ...} en píxeles actuales
    obs                   {"Zona 1": ZoneObservation} (delta, is_baseline, scores...)
    regla                 nombre de la regla que disparó la acción
    clic(x, y, boton="left")          clic en píxeles absolutos
    clic_rel(rx, ry, zona=None)       clic relativo (0-1) a una zona o a la ventana
    tecla("space")  /  escribir("hola")
    esperar(min, max)     delay entrópico interrumpible; False si se pidió parar
    debe_parar()          True tras Detener/Pánico: úsalo en tus bucles
    delay_entropico(min, max)  -> float (4 decimales)
    log("mensaje")        aparece en la pestaña Registro
    math, time            módulos estándar
"""
from __future__ import annotations

import math
import time
import traceback
from collections import OrderedDict
from collections.abc import Callable
from threading import Lock
from types import CodeType
from typing import Any

from core.entropy import dormir_entropico, generar_delay_entropico

__all__ = ["UserCodeError", "check_syntax", "run_user_code", "FILENAME", "SNIPPETS"]

FILENAME = "<tu código>"
_CACHE_MAX = 64
_cache: OrderedDict[str, CodeType] = OrderedDict()
_cache_lock = Lock()

SNIPPETS: dict[str, str] = {
    "Clic en el centro de una zona": 'clic_rel(0.5, 0.5, zona="Zona 1")\n',
    "Pulsar teclas con pausa": 'for t in ("w", "a", "s", "d"):\n'
                               '    tecla(t)\n'
                               '    if not esperar(0.2, 0.5):\n'
                               '        break\n',
    "Actuar según la similitud": 'o = obs.get("Zona 2")\n'
                                 'if o and o.scores.get("Plantilla B", 0) > 0.9:\n'
                                 '    log("Plantilla B muy clara")\n'
                                 '    tecla("enter")\n',
    "Bucle que respeta el pánico": 'while not debe_parar():\n'
                                   '    clic_rel(0.5, 0.5)\n'
                                   '    esperar(1.0, 2.0)\n',
}


class UserCodeError(RuntimeError):
    def __init__(self, message: str, line: int | None = None) -> None:
        super().__init__(message)
        self.line = line


def _compile(source: str) -> CodeType:
    with _cache_lock:
        code = _cache.get(source)
        if code is not None:
            _cache.move_to_end(source)
            return code
    code = compile(source, FILENAME, "exec")
    with _cache_lock:
        _cache[source] = code
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return code


def check_syntax(source: str) -> UserCodeError | None:
    """``None`` si el código compila; si no, el error con su línea."""
    try:
        _compile(source)
    except SyntaxError as exc:
        return UserCodeError(f"Línea {exc.lineno}: {exc.msg}", exc.lineno)
    return None


def _user_line(exc: BaseException) -> int | None:
    """Línea del código del usuario donde ocurrió la excepción."""
    for frame in reversed(traceback.extract_tb(exc.__traceback__)):
        if frame.filename == FILENAME:
            return frame.lineno
    return None


def run_user_code(source: str, namespace: dict[str, Any]) -> None:
    """Compila (con caché) y ejecuta. Cualquier fallo -> ``UserCodeError``."""
    if not source.strip():
        return
    try:
        code = _compile(source)
    except SyntaxError as exc:
        raise UserCodeError(f"Error de sintaxis en la línea {exc.lineno}: {exc.msg}", exc.lineno) from None
    try:
        exec(code, namespace)  # noqa: S102 - ejecución intencionada del código del usuario
    except BaseException as exc:  # noqa: BLE001 - incluso exit()/SystemExit no debe matar el hilo
        linea = _user_line(exc)
        donde = f" (línea {linea})" if linea else ""
        raise UserCodeError(f"Error en tu código{donde}: {type(exc).__name__}: {exc}", linea) from None


def build_namespace(ctx: Any, *, click: Callable[[int, int, str], None],
                    key: Callable[[str], None], write: Callable[[str], None],
                    log: Callable[[str], None]) -> dict[str, Any]:
    """Construye la API en español que ve el código del usuario."""
    stop = ctx.stop_event

    def clic(x: int, y: int, boton: str = "left") -> None:
        if not stop.is_set():
            click(int(x), int(y), boton)

    def clic_rel(rx: float, ry: float, zona: str | None = None, boton: str = "left") -> None:
        r = ctx.window if zona is None else ctx.zones.get(zona)
        if r is None:
            raise ValueError(f"Zona desconocida: {zona!r}")
        clic(r.left + round(rx * r.width), r.top + round(ry * r.height), boton)

    def tecla(nombre: str) -> None:
        if not stop.is_set():
            key(str(nombre))

    def escribir(texto: str) -> None:
        if not stop.is_set():
            write(str(texto))

    return {
        "__builtins__": __builtins__,
        "__name__": "__tu_codigo__",
        "ventana": ctx.window,
        "zonas": dict(ctx.zones),
        "obs": dict(ctx.observations),
        "regla": ctx.rule,
        "clic": clic,
        "clic_rel": clic_rel,
        "tecla": tecla,
        "escribir": escribir,
        "esperar": lambda a, b: dormir_entropico(float(a), float(b), stop),
        "debe_parar": stop.is_set,
        "delay_entropico": generar_delay_entropico,
        "log": lambda msg: log(str(msg)),
        "math": math,
        "time": time,
    }
