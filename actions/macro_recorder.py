"""
macro_recorder.py - Grabación y reproducción de macros de ratón y teclado.

Formato (.macro, JSON):
    {
      "version": 1,
      "ventana": [1920, 1080],          # tamaño de la ventana objetivo al grabar
      "eventos": [
        {"t": 0.4123, "tipo": "click",  "rx": 0.25, "ry": 0.6, "boton": "left", "pulsado": true},
        {"t": 0.5310, "tipo": "click",  "rx": 0.25, "ry": 0.6, "boton": "left", "pulsado": false},
        {"t": 1.0021, "tipo": "scroll", "rx": 0.5,  "ry": 0.5, "dy": -2},
        {"t": 1.2000, "tipo": "tecla",  "tecla": "a", "pulsado": true}
      ]
    }

- Las posiciones son RELATIVAS a la ventana objetivo (0.0-1.0), igual que las
  zonas: la macro funciona aunque la ventana se mueva o cambie de resolución.
- Las teclas se guardan con nombres de pydirectinput ("a", "space", "ctrlleft"...).
- Se ignoran los eventos inyectados (los que genera el propio bot).
- F10 termina la grabación (no se graba).
- La reproducción respeta los tiempos originales con una variación entrópica
  de ±10 % y, pase lo que pase (pánico incluido), suelta todas las teclas y
  botones que hayan quedado pulsados.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.entropy import generar_delay_entropico
from core.geometry import Rect

log = logging.getLogger(__name__)

__all__ = ["MACRO_VERSION", "Macro", "MacroRecorder", "MacroError", "play_macro", "key_name"]

MACRO_VERSION = 1
_JITTER = 0.10  # ±10 % sobre cada intervalo al reproducir

# pynput Key.name -> nombre de pydirectinput
_SPECIAL_KEYS = {
    "alt": "alt", "alt_l": "altleft", "alt_r": "altright", "alt_gr": "altright",
    "backspace": "backspace", "caps_lock": "capslock", "cmd": "winleft", "cmd_r": "winright",
    "ctrl": "ctrl", "ctrl_l": "ctrlleft", "ctrl_r": "ctrlright", "delete": "delete",
    "down": "down", "end": "end", "enter": "enter", "esc": "esc", "home": "home",
    "left": "left", "page_down": "pagedown", "page_up": "pageup", "right": "right",
    "shift": "shift", "shift_l": "shiftleft", "shift_r": "shiftright", "space": "space",
    "tab": "tab", "up": "up", "insert": "insert", "menu": "apps", "num_lock": "numlock",
    "pause": "pause", "print_screen": "printscreen", "scroll_lock": "scrolllock",
    **{f"f{i}": f"f{i}" for i in range(1, 13)},
}
_SYMBOLS = set("'`,-./;=[\\]")


class MacroError(ValueError):
    pass


def key_name(key: Any) -> str | None:
    """Tecla de pynput -> nombre de pydirectinput (``None`` si no se puede reproducir).

    Se usa el código virtual (vk) cuando existe: con Shift o Ctrl pulsados el
    carácter cambia ('!' o '\\x01') pero el vk sigue siendo el de la tecla física.
    """
    nombre = getattr(key, "name", None)
    if nombre:  # Key especial
        return _SPECIAL_KEYS.get(nombre)
    vk = getattr(key, "vk", None)
    if vk is not None:
        if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
            return chr(vk).lower()
        if 0x60 <= vk <= 0x69:  # teclado numérico
            return str(vk - 0x60)
    char = getattr(key, "char", None)
    if char and len(char) == 1 and (char.isalnum() or char in _SYMBOLS):
        return char.lower()
    return None


@dataclass
class Macro:
    window_size: tuple[int, int]
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.events[-1]["t"] if self.events else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"version": MACRO_VERSION, "ventana": list(self.window_size), "eventos": self.events}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Macro:
        if data.get("version") != MACRO_VERSION:
            raise MacroError(f"Versión de macro no soportada: {data.get('version')}")
        w, h = data["ventana"]
        eventos = list(data.get("eventos", []))
        for ev in eventos:
            if ev.get("tipo") not in ("click", "scroll", "tecla") or "t" not in ev:
                raise MacroError(f"Evento inválido: {ev}")
        return cls((int(w), int(h)), eventos)

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> Macro:
        try:
            return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, MacroError):
                raise
            raise MacroError(f"No se pudo leer la macro {Path(path).name}: {exc}") from exc


# =========================================================================== #
# Grabación
# =========================================================================== #
class MacroRecorder:
    """Graba clics, rueda y teclas. F10 (o ``stop()``) termina.

    ``on_stop`` se llama desde el hilo de pynput al terminar con F10.
    """

    def __init__(self, window: Rect, on_stop: Callable[[], None] | None = None) -> None:
        self.window = window
        self.on_stop = on_stop
        self.events: list[dict[str, Any]] = []
        self._t0 = 0.0
        self._lock = threading.Lock()
        self._mouse = None
        self._kb = None

    @property
    def recording(self) -> bool:
        return self._kb is not None

    def _t(self) -> float:
        return round(time.perf_counter() - self._t0, 4)

    def _rel(self, x: int, y: int) -> tuple[float, float]:
        return (round((x - self.window.left) / self.window.width, 5),
                round((y - self.window.top) / self.window.height, 5))

    def _add(self, ev: dict[str, Any]) -> None:
        with self._lock:
            self.events.append(ev)

    def start(self) -> None:
        from pynput import keyboard, mouse
        self.events = []
        self._t0 = time.perf_counter()
        self._mouse = mouse.Listener(on_click=self._on_click, on_scroll=self._on_scroll)
        self._kb = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self._mouse.start()
        self._kb.start()
        log.info("Grabando macro (F10 para terminar)")

    def stop(self) -> Macro:
        for listener in (self._mouse, self._kb):
            if listener is not None:
                listener.stop()
        self._mouse = self._kb = None
        with self._lock:
            return Macro(self.window.size, list(self.events))

    # ----------------------- callbacks (hilo pynput) ---------------------- #
    def _on_click(self, x: int, y: int, button: Any, pressed: bool, injected: bool = False) -> None:
        if injected or button.name not in ("left", "right", "middle"):
            return
        rx, ry = self._rel(x, y)
        self._add({"t": self._t(), "tipo": "click", "rx": rx, "ry": ry,
                   "boton": button.name, "pulsado": pressed})

    def _on_scroll(self, x: int, y: int, dx: int, dy: int, injected: bool = False) -> None:
        if injected or not dy:
            return
        rx, ry = self._rel(x, y)
        self._add({"t": self._t(), "tipo": "scroll", "rx": rx, "ry": ry, "dy": int(dy)})

    def _on_press(self, key: Any, injected: bool = False) -> bool | None:
        from pynput import keyboard
        if key == keyboard.Key.f10:
            if self.on_stop is not None:
                threading.Thread(target=self.on_stop, daemon=True).start()
            return False  # detiene el listener de teclado
        if not injected:
            nombre = key_name(key)
            if nombre is None:
                log.warning("Tecla no reproducible ignorada: %s", key)
            else:
                self._add({"t": self._t(), "tipo": "tecla", "tecla": nombre, "pulsado": True})
        return None

    def _on_release(self, key: Any, injected: bool = False) -> None:
        from pynput import keyboard
        if injected or key == keyboard.Key.f10:
            return
        nombre = key_name(key)
        if nombre is not None:
            self._add({"t": self._t(), "tipo": "tecla", "tecla": nombre, "pulsado": False})


# =========================================================================== #
# Reproducción
# =========================================================================== #
def play_macro(macro: Macro, window: Rect, stop_event: threading.Event,
               speed: float = 1.0, humanize: bool = True, input_module: Any = None) -> bool:
    """Reproduce la macro sobre ``window``. Devuelve False si se interrumpió.

    ``input_module`` permite inyectar un simulador (tests); por defecto
    ``core.input_sim`` (SendInput).
    """
    if speed <= 0:
        raise MacroError("La velocidad debe ser > 0")
    if input_module is None:
        from core import input_sim as input_module

    def abs_pos(ev: dict[str, Any]) -> tuple[int, int]:
        return (window.left + int(round(float(ev["rx"]) * window.width)),
                window.top + int(round(float(ev["ry"]) * window.height)))

    teclas: set[str] = set()
    botones: dict[str, tuple[int, int]] = {}
    previo = 0.0
    try:
        for ev in macro.events:
            dt = max(0.0, (float(ev["t"]) - previo) / speed)
            previo = float(ev["t"])
            if humanize and dt > 0:
                dt = generar_delay_entropico(dt * (1 - _JITTER), dt * (1 + _JITTER))
            if stop_event.wait(dt) if dt > 0 else stop_event.is_set():
                return False

            tipo = ev["tipo"]
            if tipo == "click":
                x, y = abs_pos(ev)
                boton = ev.get("boton", "left")
                if ev.get("pulsado", True):
                    input_module.mouse_down(x, y, boton)
                    botones[boton] = (x, y)
                else:
                    input_module.mouse_up(x, y, boton)
                    botones.pop(boton, None)
            elif tipo == "scroll":
                x, y = abs_pos(ev)
                input_module.scroll(x, y, int(ev.get("dy", 0)))
            elif tipo == "tecla":
                if ev.get("pulsado", True):
                    input_module.key_down(ev["tecla"])
                    teclas.add(ev["tecla"])
                else:
                    input_module.key_up(ev["tecla"])
                    teclas.discard(ev["tecla"])
        return True
    finally:
        # Nunca dejar teclas o botones "pegados" (pánico, error o fin de macro).
        for tecla in teclas:
            try:
                input_module.key_up(tecla)
            except Exception:  # noqa: BLE001
                log.exception("No se pudo soltar la tecla %s", tecla)
        for boton, (x, y) in botones.items():
            try:
                input_module.mouse_up(x, y, boton)
            except Exception:  # noqa: BLE001
                log.exception("No se pudo soltar el botón %s", boton)
