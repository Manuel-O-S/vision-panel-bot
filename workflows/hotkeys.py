"""
hotkeys.py - Atajos de teclado globales (funcionan aunque el panel no tenga el foco).

Por defecto
    Ctrl + Shift + Alt + P  -> Play / Pausa
    Ctrl + Alt + K          -> Pánico
    Esc                     -> Pánico (desactivable)

Los callbacks se ejecutan en el hilo del listener de pynput, NO en el de la GUI:
    - ``on_panic`` debe ser thread-safe y rápido (p. ej. ``engine.panic``).
    - ``on_toggle`` normalmente sólo encola una orden que la GUI procesa en su hilo.

Las pulsaciones **inyectadas** (las que genera el propio bot con SendInput) se
ignoran: si una acción pulsa Esc, no dispara el pánico.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from pynput import keyboard

log = logging.getLogger(__name__)

__all__ = ["DEFAULT_TOGGLE", "DEFAULT_PANIC", "ESC_PANIC", "HotkeyManager", "describe"]

DEFAULT_TOGGLE = "<ctrl>+<shift>+<alt>+p"
DEFAULT_PANIC = "<ctrl>+<alt>+k"
ESC_PANIC = "<esc>"


def describe(combo: str) -> str:
    """'<ctrl>+<alt>+k' -> 'Ctrl + Alt + K' (para mostrar en la GUI)."""
    partes = [p.strip("<>") for p in combo.split("+")]
    return " + ".join(p.capitalize() if len(p) > 1 else p.upper() for p in partes)


class HotkeyManager:
    def __init__(
        self,
        on_toggle: Callable[[], None],
        on_panic: Callable[[], None],
        toggle: str = DEFAULT_TOGGLE,
        panic: str = DEFAULT_PANIC,
        esc_panic: bool = True,
        ignore_injected: bool = True,
    ) -> None:
        self._on_toggle = on_toggle
        self._on_panic = on_panic
        self.toggle_combo = toggle
        self.panic_combo = panic
        self.ignore_injected = ignore_injected
        self._esc_enabled = threading.Event()
        if esc_panic:
            self._esc_enabled.set()
        # Valida las combinaciones ya en el constructor (error temprano y claro).
        self._hotkeys = [
            keyboard.HotKey(keyboard.HotKey.parse(toggle), self._safe(on_toggle, "play/pausa")),
            keyboard.HotKey(keyboard.HotKey.parse(panic), self._safe(on_panic, "pánico")),
        ]
        self._esc = keyboard.HotKey(keyboard.HotKey.parse(ESC_PANIC), self._safe(on_panic, "pánico (Esc)"))
        self._listener: keyboard.Listener | None = None

    @staticmethod
    def _safe(cb: Callable[[], None], nombre: str) -> Callable[[], None]:
        def _wrapped() -> None:
            log.info("Hotkey: %s", nombre)
            try:
                cb()
            except Exception:  # noqa: BLE001 - una excepción mataría el listener
                log.exception("Error en el hotkey %s", nombre)
        return _wrapped

    @property
    def esc_panic(self) -> bool:
        return self._esc_enabled.is_set()

    @esc_panic.setter
    def esc_panic(self, value: bool) -> None:
        (self._esc_enabled.set if value else self._esc_enabled.clear)()

    def _all(self) -> list[keyboard.HotKey]:
        return self._hotkeys + ([self._esc] if self._esc_enabled.is_set() else [])

    # Los callbacks reciben (key, injected) en pynput >= 1.8.
    def _press(self, key: keyboard.Key | keyboard.KeyCode | None, injected: bool = False) -> None:
        if key is None or (injected and self.ignore_injected) or self._listener is None:
            return
        k = self._listener.canonical(key)
        for hk in self._all():
            hk.press(k)

    def _release(self, key: keyboard.Key | keyboard.KeyCode | None, injected: bool = False) -> None:
        if key is None or (injected and self.ignore_injected) or self._listener is None:
            return
        k = self._listener.canonical(key)
        for hk in self._hotkeys + [self._esc]:
            hk.release(k)

    def start(self) -> None:
        if self._listener is not None:
            return
        self._listener = keyboard.Listener(on_press=self._press, on_release=self._release)
        self._listener.daemon = True
        self._listener.start()
        log.info("Hotkeys activos: %s (play/pausa), %s (pánico)%s",
                 describe(self.toggle_combo), describe(self.panic_combo),
                 ", Esc (pánico)" if self.esc_panic else "")

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
