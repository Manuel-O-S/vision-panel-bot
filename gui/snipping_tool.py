"""
snipping_tool.py - Herramienta de recorte interactiva para seleccionar multi-zonas.
"""
from __future__ import annotations

import tkinter as tk
from typing import Callable

Region = tuple[int, int, int, int]


class SnippingTool(tk.Toplevel):
    """Overlay semitransparente a pantalla completa.

    - Arrastrar con clic izquierdo: añade una zona.
    - Clic derecho: deshace la última zona.
    - Enter: confirma.  Esc: cancela.
    """

    def __init__(self, master: tk.Misc, on_done: Callable[[list[Region]], None] | None = None) -> None:
        super().__init__(master)
        self.on_done = on_done
        self.regions: list[Region] = []
        self._start: tuple[int, int] | None = None
        self._rect_id: int | None = None
        self._rect_ids: list[int] = []

        self.attributes("-fullscreen", True)
        self.attributes("-alpha", 0.3)
        self.attributes("-topmost", True)
        self.canvas = tk.Canvas(self, cursor="cross", bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<ButtonPress-3>", self._undo)
        self.bind("<Return>", self._confirm)
        self.bind("<Escape>", lambda _e: self.destroy())
        self.focus_force()

    def _on_press(self, e: tk.Event) -> None:
        self._start = (e.x_root, e.y_root)
        self._rect_id = self.canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="red", width=2)

    def _on_drag(self, e: tk.Event) -> None:
        if self._start and self._rect_id:
            sx, sy = self._start
            self.canvas.coords(self._rect_id, sx - self.winfo_rootx(), sy - self.winfo_rooty(), e.x, e.y)

    def _on_release(self, e: tk.Event) -> None:
        if not self._start or not self._rect_id:
            return
        sx, sy = self._start
        l, t, r, b = min(sx, e.x_root), min(sy, e.y_root), max(sx, e.x_root), max(sy, e.y_root)
        if r - l > 3 and b - t > 3:
            self.regions.append((l, t, r, b))
            self._rect_ids.append(self._rect_id)
        else:
            self.canvas.delete(self._rect_id)
        self._start, self._rect_id = None, None

    def _undo(self, _e: tk.Event) -> None:
        if self.regions:
            self.regions.pop()
            self.canvas.delete(self._rect_ids.pop())

    def _confirm(self, _e: tk.Event) -> None:
        if self.on_done:
            self.on_done(list(self.regions))
        self.destroy()
