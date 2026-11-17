"""
vision.py - Captura de pantalla (dxcam / mss) y detección por plantillas OpenCV.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)

Region = tuple[int, int, int, int]  # (left, top, right, bottom)


@dataclass(slots=True)
class Match:
    name: str
    x: int
    y: int
    w: int
    h: int
    score: float

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2


class ScreenCapture:
    """Usa dxcam (rápido, DXGI) si está disponible; si no, recurre a mss."""

    def __init__(self, prefer_dxcam: bool = True) -> None:
        self._backend = None
        self._kind = "mss"
        if prefer_dxcam:
            try:
                import dxcam
                self._backend = dxcam.create(output_color="BGR")
                self._kind = "dxcam"
            except Exception:  # noqa: BLE001
                log.warning("dxcam no disponible, usando mss")
        if self._backend is None:
            import mss
            self._backend = mss.mss()

    @property
    def backend(self) -> str:
        return self._kind

    def grab(self, region: Region | None = None) -> np.ndarray | None:
        """Devuelve un frame BGR (numpy) o None si no hubo frame nuevo."""
        if self._kind == "dxcam":
            return self._backend.grab(region=region)
        monitor = self._backend.monitors[1]
        if region:
            l, t, r, b = region
            monitor = {"left": l, "top": t, "width": r - l, "height": b - t}
        img = np.asarray(self._backend.grab(monitor))
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)


class TemplateMatcher:
    """Carga plantillas desde assets/templates y las busca con matchTemplate."""

    def __init__(self, templates_dir: str | Path, threshold: float = 0.85) -> None:
        self.templates_dir = Path(templates_dir)
        self.threshold = threshold
        self.templates: dict[str, np.ndarray] = {}

    def load_all(self) -> None:
        for path in self.templates_dir.glob("*.png"):
            img = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if img is not None:
                self.templates[path.stem] = img
        log.info("Plantillas cargadas: %s", list(self.templates))

    def find(self, frame: np.ndarray, name: str, threshold: float | None = None) -> Match | None:
        tpl = self.templates.get(name)
        if tpl is None or frame is None:
            return None
        res = cv2.matchTemplate(frame, tpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if max_val >= (threshold or self.threshold):
            h, w = tpl.shape[:2]
            return Match(name, max_loc[0], max_loc[1], w, h, float(max_val))
        return None
