"""
input_sim.py - Simulación de entrada de hardware (pydirectinput / SendInput).
"""
from __future__ import annotations

import ctypes
import sys
import time

import pydirectinput

from core import entropy

pydirectinput.PAUSE = 0  # los retardos los gestiona este módulo
pydirectinput.FAILSAFE = True  # mover el ratón a la esquina (0,0) aborta

_WHEEL_DELTA = 120
_MOUSEEVENTF_WHEEL = 0x0800


def human_delay(mu: float = 0.08, sigma: float = 0.02, low: float = 0.02, high: float = 0.3) -> None:
    time.sleep(entropy.gaussian_clamped(mu, sigma, low, high))


def move(x: int, y: int, jitter_px: float = 0.0) -> None:
    if jitter_px:
        x, y = entropy.jitter_point(x, y, jitter_px)
    pydirectinput.moveTo(x, y)


def click(x: int, y: int, button: str = "left", jitter_px: float = 2.0) -> None:
    move(x, y, jitter_px)
    human_delay()
    pydirectinput.click(button=button)


def press(key: str) -> None:
    pydirectinput.keyDown(key)
    human_delay(0.06, 0.015)
    pydirectinput.keyUp(key)


def type_text(text: str) -> None:
    for ch in text:
        pydirectinput.press(ch)
        human_delay(0.05, 0.015)


# ------------------- primitivas para reproducir macros ------------------- #
def key_down(key: str) -> None:
    pydirectinput.keyDown(key)


def key_up(key: str) -> None:
    pydirectinput.keyUp(key)


def mouse_down(x: int, y: int, button: str = "left") -> None:
    pydirectinput.mouseDown(x, y, button=button)


def mouse_up(x: int, y: int, button: str = "left") -> None:
    pydirectinput.mouseUp(x, y, button=button)


def scroll(x: int, y: int, clicks: int) -> None:
    """Rueda del ratón (positivo = arriba). pydirectinput no lo implementa."""
    pydirectinput.moveTo(x, y)
    if sys.platform != "win32" or not clicks:
        return
    inp = pydirectinput.Input(ctypes.c_ulong(0))  # INPUT_MOUSE
    inp.ii.mi = pydirectinput.MouseInput(0, 0, ctypes.c_ulong(clicks * _WHEEL_DELTA & 0xFFFFFFFF),
                                         _MOUSEEVENTF_WHEEL, 0, ctypes.pointer(ctypes.c_ulong(0)))
    pydirectinput.SendInput(1, ctypes.pointer(inp), ctypes.sizeof(inp))
