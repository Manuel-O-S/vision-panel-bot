"""
macro_recorder.py - Grabador y reproductor de entradas de ratón y teclado (pynput).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from pynput import keyboard, mouse


class MacroRecorder:
    """Graba eventos con marca de tiempo relativa. Tecla F10 detiene la grabación."""

    STOP_KEY = keyboard.Key.f10

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._t0 = 0.0
        self._mouse: mouse.Listener | None = None
        self._kb: keyboard.Listener | None = None

    def _t(self) -> float:
        return round(time.perf_counter() - self._t0, 4)

    def start(self) -> None:
        self.events.clear()
        self._t0 = time.perf_counter()
        self._mouse = mouse.Listener(on_click=self._on_click, on_scroll=self._on_scroll)
        self._kb = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self._mouse.start()
        self._kb.start()

    def stop(self) -> None:
        for listener in (self._mouse, self._kb):
            if listener:
                listener.stop()

    # ------------------------- callbacks ------------------------- #
    def _on_click(self, x: int, y: int, button: mouse.Button, pressed: bool) -> None:
        self.events.append({"t": self._t(), "type": "click", "x": x, "y": y,
                            "button": button.name, "pressed": pressed})

    def _on_scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        self.events.append({"t": self._t(), "type": "scroll", "x": x, "y": y, "dx": dx, "dy": dy})

    def _on_press(self, key: keyboard.Key | keyboard.KeyCode) -> bool | None:
        if key == self.STOP_KEY:
            self.stop()
            return False
        self.events.append({"t": self._t(), "type": "key_down", "key": str(key)})
        return None

    def _on_release(self, key: keyboard.Key | keyboard.KeyCode) -> None:
        self.events.append({"t": self._t(), "type": "key_up", "key": str(key)})

    # ------------------------ persistencia ----------------------- #
    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.events, indent=2), encoding="utf-8")

    @staticmethod
    def load(path: str | Path) -> list[dict[str, Any]]:
        return json.loads(Path(path).read_text(encoding="utf-8"))

    # TODO: play(events) usando core.input_sim respetando los tiempos "t"
