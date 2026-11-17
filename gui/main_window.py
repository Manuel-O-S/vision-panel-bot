"""
main_window.py - Ventana principal (CustomTkinter).
"""
from __future__ import annotations

import customtkinter as ctk

from core.engine import Engine, EngineState


class MainWindow(ctk.CTk):
    def __init__(self, engine: Engine) -> None:
        super().__init__()
        self.engine = engine
        self.title("VISION BOT")
        self.geometry("1000x650")
        ctk.set_appearance_mode("dark")

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # Barra lateral
        sidebar = ctk.CTkFrame(self, width=200, corner_radius=0)
        sidebar.grid(row=0, column=0, sticky="nsw")
        ctk.CTkButton(sidebar, text="▶ Iniciar", command=self.engine.start).pack(padx=12, pady=(16, 6), fill="x")
        ctk.CTkButton(sidebar, text="⏸ Pausar", command=self.engine.pause).pack(padx=12, pady=6, fill="x")
        ctk.CTkButton(sidebar, text="⏹ Detener", command=self.engine.stop).pack(padx=12, pady=6, fill="x")
        ctk.CTkButton(sidebar, text="✂ Recortar zonas", command=self.open_snipping).pack(padx=12, pady=6, fill="x")
        self.status = ctk.CTkLabel(sidebar, text="Estado: IDLE")
        self.status.pack(side="bottom", padx=12, pady=16)

        # Área central (pestañas)
        tabs = ctk.CTkTabview(self)
        tabs.grid(row=0, column=1, sticky="nsew", padx=10, pady=10)
        tabs.add("Bloques")
        tabs.add("Plantillas")
        tabs.add("Registro")

        self.engine.add_state_listener(self._on_state)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_state(self, state: EngineState) -> None:
        # Los listeners corren en el hilo del motor -> volver al hilo de Tk
        self.after(0, lambda: self.status.configure(text=f"Estado: {state.name}"))

    def open_snipping(self) -> None:
        from gui.snipping_tool import SnippingTool
        SnippingTool(self)

    def _on_close(self) -> None:
        self.engine.stop()
        self.destroy()
