"""
block_editor.py - Editor visual de bloques (estilo App Inventor / Scratch).
"""
from __future__ import annotations

import tkinter as tk
import uuid
from dataclasses import dataclass, field
from typing import Any

import customtkinter as ctk

BLOCK_COLORS = {
    "event": "#C88330",
    "vision": "#4C97FF",
    "action": "#59C059",
    "control": "#FFAB19",
    "logic": "#9966FF",
}


@dataclass
class Block:
    type: str                       # p.ej. "find_template", "click", "wait", "if"
    category: str                   # clave de BLOCK_COLORS
    params: dict[str, Any] = field(default_factory=dict)
    x: int = 20
    y: int = 20
    next_id: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class BlockEditor(ctk.CTkFrame):
    """Lienzo donde se arrastran bloques. TODO: encaje (snapping) y paleta."""

    W, H = 160, 40

    def __init__(self, master: tk.Misc, **kw: Any) -> None:
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, bg="#1e1e1e", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.blocks: dict[str, Block] = {}
        self._drag: tuple[str, int, int] | None = None
        self.canvas.tag_bind("block", "<ButtonPress-1>", self._on_press)
        self.canvas.tag_bind("block", "<B1-Motion>", self._on_drag)
        self.canvas.tag_bind("block", "<ButtonRelease-1>", lambda _e: setattr(self, "_drag", None))

    def add_block(self, block: Block) -> None:
        self.blocks[block.id] = block
        color = BLOCK_COLORS.get(block.category, "#888888")
        tags = ("block", block.id)
        self.canvas.create_rectangle(block.x, block.y, block.x + self.W, block.y + self.H,
                                     fill=color, outline="", tags=tags)
        self.canvas.create_text(block.x + 10, block.y + self.H // 2, anchor="w",
                                text=block.type, fill="white", tags=tags)

    def _on_press(self, e: tk.Event) -> None:
        item = self.canvas.find_withtag("current")
        block_id = next((t for t in self.canvas.gettags(item) if t in self.blocks), None)
        if block_id:
            self._drag = (block_id, e.x, e.y)

    def _on_drag(self, e: tk.Event) -> None:
        if not self._drag:
            return
        block_id, lx, ly = self._drag
        dx, dy = e.x - lx, e.y - ly
        self.canvas.move(block_id, dx, dy)
        b = self.blocks[block_id]
        b.x, b.y = b.x + dx, b.y + dy
        self._drag = (block_id, e.x, e.y)

    def to_workflow(self) -> list[dict[str, Any]]:
        return [b.to_dict() for b in self.blocks.values()]
