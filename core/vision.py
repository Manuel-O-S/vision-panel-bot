"""
vision.py - Localización de ventana, captura ROI (dxcam / mss) y visión OpenCV.

Piezas
    WindowLocator   Rect del área cliente de la ventana objetivo (Win32 / ctypes).
    ScreenCapture   Captura de una región: dxcam (DXGI, monitor principal) con
                    respaldo automático a mss (cualquier monitor).
    preprocess      Escala de grises (+ desenfoque) o bordes Canny.
    TemplateLibrary Plantillas en memoria, reescaladas a la resolución actual y
                    cacheadas ya preprocesadas.
    match_template  cv2.matchTemplate con TM_CCOEFF_NORMED.
    baseline_*      Firma del estado neutro (Zona -1) y distancia (Delta).

Hilos: ``ScreenCapture`` debe crearse y usarse en el MISMO hilo (mss guarda
handles de GDI por hilo y dxcam no es thread-safe). El motor la crea dentro de
su hilo de trabajo. ``TemplateLibrary`` sí es thread-safe.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import cv2
import numpy as np

from core.geometry import Rect

log = logging.getLogger(__name__)

__all__ = [
    "WindowLocator", "ScreenCapture", "PreprocessMode", "PreprocessConfig",
    "preprocess", "to_gray", "read_image", "Template", "TemplateLibrary",
    "MatchResult", "match_template", "baseline_signature", "baseline_delta",
    "crop",
]

_IS_WINDOWS = sys.platform == "win32"


# --------------------------------------------------------------------------- #
# Ventana objetivo (Win32)
# --------------------------------------------------------------------------- #
if _IS_WINDOWS:
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    _user32.EnumWindows.argtypes = [_EnumWindowsProc, wintypes.LPARAM]
    _user32.EnumWindows.restype = wintypes.BOOL
    _user32.IsWindow.argtypes = [wintypes.HWND]
    _user32.IsWindow.restype = wintypes.BOOL
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.restype = wintypes.BOOL
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.IsIconic.restype = wintypes.BOOL
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.GetClientRect.restype = wintypes.BOOL
    _user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    _user32.ClientToScreen.restype = wintypes.BOOL
    _user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    _user32.GetSystemMetrics.restype = ctypes.c_int

    def _window_title(hwnd: int) -> str:
        n = _user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    def _window_pid(hwnd: int) -> int:
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value


class WindowLocator:
    """Devuelve el Rect del **área cliente** (sin bordes ni barra de título).

    ``title``: subcadena del título (sin distinguir mayúsculas). ``None`` o
    vacío -> se usa la pantalla principal completa.

    El HWND se cachea y sólo se vuelve a buscar si la ventana se cierra, así
    que ``locate()`` cuesta microsegundos y puede llamarse en cada ciclo para
    seguir a la ventana si se mueve o cambia de tamaño. Se ignoran las ventanas
    del propio proceso (el panel) para no capturarse a sí mismo.
    """

    def __init__(self, title: str | None = None) -> None:
        self.title = (title or "").strip() or None
        self._hwnd: int | None = None

    def locate(self) -> Rect | None:
        """Rect actual o ``None`` si la ventana no existe o está minimizada."""
        if self.title is None:
            return primary_screen()
        if not _IS_WINDOWS:
            log.warning("Búsqueda de ventanas sólo disponible en Windows; uso pantalla completa")
            return primary_screen()

        if self._hwnd is None or not _user32.IsWindow(self._hwnd):
            self._hwnd = self._find()
            if self._hwnd is None:
                return None
        if _user32.IsIconic(self._hwnd):
            return None

        rc = wintypes.RECT()
        pt = wintypes.POINT(0, 0)
        if not (_user32.GetClientRect(self._hwnd, ctypes.byref(rc))
                and _user32.ClientToScreen(self._hwnd, ctypes.byref(pt))):
            self._hwnd = None
            return None
        w, h = rc.right - rc.left, rc.bottom - rc.top
        if w <= 0 or h <= 0:
            return None
        return Rect(pt.x, pt.y, w, h)

    def _find(self) -> int | None:
        objetivo = self.title.casefold()  # type: ignore[union-attr]
        propio = os.getpid()
        encontrado: list[int] = []

        @_EnumWindowsProc
        def _cb(hwnd: int, _lparam: int) -> bool:
            if (_user32.IsWindowVisible(hwnd)
                    and objetivo in _window_title(hwnd).casefold()
                    and _window_pid(hwnd) != propio):
                encontrado.append(hwnd)
                return False  # detener la enumeración
            return True

        _user32.EnumWindows(_cb, 0)
        if encontrado:
            log.info("Ventana objetivo encontrada: %r", _window_title(encontrado[0]))
            return encontrado[0]
        return None


def primary_screen() -> Rect:
    """Rect de la pantalla principal en píxeles físicos (requiere DPI awareness)."""
    if _IS_WINDOWS:
        return Rect(0, 0, _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1))
    import mss
    with mss.mss() as sct:
        m = sct.monitors[1]
        return Rect(m["left"], m["top"], m["width"], m["height"])


# --------------------------------------------------------------------------- #
# Captura de pantalla
# --------------------------------------------------------------------------- #
class ScreenCapture:
    """Captura BGR de una región absoluta de pantalla.

    - dxcam (DXGI Desktop Duplication): muy rápido, pero sólo el monitor
      principal. Si no hay frame nuevo devuelve ``None``; en ese caso se
      reutiliza el último frame de la misma región (la pantalla no cambió).
    - mss: respaldo para cuando dxcam no está instalado o la región está en
      otro monitor.

    ``bounds`` = escritorio virtual completo (unión de monitores).
    """

    def __init__(self, prefer_dxcam: bool = True) -> None:
        self._owner = threading.get_ident()
        self._dx = None
        self._dx_bounds: Rect | None = None
        self._mss = None
        self._last_key: tuple[int, int, int, int] | None = None
        self._last_frame: np.ndarray | None = None

        if prefer_dxcam and _IS_WINDOWS:
            try:
                import dxcam
                self._dx = dxcam.create(output_idx=0, output_color="BGR")
                if self._dx is None:
                    raise RuntimeError("dxcam.create() devolvió None")
                self._dx_bounds = Rect(0, 0, int(self._dx.width), int(self._dx.height))
            except Exception as exc:  # noqa: BLE001 - dxcam falla de muchas formas según drivers
                log.warning("dxcam no disponible (%s); usando mss", exc)
                self._dx = None

        import mss  # siempre disponible como respaldo
        self._mss = mss.mss()
        v = self._mss.monitors[0]
        self.bounds = Rect(v["left"], v["top"], v["width"], v["height"])
        log.info("Captura: %s", self.backend)

    @property
    def backend(self) -> str:
        return "dxcam+mss" if self._dx is not None else "mss"

    def _check_thread(self) -> None:
        if threading.get_ident() != self._owner:
            raise RuntimeError("ScreenCapture debe usarse en el hilo que la creó")

    def grab(self, rect: Rect) -> np.ndarray | None:
        """Frame BGR (alto × ancho × 3, uint8) de ``rect``.

        ``rect`` debe estar dentro de ``bounds`` (recórtalo antes con
        ``bounds.intersect``). Devuelve ``None`` si la captura falla.
        """
        self._check_thread()
        if not self.bounds.contains(rect):
            raise ValueError(f"Región fuera del escritorio: {rect}")

        if self._dx is not None and self._dx_bounds is not None and self._dx_bounds.contains(rect):
            key = rect.as_ltrb()
            try:
                frame = self._dx.grab(region=key)
            except Exception:  # noqa: BLE001
                log.exception("Fallo de dxcam; se usa mss en este ciclo")
                frame = None
            if frame is not None:
                self._last_key, self._last_frame = key, frame
                return frame
            if key == self._last_key and self._last_frame is not None:
                return self._last_frame  # sin cambios en pantalla desde el último frame

        return self._grab_mss(rect)

    def _grab_mss(self, rect: Rect) -> np.ndarray | None:
        try:
            shot = self._mss.grab({"left": rect.left, "top": rect.top,
                                   "width": rect.width, "height": rect.height})
        except Exception:  # noqa: BLE001
            log.exception("Fallo de mss")
            return None
        return cv2.cvtColor(np.asarray(shot), cv2.COLOR_BGRA2BGR)

    def close(self) -> None:
        if self._dx is not None:
            release = getattr(self._dx, "release", None)
            if callable(release):
                try:
                    release()
                except Exception:  # noqa: BLE001
                    log.debug("Error liberando dxcam", exc_info=True)
            self._dx = None
        if self._mss is not None:
            self._mss.close()
            self._mss = None

    def __enter__(self) -> ScreenCapture:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def crop(frame: np.ndarray, frame_rect: Rect, target: Rect) -> np.ndarray | None:
    """Recorta ``target`` (coords absolutas) de un ``frame`` capturado en ``frame_rect``.

    Devuelve una vista (sin copia) o ``None`` si no se solapan.
    """
    inter = frame_rect.intersect(target)
    if inter is None:
        return None
    x0, y0 = inter.left - frame_rect.left, inter.top - frame_rect.top
    return frame[y0:y0 + inter.height, x0:x0 + inter.width]


# --------------------------------------------------------------------------- #
# Preprocesado
# --------------------------------------------------------------------------- #
class PreprocessMode(str, Enum):
    GRAY = "gris"    # robusto a cambios de color; buen valor por defecto
    CANNY = "canny"  # sólo bordes: robusto a brillo/sombras, peor en zonas lisas


@dataclass(frozen=True, slots=True)
class PreprocessConfig:
    mode: PreprocessMode = PreprocessMode.GRAY
    blur_ksize: int = 3          # desenfoque gaussiano previo (impar; 0/1 = sin desenfoque)
    canny_low: int = 50
    canny_high: int = 150

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", PreprocessMode(self.mode))
        if self.blur_ksize > 1 and self.blur_ksize % 2 == 0:
            raise ValueError("blur_ksize debe ser impar")
        if not 0 <= self.canny_low < self.canny_high:
            raise ValueError("Se requiere 0 <= canny_low < canny_high")


def to_gray(img: np.ndarray) -> np.ndarray:
    if img.ndim == 2:
        return img
    if img.shape[2] == 4:
        return cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def preprocess(img: np.ndarray, cfg: PreprocessConfig) -> np.ndarray:
    """BGR/BGRA/gris -> imagen de un canal uint8 lista para matchTemplate."""
    g = to_gray(img)
    if cfg.blur_ksize > 1:
        g = cv2.GaussianBlur(g, (cfg.blur_ksize, cfg.blur_ksize), 0)
    if cfg.mode is PreprocessMode.CANNY:
        g = cv2.Canny(g, cfg.canny_low, cfg.canny_high)
    return g


def read_image(path: str | Path) -> np.ndarray:
    """Lee una imagen BGR. A diferencia de ``cv2.imread`` soporta rutas con
    tildes/ñ en Windows."""
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"No se pudo leer la imagen: {path}")
    return img


# --------------------------------------------------------------------------- #
# Plantillas
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Template:
    name: str
    image: np.ndarray                    # BGR original
    ref_size: tuple[int, int] | None     # (ancho, alto) de la ventana al recortarla


class TemplateLibrary:
    """Plantillas en memoria con caché de versiones reescaladas + preprocesadas.

    Si la plantilla se recortó con la ventana a 1920×1080 (``ref_size``) y ahora
    la ventana mide 1280×720, se reescala ×0.667 antes de buscarla.
    """

    _SCALE_DECIMALS = 2  # cuantiza la escala para que la caché no crezca sin límite

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._templates: dict[str, Template] = {}
        self._cache: dict[tuple, np.ndarray] = {}

    def add(self, name: str, image: np.ndarray, ref_size: tuple[int, int] | None = None) -> None:
        if image is None or image.size == 0:
            raise ValueError(f"Plantilla vacía: {name}")
        with self._lock:
            self._templates[name] = Template(name, image, ref_size)
            self._drop_cache(name)

    def load_file(self, name: str, path: str | Path, ref_size: tuple[int, int] | None = None) -> None:
        self.add(name, read_image(path), ref_size)

    def load_dir(self, directory: str | Path, ref_size: tuple[int, int] | None = None) -> list[str]:
        """Carga todos los .png del directorio; el nombre es el archivo sin extensión."""
        cargadas = []
        for path in sorted(Path(directory).glob("*.png")):
            try:
                self.load_file(path.stem, path, ref_size)
                cargadas.append(path.stem)
            except ValueError:
                log.warning("Plantilla ilegible: %s", path)
        return cargadas

    def remove(self, name: str) -> None:
        with self._lock:
            self._templates.pop(name, None)
            self._drop_cache(name)

    def __contains__(self, name: object) -> bool:
        with self._lock:
            return name in self._templates

    def names(self) -> list[str]:
        with self._lock:
            return list(self._templates)

    def _drop_cache(self, name: str) -> None:
        for key in [k for k in self._cache if k[0] == name]:
            del self._cache[key]

    def prepared(self, name: str, window_size: tuple[int, int],
                 cfg: PreprocessConfig) -> np.ndarray | None:
        """Plantilla reescalada a ``window_size`` y preprocesada (cacheada)."""
        with self._lock:
            tpl = self._templates.get(name)
            if tpl is None:
                return None
            if tpl.ref_size:
                sx = round(window_size[0] / tpl.ref_size[0], self._SCALE_DECIMALS)
                sy = round(window_size[1] / tpl.ref_size[1], self._SCALE_DECIMALS)
            else:
                sx = sy = 1.0
            key = (name, sx, sy, cfg)
            cached = self._cache.get(key)
            if cached is not None:
                return cached

        img = tpl.image
        if (sx, sy) != (1.0, 1.0):
            h, w = img.shape[:2]
            nw, nh = max(1, round(w * sx)), max(1, round(h * sy))
            interp = cv2.INTER_AREA if sx * sy < 1.0 else cv2.INTER_LINEAR
            img = cv2.resize(img, (nw, nh), interpolation=interp)
        processed = preprocess(img, cfg)
        with self._lock:
            if name in self._templates:  # pudo borrarse mientras se procesaba
                self._cache[key] = processed
        return processed


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class MatchResult:
    score: float   # -1.0 .. 1.0 (TM_CCOEFF_NORMED); >= umbral => detectado
    x: int         # esquina sup. izq. del match, relativa a la imagen buscada
    y: int
    w: int
    h: int


def match_template(haystack: np.ndarray, template: np.ndarray) -> MatchResult:
    """Busca ``template`` en ``haystack`` (ambos de un canal, ya preprocesados).

    - Si la plantilla es mayor que la zona se reduce para que quepa.
    - Plantillas planas (un solo color, o Canny sin bordes) hacen que
      TM_CCOEFF_NORMED sea indefinido (división por 0); en ese caso se usa
      TM_SQDIFF normalizado a [0, 1].
    """
    hh, hw = haystack.shape[:2]
    th, tw = template.shape[:2]
    if th > hh or tw > hw:
        f = min(hh / th, hw / tw)
        template = cv2.resize(template, (max(1, int(tw * f)), max(1, int(th * f))),
                              interpolation=cv2.INTER_AREA)
        th, tw = template.shape[:2]

    if float(template.std()) < 1e-6:
        res = cv2.matchTemplate(haystack, template, cv2.TM_SQDIFF)
        min_val, _, min_loc, _ = cv2.minMaxLoc(res)
        score = 1.0 - min_val / (template.size * 255.0 ** 2)
        x, y = min_loc
    else:
        res = cv2.matchTemplate(haystack, template, cv2.TM_CCOEFF_NORMED)
        # Ventanas planas del haystack producen NaN/inf: se tratan como "sin match".
        np.nan_to_num(res, copy=False, nan=-1.0, posinf=-1.0, neginf=-1.0)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        score = max_val
        x, y = max_loc
    return MatchResult(float(np.clip(score, -1.0, 1.0)), int(x), int(y), tw, th)


# --------------------------------------------------------------------------- #
# Baseline (estado neutro, "Zona -1")
# --------------------------------------------------------------------------- #
def baseline_signature(img: np.ndarray, blur_ksize: int = 5) -> np.ndarray:
    """Firma de una zona para comparar contra su estado neutro.

    Gris + desenfoque (absorbe ruido y antialiasing) y **se resta la media**:
    un cambio global de brillo (iluminación, sombra suave) no altera la firma.
    """
    g = to_gray(img)
    if blur_ksize > 1:
        g = cv2.GaussianBlur(g, (blur_ksize, blur_ksize), 0)
    f = g.astype(np.float32)
    f -= float(f.mean())
    return f


def baseline_delta(current: np.ndarray, baseline: np.ndarray) -> float:
    """Distancia media absoluta normalizada entre dos firmas: 0.0 (igual) - 1.0.

    Si la ventana cambió de tamaño, la firma actual se reescala a la del baseline.
    Valores típicos: ruido < 0.01, cambio sutil 0.02-0.05, cambio claro > 0.08.
    """
    if current.shape != baseline.shape:
        current = cv2.resize(current, (baseline.shape[1], baseline.shape[0]),
                             interpolation=cv2.INTER_AREA)
    return float(np.mean(np.abs(current - baseline))) / 255.0
