"""
snipping_tool.py - Herramienta de recorte tipo Snipping Tool para zonas y plantillas.

Funcionamiento
    1. Se oculta el panel y se toma UNA captura del escritorio completo.
    2. Se muestra a pantalla completa oscurecida; cada recorte se ve con su
       brillo original, un borde y su nombre ("Zona 1", "Zona 2"...).
    3. Al confirmar se devuelven los recortes (Rect absoluto + imagen BGR).

Controles
    Arrastrar ............ nuevo recorte (con el nombre del campo "Nombre")
    Clic en un recorte ... seleccionarlo
    Supr / "Eliminar" .... borrar el seleccionado (o el último)
    Ctrl+Z / Ctrl+Y ...... deshacer / rehacer
    Enter / Esc .......... confirmar / cancelar

Como la imagen está congelada, las plantillas se recortan con píxeles exactos
(sin el tinte del overlay) y la aplicación objetivo no recibe los clics.
"""
from __future__ import annotations

import base64
import logging
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass

import cv2
import customtkinter as ctk
import numpy as np

from core.geometry import Rect
from core.vision import grab_screen

log = logging.getLogger(__name__)

__all__ = ["Snip", "SnippingTool", "bgr_to_photo"]

_DIM = 0.4            # brillo del fondo oscurecido
_MIN_SIZE = 4         # px mínimos de un recorte
_COLOR = "#4C97FF"
_COLOR_SEL = "#FFAB19"
_COLOR_WIN = "#59C059"


def bgr_to_photo(img: np.ndarray, master: tk.Misc | None = None,
                 max_size: tuple[int, int] | None = None) -> tk.PhotoImage:
    """numpy BGR -> ``tk.PhotoImage`` sin Pillow (PNG con compresión mínima: rápido y sin pérdidas)."""
    if max_size:
        h, w = img.shape[:2]
        f = min(max_size[0] / w, max_size[1] / h, 1.0)
        if f < 1.0:
            img = cv2.resize(img, (max(1, int(w * f)), max(1, int(h * f))), interpolation=cv2.INTER_AREA)
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    ok, buf = cv2.imencode(".png", img, [cv2.IMWRITE_PNG_COMPRESSION, 1])
    if not ok:
        raise ValueError("No se pudo convertir la imagen")
    return tk.PhotoImage(master=master, data=base64.b64encode(buf.tobytes()).decode("ascii"))


@dataclass(slots=True)
class Snip:
    name: str
    rect: Rect          # coordenadas absolutas de pantalla
    image: np.ndarray   # recorte BGR


class SnippingTool(tk.Toplevel):
    """Usa ``SnippingTool.open(...)``: oculta el panel antes de capturar."""

    @classmethod
    def open(
        cls,
        master: tk.Misc,
        on_done: Callable[[list[Snip]], None],
        *,
        prefix: str = "Zona",
        first_index: int = 1,
        taken_names: set[str] | None = None,
        window_rect: Rect | None = None,
        title: str = "Recortar zonas",
    ) -> None:
        top = master.winfo_toplevel()
        top.withdraw()

        def _capturar() -> None:
            try:
                img, bounds = grab_screen()
            except Exception as exc:  # noqa: BLE001
                log.exception("No se pudo capturar la pantalla")
                top.deiconify()
                from tkinter import messagebox
                messagebox.showerror("Captura", f"No se pudo capturar la pantalla:\n{exc}", parent=top)
                return
            cls(master, img, bounds, on_done, prefix=prefix, first_index=first_index,
                taken_names=taken_names or set(), window_rect=window_rect, title=title)

        # Pequeña espera para que Windows termine de ocultar el panel antes de capturar.
        top.after(250, _capturar)

    def __init__(self, master: tk.Misc, screenshot: np.ndarray, bounds: Rect,
                 on_done: Callable[[list[Snip]], None], *, prefix: str, first_index: int,
                 taken_names: set[str], window_rect: Rect | None, title: str) -> None:
        super().__init__(master)
        self._master_top = master.winfo_toplevel()
        self._img = screenshot
        self._bounds = bounds
        self._on_done = on_done
        self._prefix = prefix
        self._next_index = first_index
        self._taken = set(taken_names)
        self._window_rect = window_rect

        self.snips: list[Snip] = []
        self._redo: list[Snip] = []
        self._selected: int | None = None
        self._drag_start: tuple[int, int] | None = None
        self._drag_id: int | None = None
        self._photos: list[tk.PhotoImage] = []  # referencias vivas (si no, Tk las borra)

        # Ventana sin bordes que cubre todo el escritorio virtual (multi-monitor).
        self.overrideredirect(True)
        self.geometry(f"{bounds.width}x{bounds.height}{bounds.left:+d}{bounds.top:+d}")
        self.attributes("-topmost", True)
        self.configure(bg="black")

        self.canvas = tk.Canvas(self, width=bounds.width, height=bounds.height,
                                highlightthickness=0, cursor="crosshair", bg="black")
        self.canvas.pack(fill="both", expand=True)
        dim = cv2.convertScaleAbs(screenshot, alpha=_DIM, beta=0)
        self._bg = bgr_to_photo(dim, self)
        self.canvas.create_image(0, 0, image=self._bg, anchor="nw")
        if window_rect is not None:
            x0, y0 = self._to_canvas(window_rect.left, window_rect.top)
            self.canvas.create_rectangle(x0, y0, x0 + window_rect.width, y0 + window_rect.height,
                                         outline=_COLOR_WIN, dash=(6, 4), width=2)

        self._build_toolbar(title)
        self._suggest_name()

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Escape>", lambda _e: self._cancel())
        self.bind("<Return>", lambda _e: self._confirm())
        self.bind("<Delete>", lambda _e: self._delete())
        self.bind("<Control-z>", lambda _e: self._undo())
        self.bind("<Control-y>", lambda _e: self._redo_last())
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.after(50, self._grab_focus)

    # ------------------------------ UI ------------------------------- #
    def _grab_focus(self) -> None:
        self.lift()
        self.focus_force()
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def _build_toolbar(self, title: str) -> None:
        bar = ctk.CTkFrame(self, corner_radius=10, fg_color="#1f1f1f", border_width=1,
                           border_color="#3a3a3a")
        bar.place(relx=0.5, y=14, anchor="n")
        ctk.CTkLabel(bar, text=title, font=ctk.CTkFont(weight="bold")).pack(side="left", padx=(12, 8), pady=8)
        ctk.CTkLabel(bar, text="Nombre:").pack(side="left", padx=(4, 4))
        self.name_var = tk.StringVar()
        ctk.CTkEntry(bar, textvariable=self.name_var, width=150).pack(side="left", padx=4)
        for texto, cmd, color in (
            ("↶ Deshacer", self._undo, None),
            ("↷ Rehacer", self._redo_last, None),
            ("🗑 Eliminar", self._delete, "#8a3b3b"),
            ("✔ Confirmar", self._confirm, "#2f7a3e"),
            ("✖ Cancelar", self._cancel, "#555555"),
        ):
            kw = {"fg_color": color} if color else {}
            ctk.CTkButton(bar, text=texto, width=90, command=cmd, **kw).pack(side="left", padx=4, pady=8)
        self.info = ctk.CTkLabel(bar, text="", text_color="#bbbbbb")
        self.info.pack(side="left", padx=(8, 12))
        self._update_info()

    def _update_info(self) -> None:
        self.info.configure(text=f"{len(self.snips)} recorte(s) · Enter confirma · Esc cancela")

    def _to_canvas(self, x: int, y: int) -> tuple[int, int]:
        return x - self._bounds.left, y - self._bounds.top

    def _used_names(self) -> set[str]:
        return self._taken | {s.name for s in self.snips}

    def _suggest_name(self) -> None:
        usados = self._used_names()
        i = self._next_index
        while f"{self._prefix} {i}" in usados:
            i += 1
        self.name_var.set(f"{self._prefix} {i}")

    def _redraw(self) -> None:
        self.canvas.delete("snip")
        self._photos.clear()
        for i, s in enumerate(self.snips):
            x0, y0 = self._to_canvas(s.rect.left, s.rect.top)
            foto = bgr_to_photo(s.image, self)
            self._photos.append(foto)
            color = _COLOR_SEL if i == self._selected else _COLOR
            self.canvas.create_image(x0, y0, image=foto, anchor="nw", tags=("snip",))
            self.canvas.create_rectangle(x0, y0, x0 + s.rect.width, y0 + s.rect.height,
                                         outline=color, width=2, tags=("snip",))
            ty = y0 - 4 if y0 > 22 else y0 + s.rect.height + 18
            self.canvas.create_text(x0 + 4, ty, text=s.name, anchor="sw", fill=color,
                                    font=("Segoe UI", 11, "bold"), tags=("snip",))
        self._update_info()

    # --------------------------- ratón -------------------------------- #
    def _on_press(self, e: tk.Event) -> None:
        self._drag_start = (e.x, e.y)
        self._drag_id = self.canvas.create_rectangle(e.x, e.y, e.x, e.y, outline=_COLOR_SEL,
                                                     width=2, dash=(4, 2))

    def _on_drag(self, e: tk.Event) -> None:
        if self._drag_start and self._drag_id:
            sx, sy = self._drag_start
            self.canvas.coords(self._drag_id, sx, sy, e.x, e.y)

    def _on_release(self, e: tk.Event) -> None:
        if not self._drag_start:
            return
        sx, sy = self._drag_start
        if self._drag_id:
            self.canvas.delete(self._drag_id)
        self._drag_start, self._drag_id = None, None

        x0, x1 = sorted((max(0, min(sx, e.x)), max(0, max(sx, e.x))))
        y0, y1 = sorted((max(0, min(sy, e.y)), max(0, max(sy, e.y))))
        x1, y1 = min(x1, self._bounds.width), min(y1, self._bounds.height)
        if x1 - x0 < _MIN_SIZE or y1 - y0 < _MIN_SIZE:
            self._select_at(e.x, e.y)  # fue un clic: seleccionar
            return

        nombre = self.name_var.get().strip()
        if not nombre or nombre in self._used_names():
            self._suggest_name()
            nombre = self.name_var.get()
        rect = Rect(self._bounds.left + x0, self._bounds.top + y0, x1 - x0, y1 - y0)
        self.snips.append(Snip(nombre, rect, self._img[y0:y1, x0:x1].copy()))
        self._redo.clear()
        self._selected = None
        self._suggest_name()
        self._redraw()

    def _select_at(self, x: int, y: int) -> None:
        ax, ay = self._bounds.left + x, self._bounds.top + y
        self._selected = None
        for i in range(len(self.snips) - 1, -1, -1):  # el de arriba primero
            r = self.snips[i].rect
            if r.left <= ax < r.right and r.top <= ay < r.bottom:
                self._selected = i
                break
        self._redraw()

    # --------------------------- acciones ----------------------------- #
    def _delete(self) -> None:
        if not self.snips:
            return
        idx = self._selected if self._selected is not None else len(self.snips) - 1
        self._redo.append(self.snips.pop(idx))
        self._selected = None
        self._suggest_name()
        self._redraw()

    def _undo(self) -> None:
        self._selected = None
        self._delete()

    def _redo_last(self) -> None:
        if self._redo:
            s = self._redo.pop()
            if s.name in self._used_names():
                return
            self.snips.append(s)
            self._suggest_name()
            self._redraw()

    def _close(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
        self._master_top.deiconify()
        self._master_top.lift()

    def _confirm(self) -> None:
        resultado = list(self.snips)
        self._close()
        if resultado:
            self._on_done(resultado)

    def _cancel(self) -> None:
        self._close()
