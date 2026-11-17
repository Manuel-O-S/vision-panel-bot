"""
input_sim.py - Simulación de entrada de hardware (pydirectinput / SendInput).
"""
from __future__ import annotations

import time

import pydirectinput

from core import entropy

pydirectinput.PAUSE = 0  # los retardos los gestiona este módulo
pydirectinput.FAILSAFE = True  # mover el ratón a la esquina (0,0) aborta


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
