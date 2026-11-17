"""
engine.py - Bucle principal de ejecución, multithreading y control de estados.
"""
from __future__ import annotations

import logging
import threading
from enum import Enum, auto
from typing import Callable

log = logging.getLogger(__name__)


class EngineState(Enum):
    IDLE = auto()
    RUNNING = auto()
    PAUSED = auto()
    STOPPING = auto()


class Engine:
    """Ejecuta un flujo de trabajo en un hilo dedicado con control de estados."""

    def __init__(self, tick_interval: float = 0.05) -> None:
        self.tick_interval = tick_interval
        self._state = EngineState.IDLE
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # set = no pausado
        self._thread: threading.Thread | None = None
        self._listeners: list[Callable[[EngineState], None]] = []

    # ----------------------------- estado ----------------------------- #
    @property
    def state(self) -> EngineState:
        with self._lock:
            return self._state

    def _set_state(self, new_state: EngineState) -> None:
        with self._lock:
            self._state = new_state
        for cb in list(self._listeners):
            try:
                cb(new_state)
            except Exception:  # noqa: BLE001
                log.exception("Error en listener de estado")

    def add_state_listener(self, callback: Callable[[EngineState], None]) -> None:
        self._listeners.append(callback)

    # ---------------------------- control ----------------------------- #
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._pause_event.set()
        self._thread = threading.Thread(target=self._run, name="EngineLoop", daemon=True)
        self._thread.start()
        self._set_state(EngineState.RUNNING)

    def pause(self) -> None:
        self._pause_event.clear()
        self._set_state(EngineState.PAUSED)

    def resume(self) -> None:
        self._pause_event.set()
        self._set_state(EngineState.RUNNING)

    def stop(self, timeout: float = 2.0) -> None:
        self._set_state(EngineState.STOPPING)
        self._stop_event.set()
        self._pause_event.set()
        if self._thread:
            self._thread.join(timeout)
        self._set_state(EngineState.IDLE)

    # ----------------------------- bucle ------------------------------ #
    def _run(self) -> None:
        log.info("Engine iniciado")
        while not self._stop_event.is_set():
            self._pause_event.wait()
            if self._stop_event.is_set():
                break
            try:
                self.step()
            except Exception:  # noqa: BLE001
                log.exception("Error en step()")
            self._stop_event.wait(self.tick_interval)
        log.info("Engine detenido")

    def step(self) -> None:
        """Un ciclo: capturar -> detectar -> decidir -> actuar. TODO: implementar."""
        pass
