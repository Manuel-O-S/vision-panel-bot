"""
main_window.py - Ventana principal (CustomTkinter, modo oscuro).

Hilos
    La GUI vive en el hilo principal y NUNCA espera al motor:
    - órdenes -> métodos no bloqueantes del Engine (start, pause, panic...)
    - estado  -> ``engine.poll_events()`` cada 16 ms (≈ 60 FPS) con ``after``
    - hotkeys -> el listener de pynput encola órdenes en ``_ui_queue`` (salvo el
      pánico, que llama a ``engine.panic()`` directamente desde su hilo).

Recolector de basura
    Tk NO es thread-safe y sus objetos (fuentes, imágenes) suelen quedar en
    ciclos de referencias. Si el GC automático se dispara en el hilo del motor,
    destruye allí esos objetos, llama a Tcl desde el hilo equivocado y el motor
    se bloquea. Por eso se desactiva el GC automático y se ejecuta
    ``gc.collect()`` periódicamente desde el hilo de la GUI.

Edición vs. ejecución
    Al pulsar Play el motor recibe una COPIA del flujo: puedes seguir editando
    sin afectar a la ejecución en curso; los cambios se aplican en el próximo Play.
"""
from __future__ import annotations

import gc
import logging
import queue
import time
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any

import customtkinter as ctk

from actions.macro_recorder import MacroError, MacroRecorder
from core.engine import Engine, EngineEvent, EngineState, EventType
from core.geometry import Rect, RelativeZone
from core.rules import BASELINE, TemplateRef
from core.vision import TemplateLibrary, WindowLocator, list_windows, primary_screen, read_image
from gui.block_editor import ActionsEditor, FlowEditor
from gui.snipping_tool import Snip, SnippingTool, bgr_to_photo
from workflows.hotkeys import (DEFAULT_PANIC, DEFAULT_SNIP_TEMPLATES, DEFAULT_SNIP_ZONES,
                               DEFAULT_TOGGLE, HotkeyManager, describe)
from workflows.task_manager import Task, TaskError, TaskManager, save_template_image

log = logging.getLogger(__name__)

FULL_SCREEN = "(pantalla completa)"
STATE_STYLE = {
    EngineState.IDLE: ("● Detenido", "gray70", "▶  Play"),
    EngineState.RUNNING: ("● Ejecutando", "#59C059", "❚❚  Pausa"),
    EngineState.PAUSED: ("● En pausa", "#FFAB19", "▶  Reanudar"),
    EngineState.STOPPING: ("● Deteniendo…", "#e05555", "▶  Play"),
}
MAX_LOG_LINES = 2000
GC_INTERVAL_S = 2.0


class MainWindow(ctk.CTk):
    POLL_MS = 16

    def __init__(self, engine: Engine, tasks: TaskManager, root_dir: Path) -> None:
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        super().__init__()
        self.engine = engine
        self.tasks = tasks
        self.root_dir = root_dir
        self.templates_dir = root_dir / "assets" / "templates"
        self.macros_dir = root_dir / "macros"
        self._recorder: MacroRecorder | None = None

        self.task = Task.new()
        self.task_path: Path | None = None
        self.dirty = False
        self._ui_queue: queue.Queue[str] = queue.Queue()
        self._lib_selected: str | None = None
        self._thumbs: list[tk.PhotoImage] = []
        self._log_lines = 0
        self._loading = True  # silencia los callbacks mientras se rellena la UI
        gc.disable()  # ver "Recolector de basura" en el docstring del módulo
        self._next_gc = time.monotonic() + GC_INTERVAL_S

        self.geometry("1280x760")
        self.minsize(1100, 640)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        self._build_tabs()

        self.hotkeys = HotkeyManager(
            on_toggle=lambda: self._ui_queue.put("toggle"),
            on_panic=self._panic_from_hotkey,
            esc_panic=self.task.settings.esc_panic,
            extra={DEFAULT_SNIP_ZONES: lambda: self._ui_queue.put("snip_zones"),
                   DEFAULT_SNIP_TEMPLATES: lambda: self._ui_queue.put("snip_templates")},
        )
        try:
            self.hotkeys.start()
        except Exception as exc:  # noqa: BLE001
            log.exception("No se pudieron registrar los hotkeys")
            self._log(f"⚠ Hotkeys globales no disponibles: {exc}")

        self._load_task_into_ui()
        self._set_state_ui(EngineState.IDLE)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(self.POLL_MS, self._poll)

    # ======================================================================= #
    # Construcción
    # ======================================================================= #
    def _build_sidebar(self) -> None:
        bar = ctk.CTkFrame(self, width=210, corner_radius=0)
        bar.grid(row=0, column=0, sticky="nsw")
        bar.grid_propagate(False)

        ctk.CTkLabel(bar, text="VISION BOT", font=ctk.CTkFont(size=22, weight="bold")).pack(pady=(18, 0))
        ctk.CTkLabel(bar, text="Panel de automatización V1", text_color="gray60").pack(pady=(0, 14))

        self.play_btn = ctk.CTkButton(bar, text="▶  Play", height=42, font=ctk.CTkFont(size=15, weight="bold"),
                                      command=self._play)
        self.play_btn.pack(fill="x", padx=14, pady=4)
        ctk.CTkButton(bar, text="⏹  Detener", fg_color="#444444", command=self._stop
                      ).pack(fill="x", padx=14, pady=4)
        ctk.CTkButton(bar, text="⛔  PÁNICO", fg_color="#a32020", hover_color="#7d1818",
                      command=self._panic).pack(fill="x", padx=14, pady=4)

        ctk.CTkLabel(bar, text="HERRAMIENTAS", text_color="gray55",
                     font=ctk.CTkFont(size=11, weight="bold")).pack(anchor="w", padx=16, pady=(18, 2))
        for texto, cmd in (("✂  Recortar zonas", self._snip_zones),
                           ("🖼  Recortar plantilla", self._snip_templates),
                           ("⟳  Recalibrar Baseline", self._recalibrate),
                           ("⏺  Grabar macro", self._record_macro)):
            ctk.CTkButton(bar, text=texto, anchor="w", fg_color="#2f3b4d", command=cmd
                          ).pack(fill="x", padx=14, pady=3)

        self.status = ctk.CTkLabel(bar, text="", font=ctk.CTkFont(size=14, weight="bold"))
        self.status.pack(side="bottom", pady=(4, 16))
        ctk.CTkLabel(bar, justify="left", text_color="gray55",
                     text=f"Play/Pausa: {describe(DEFAULT_TOGGLE)}\n"
                          f"Pánico: {describe(DEFAULT_PANIC)} / Esc\n"
                          f"Recortar zonas: {describe(DEFAULT_SNIP_ZONES)}\n"
                          f"Recortar plantilla: {describe(DEFAULT_SNIP_TEMPLATES)}"
                     ).pack(side="bottom", padx=10)

    def _build_tabs(self) -> None:
        self.tabs = ctk.CTkTabview(self)
        self.tabs.grid(row=0, column=1, sticky="nsew", padx=10, pady=(0, 10))
        for nombre in ("Inicio", "Editor", "Acciones", "Zonas y plantillas", "Ajustes", "Registro"):
            self.tabs.add(nombre)
        self._build_home(self.tabs.tab("Inicio"))
        self.editor = FlowEditor(self.tabs.tab("Editor"), on_change=self._mark_dirty,
                                 base_dir=self.root_dir)
        self.editor.pack(fill="both", expand=True)
        self._build_library(self.tabs.tab("Acciones"))
        self._build_assets(self.tabs.tab("Zonas y plantillas"))
        self._build_settings(self.tabs.tab("Ajustes"))
        self.log_box = ctk.CTkTextbox(self.tabs.tab("Registro"), font=ctk.CTkFont(family="Consolas", size=12))
        self.log_box.pack(fill="both", expand=True)
        self.log_box.configure(state="disabled")

    def _build_home(self, tab: Any) -> None:
        cab = ctk.CTkFrame(tab, fg_color="transparent")
        cab.pack(fill="x", padx=10, pady=(10, 4))
        ctk.CTkLabel(cab, text="Tarea", font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")
        self.name_var = tk.StringVar()
        self.name_var.trace_add("write", self._on_name_change)
        ctk.CTkEntry(cab, textvariable=self.name_var, width=300).pack(side="left", padx=10)
        self.path_label = ctk.CTkLabel(cab, text="", text_color="gray60")
        self.path_label.pack(side="left", padx=6)

        botones = ctk.CTkFrame(tab, fg_color="transparent")
        botones.pack(fill="x", padx=10, pady=8)
        for texto, cmd, color in (
            ("📄  Nueva", self._new_task, "#3a3a3a"),
            ("📂  Cargar Tarea", self._load_task, "#2f3b4d"),
            ("💾  Guardar Tarea", self._save_task, "#2f3b4d"),
            ("✏  Editar", lambda: self.tabs.set("Editor"), "#5b3f99"),
        ):
            ctk.CTkButton(botones, text=texto, height=48, width=150, fg_color=color,
                          font=ctk.CTkFont(size=14), command=cmd).pack(side="left", padx=4)
        self.home_play = ctk.CTkButton(botones, text="▶  Play", height=48, width=150,
                                       font=ctk.CTkFont(size=14, weight="bold"), command=self._play)
        self.home_play.pack(side="left", padx=4)
        ctk.CTkButton(botones, text="Guardar como…", width=120, fg_color="transparent",
                      border_width=1, command=lambda: self._save_task(save_as=True)).pack(side="left", padx=10)

        self.summary = ctk.CTkLabel(tab, text="", justify="left", anchor="w")
        self.summary.pack(fill="x", padx=14, pady=(6, 2))
        self.problems = ctk.CTkLabel(tab, text="", justify="left", anchor="w", text_color="#e0a050")
        self.problems.pack(fill="x", padx=14)

        ctk.CTkLabel(tab, text="MONITOR EN VIVO", text_color="gray55",
                     font=ctk.CTkFont(size=11, weight="bold")).pack(anchor="w", padx=14, pady=(12, 2))
        self.monitor = ctk.CTkTextbox(tab, font=ctk.CTkFont(family="Consolas", size=13), height=220)
        self.monitor.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._set_monitor("Pulsa Play para ver aquí lo que detecta cada zona.\n"
                          "Al iniciar se toma el Baseline (estado neutro) de cada zona: "
                          "deja la aplicación en su estado de reposo.")

    def _build_library(self, tab: Any) -> None:
        tab.grid_columnconfigure(1, weight=1)
        tab.grid_rowconfigure(0, weight=1)
        izq = ctk.CTkFrame(tab, width=220)
        izq.grid(row=0, column=0, sticky="nsw", padx=(0, 8))
        izq.grid_propagate(False)
        ctk.CTkLabel(izq, text="BIBLIOTECA DE ACCIONES", font=ctk.CTkFont(size=12, weight="bold")
                     ).pack(anchor="w", padx=10, pady=(10, 2))
        ctk.CTkLabel(izq, text="Secuencias reutilizables que las\nreglas invocan (A1, A2…).",
                     text_color="gray60", justify="left").pack(anchor="w", padx=10)
        ctk.CTkButton(izq, text="+ Nueva acción", fg_color="#3d8a3d",
                      command=self._new_library_action).pack(fill="x", padx=10, pady=8)
        self.lib_list = ctk.CTkScrollableFrame(izq, fg_color="transparent")
        self.lib_list.pack(fill="both", expand=True, padx=4, pady=(0, 6))
        self.lib_panel = ctk.CTkScrollableFrame(tab)
        self.lib_panel.grid(row=0, column=1, sticky="nsew")

    def _refresh_library(self) -> None:
        flow = self.task.flow
        if self._lib_selected not in flow.actions:
            self._lib_selected = next(iter(flow.actions), None)
        self._render_library_list()

        for w in self.lib_panel.winfo_children():
            w.destroy()
        nombre = self._lib_selected
        if nombre is None:
            ctk.CTkLabel(self.lib_panel, text="Crea una acción con '+ Nueva acción' y luego asígnala "
                         "a una regla desde su tarjeta.", text_color="gray60").pack(pady=30)
            return
        cab = ctk.CTkFrame(self.lib_panel, fg_color="transparent")
        cab.pack(fill="x", padx=8, pady=(8, 4))
        ctk.CTkLabel(cab, text=f"Acción {nombre}", font=ctk.CTkFont(size=16, weight="bold")).pack(side="left")
        usos = flow.action_usage(nombre)
        ctk.CTkLabel(cab, text=f"   usada {usos} vez/veces", text_color="gray60").pack(side="left")
        ctk.CTkButton(cab, text="🗑 Eliminar", width=100, fg_color="#8a3b3b",
                      command=lambda: self._delete_library_action(nombre)).pack(side="right")
        ActionsEditor(self.lib_panel, flow.actions[nombre], [z.name for z in flow.zones],
                      list(flow.templates), self._on_library_change, base_dir=self.root_dir,
                      library=list(flow.actions), owner=nombre, exclude=nombre,
                      empty_text="(vacía: añade pasos o inserta código)").pack(fill="x", padx=8, pady=8)

    def _select_library(self, nombre: str) -> None:
        self._lib_selected = nombre
        self._refresh_library()

    def _render_library_list(self) -> None:
        flow = self.task.flow
        for w in self.lib_list.winfo_children():
            w.destroy()
        for nombre, pasos in flow.actions.items():
            sel = nombre == self._lib_selected
            ctk.CTkButton(self.lib_list, text=f"⚡ {nombre}   ({len(pasos)} paso{'s' if len(pasos) != 1 else ''})",
                          anchor="w", fg_color="#1f538d" if sel else "#2b2b2b",
                          command=lambda n=nombre: self._select_library(n)).pack(fill="x", pady=2)

    def _on_library_change(self) -> None:
        self._render_library_list()  # sólo la lista: no reconstruye el editor (mantiene el foco)
        self._mark_dirty()

    def _new_library_action(self) -> None:
        flow = self.task.flow
        nombre = flow.next_action_name()
        flow.actions[nombre] = []
        self._lib_selected = nombre
        self._refresh_library()
        self.editor.refresh()
        self._mark_dirty()

    def _delete_library_action(self, nombre: str) -> None:
        flow = self.task.flow
        usos = flow.action_usage(nombre)
        msg = f"¿Eliminar la acción {nombre!r}?"
        if usos:
            msg += f"\n\nSe invoca {usos} vez/veces: esas reglas darán error hasta que elijas otra."
        if not messagebox.askyesno("Eliminar acción", msg, parent=self):
            return
        del flow.actions[nombre]
        self._lib_selected = None
        self._refresh_library()
        self.editor.refresh()
        self._mark_dirty()

    def _build_assets(self, tab: Any) -> None:
        tab.grid_columnconfigure((0, 1), weight=1)
        tab.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(tab, text="ZONAS", font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, sticky="w", padx=10)
        ctk.CTkLabel(tab, text="PLANTILLAS", font=ctk.CTkFont(weight="bold")).grid(row=0, column=1, sticky="w", padx=10)
        self.zones_box = ctk.CTkScrollableFrame(tab)
        self.zones_box.grid(row=1, column=0, sticky="nsew", padx=(0, 5), pady=6)
        self.tpl_box = ctk.CTkScrollableFrame(tab)
        self.tpl_box.grid(row=1, column=1, sticky="nsew", padx=(5, 0), pady=6)

    def _build_settings(self, tab: Any) -> None:
        f = ctk.CTkScrollableFrame(tab)
        f.pack(fill="both", expand=True)

        def fila(titulo: str, ayuda: str = "") -> ctk.CTkFrame:
            ctk.CTkLabel(f, text=titulo, font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=10, pady=(14, 0))
            if ayuda:
                ctk.CTkLabel(f, text=ayuda, text_color="gray60", justify="left").pack(anchor="w", padx=10)
            r = ctk.CTkFrame(f, fg_color="transparent")
            r.pack(fill="x", padx=10, pady=4)
            return r

        r = fila("Ventana objetivo", "Las zonas se guardan relativas a su área cliente (independiente de la resolución).")
        self.window_combo = ctk.CTkComboBox(r, width=420, command=self._on_window_change)
        self.window_combo.pack(side="left")
        self.window_combo.bind("<FocusOut>", lambda _e: self._on_window_change(self.window_combo.get()))
        ctk.CTkButton(r, text="⟳", width=36, command=self._refresh_windows).pack(side="left", padx=6)

        r = fila("Similitud mínima (plantillas)", "TM_CCOEFF_NORMED. Con Canny conviene ~0.75–0.80.")
        self.match_slider, self.match_label = self._slider(r, 0.5, 1.0, 50, self._on_match_change)

        r = fila("Tolerancia Delta (Baseline)",
                 "Diferencia admitida respecto al estado neutro: absorbe iluminación y sombras.")
        self.delta_slider, self.delta_label = self._slider(r, 0.0, 0.3, 60, self._on_delta_change)
        ctk.CTkButton(r, text="⟳ Recalibrar", width=110, command=self._recalibrate).pack(side="left", padx=10)

        r = fila("Preprocesado", "Gris: uso general.  Canny: sólo bordes, robusto a cambios de brillo.")
        self.pre_seg = ctk.CTkSegmentedButton(r, values=["gris", "canny"], command=self._on_pre_change)
        self.pre_seg.pack(side="left")

        r = fila("Ciclos de visión por segundo (máx.)")
        self.fps_slider, self.fps_label = self._slider(r, 5, 60, 55, self._on_fps_change, fmt="{:.0f}")

        r = fila("Atajos globales",
                 f"{describe(DEFAULT_TOGGLE)} → Play/Pausa    {describe(DEFAULT_PANIC)} → Pánico\n"
                 f"{describe(DEFAULT_SNIP_ZONES)} → Recortar zonas    "
                 f"{describe(DEFAULT_SNIP_TEMPLATES)} → Recortar plantilla")
        self.esc_var = tk.BooleanVar(value=True)
        ctk.CTkSwitch(r, text="Esc también activa el pánico", variable=self.esc_var,
                      command=self._on_esc_change).pack(side="left")

    @staticmethod
    def _slider(parent: Any, lo: float, hi: float, steps: int, cmd: Any,
                fmt: str = "{:.3f}") -> tuple[ctk.CTkSlider, ctk.CTkLabel]:
        lbl = ctk.CTkLabel(parent, text="", width=60)
        s = ctk.CTkSlider(parent, from_=lo, to=hi, number_of_steps=steps, width=320,
                          command=lambda v: (lbl.configure(text=fmt.format(v)), cmd(v)))
        s.pack(side="left")
        lbl.pack(side="left", padx=8)
        s._fmt = fmt  # type: ignore[attr-defined]
        return s, lbl

    @staticmethod
    def _set_slider(s: ctk.CTkSlider, lbl: ctk.CTkLabel, v: float) -> None:
        s.set(v)
        lbl.configure(text=s._fmt.format(v))  # type: ignore[attr-defined]

    # ======================================================================= #
    # Tarea <-> UI
    # ======================================================================= #
    def _load_task_into_ui(self) -> None:
        st = self.task.settings
        self._loading = True
        self.name_var.set(self.task.name)
        self._refresh_windows()
        self.window_combo.set(st.window_title or FULL_SCREEN)
        self._set_slider(self.match_slider, self.match_label, st.match_threshold)
        self._set_slider(self.delta_slider, self.delta_label, st.delta_threshold)
        self._set_slider(self.fps_slider, self.fps_label, st.max_fps)
        self.pre_seg.set(st.preprocess)
        self.esc_var.set(st.esc_panic)
        self.hotkeys.esc_panic = st.esc_panic
        self._loading = False
        self.editor.set_flow(self.task.flow)
        self._lib_selected = None
        self._refresh_assets()
        self._refresh_library()
        self.dirty = False
        self._refresh_header()

    def _mark_dirty(self) -> None:
        if getattr(self, "_loading", False):
            return
        self.dirty = True
        self._refresh_header()

    def _refresh_header(self) -> None:
        marca = " •" if self.dirty else ""
        self.title(f"VISION BOT — {self.task.name}{marca}")
        self.path_label.configure(text=(str(self.task_path) if self.task_path else "(sin guardar)") + marca)
        flow = self.task.flow
        reglas = sum(1 for _ in flow.iter_rules())
        procesos = sum(len(l.processes) for l in flow.loops)
        self.summary.configure(
            text=f"{len(flow.zones)} zona(s) · {len(flow.templates)} plantilla(s) · "
                 f"{len(flow.loops)} loop(s) · {procesos} proceso(s) · {reglas} regla(s)")
        problemas = flow.validate()
        self.problems.configure(text="\n".join(f"⚠ {p}" for p in problemas[:6]))

    def _on_name_change(self, *_a: object) -> None:
        nombre = self.name_var.get().strip()
        if nombre and nombre != self.task.flow.name:
            self.task.flow.name = nombre
            self._mark_dirty()

    def _confirm_discard(self) -> bool:
        """True si se puede descartar la tarea actual."""
        if not self.dirty:
            return True
        r = messagebox.askyesnocancel("Cambios sin guardar",
                                      f"¿Guardar los cambios de {self.task.name!r}?", parent=self)
        if r is None:
            return False
        return self._save_task() if r else True

    def _require_idle(self, accion: str) -> bool:
        if self.engine.is_alive():
            messagebox.showwarning(accion, "Detén la ejecución primero.", parent=self)
            return False
        return True

    def _new_task(self) -> None:
        if not self._require_idle("Nueva tarea") or not self._confirm_discard():
            return
        self.task, self.task_path = Task.new(), None
        self.engine.templates = TemplateLibrary()
        self._load_task_into_ui()
        self._log("Nueva tarea creada")

    def _load_task(self) -> None:
        if not self._require_idle("Cargar tarea") or not self._confirm_discard():
            return
        ruta = filedialog.askopenfilename(
            parent=self, title="Cargar tarea", initialdir=self.tasks.tasks_dir,
            filetypes=[("Tareas", "*.tarea *.json"), ("Todos", "*.*")])
        if not ruta:
            return
        try:
            self.task = self.tasks.load(ruta)
        except TaskError as exc:
            messagebox.showerror("Cargar tarea", str(exc), parent=self)
            return
        self.task_path = Path(ruta)
        self.engine.templates = TemplateLibrary()
        self._load_task_into_ui()
        self._log(f"Tarea cargada: {ruta}")

    def _save_task(self, save_as: bool = False) -> bool:
        ruta = self.task_path
        if save_as or ruta is None:
            elegido = filedialog.asksaveasfilename(
                parent=self, title="Guardar tarea", initialdir=self.tasks.tasks_dir,
                initialfile=f"{self.task.name}.tarea", defaultextension=".tarea",
                filetypes=[("Tarea", "*.tarea"), ("JSON", "*.json")])
            if not elegido:
                return False
            ruta = Path(elegido)
        try:
            self.task_path = self.tasks.save(self.task, ruta)
        except (TaskError, OSError) as exc:
            messagebox.showerror("Guardar tarea", str(exc), parent=self)
            return False
        self.dirty = False
        self._refresh_header()
        self._log(f"Tarea guardada: {self.task_path}")
        return True

    # ======================================================================= #
    # Zonas y plantillas
    # ======================================================================= #
    def _target_rect(self) -> Rect | None:
        titulo = self.task.settings.window_title
        if not titulo:
            return primary_screen()
        r = WindowLocator(titulo).locate()
        if r is None:
            messagebox.showwarning("Ventana objetivo",
                                   f"No encuentro la ventana {titulo!r} (¿cerrada o minimizada?).\n"
                                   "Ábrela o elige otra en Ajustes.", parent=self)
        return r

    def _can_snip_from_hotkey(self) -> bool:
        """Evita abrir dos recortes a la vez o recortar mientras se graba una macro."""
        if self._recorder is not None or self.state() == "withdrawn":
            return False  # 'withdrawn' = ya hay un recorte abriéndose o abierto
        if any(isinstance(w, SnippingTool) for w in self.winfo_children()):
            return False
        return True

    def _snip_zones(self) -> None:
        win = self._target_rect()
        if win is None:
            return
        usados = {z.name for z in self.task.flow.zones}
        SnippingTool.open(self, lambda s: self._on_zone_snips(s, win), prefix="Zona",
                          taken_names=usados, window_rect=win, title="Recortar zonas")

    def _on_zone_snips(self, snips: list[Snip], win: Rect) -> None:
        for s in snips:
            try:
                zona = RelativeZone.from_rect(s.name, s.rect, win)
            except ValueError as exc:
                self._log(f"⚠ {s.name} ignorada: {exc}")
                continue
            self.task.flow.zones.append(zona)
            self._log(f"Zona añadida: {zona.name}  (x={zona.x:.3f} y={zona.y:.3f} "
                      f"ancho={zona.w:.3f} alto={zona.h:.3f})")
        self._after_assets_change()

    def _snip_templates(self) -> None:
        win = self._target_rect()
        if win is None:
            return
        SnippingTool.open(self, lambda s: self._on_template_snips(s, win), prefix="Plantilla",
                          taken_names=set(self.task.flow.templates), window_rect=win,
                          title="Recortar plantillas")

    def _on_template_snips(self, snips: list[Snip], win: Rect) -> None:
        for s in snips:
            try:
                ruta = save_template_image(s.image, s.name, self.templates_dir, self.root_dir)
            except (TaskError, OSError) as exc:
                self._log(f"⚠ No se pudo guardar {s.name}: {exc}")
                continue
            self.task.flow.templates[s.name] = TemplateRef(ruta, ref_size=win.size)
            self.engine.templates.add(s.name, s.image, win.size)
            self._log(f"Plantilla añadida: {s.name} → {ruta}")
        self._after_assets_change()

    def _after_assets_change(self) -> None:
        self._mark_dirty()
        self._refresh_assets()
        self._refresh_library()
        self.editor.refresh()
        self.tabs.set("Zonas y plantillas")

    def _refresh_assets(self) -> None:
        for box in (self.zones_box, self.tpl_box):
            for w in box.winfo_children():
                w.destroy()
        self._thumbs.clear()
        flow = self.task.flow
        uso = flow.requirements()
        plantillas_usadas = {t for ts in uso.values() for t in ts if t != BASELINE}

        if not flow.zones:
            ctk.CTkLabel(self.zones_box, text="Sin zonas. Usa '✂ Recortar zonas'.",
                         text_color="gray60").pack(pady=20)
        for i, z in enumerate(flow.zones):
            fila = ctk.CTkFrame(self.zones_box)
            fila.pack(fill="x", pady=3)
            ctk.CTkLabel(fila, text=z.name, font=ctk.CTkFont(weight="bold"), width=90, anchor="w"
                         ).pack(side="left", padx=8, pady=6)
            ctk.CTkLabel(fila, text=f"x {z.x:.3f}  y {z.y:.3f}\n{z.w:.3f} × {z.h:.3f}",
                         text_color="gray65", justify="left").pack(side="left", padx=4)
            ctk.CTkButton(fila, text="🗑", width=32, fg_color="#8a3b3b",
                          command=lambda n=z.name: self._delete_zone(n)).pack(side="right", padx=(2, 6))
            ctk.CTkButton(fila, text="✏", width=32, fg_color="#3a3a3a",
                          command=lambda n=z.name: self._rename("zona", n)).pack(side="right", padx=2)
            var = tk.StringVar(value="" if z.tolerance is None else str(z.tolerance))
            e = ctk.CTkEntry(fila, textvariable=var, width=70, placeholder_text="global")
            e.pack(side="right", padx=4)
            e.bind("<FocusOut>", lambda _e, i=i, v=var, w=e: self._set_zone_tolerance(i, v, w))
            e.bind("<Return>", lambda _e, i=i, v=var, w=e: self._set_zone_tolerance(i, v, w))
            ctk.CTkLabel(fila, text="tolerancia\n(vacío = global)", text_color="gray60",
                         justify="right").pack(side="right")
            if z.name in uso:
                ctk.CTkLabel(fila, text="en uso", text_color="#59C059").pack(side="right", padx=8)

        if not flow.templates:
            ctk.CTkLabel(self.tpl_box, text="Sin plantillas. Usa '🖼 Recortar plantilla'.",
                         text_color="gray60").pack(pady=20)
        for nombre, ref in flow.templates.items():
            fila = ctk.CTkFrame(self.tpl_box)
            fila.pack(fill="x", pady=3)
            try:
                ruta = Path(ref.path) if Path(ref.path).is_absolute() else self.root_dir / ref.path
                foto = bgr_to_photo(read_image(ruta), self, max_size=(120, 60))
                self._thumbs.append(foto)
                tk.Label(fila, image=foto, bg="#2b2b2b").pack(side="left", padx=6, pady=6)
            except (OSError, ValueError):
                ctk.CTkLabel(fila, text="(archivo\nno encontrado)", text_color="#e05555").pack(side="left", padx=6)
            ref_txt = f"{ref.ref_size[0]}×{ref.ref_size[1]}" if ref.ref_size else "sin ref."
            ctk.CTkLabel(fila, text=f"{nombre}\n{ref_txt}", justify="left", anchor="w"
                         ).pack(side="left", padx=6)
            ctk.CTkButton(fila, text="🗑", width=32, fg_color="#8a3b3b",
                          command=lambda n=nombre: self._delete_template(n)).pack(side="right", padx=(2, 6))
            ctk.CTkButton(fila, text="✏", width=32, fg_color="#3a3a3a",
                          command=lambda n=nombre: self._rename("plantilla", n)).pack(side="right", padx=2)
            if nombre in plantillas_usadas:
                ctk.CTkLabel(fila, text="en uso", text_color="#59C059").pack(side="right", padx=8)
        self._refresh_header()

    def _set_zone_tolerance(self, i: int, var: tk.StringVar, entry: ctk.CTkEntry) -> None:
        zonas = self.task.flow.zones
        if i >= len(zonas):
            return
        texto = var.get().strip().replace(",", ".")
        try:
            tol = None if not texto else float(texto)
            nueva = replace(zonas[i], tolerance=tol)
        except ValueError:
            entry.configure(border_color="#e05555")
            return
        entry.configure(border_color="#565b5e")
        if nueva != zonas[i]:
            zonas[i] = nueva
            self._mark_dirty()

    def _rename(self, tipo: str, viejo: str, nuevo: str | None = None) -> bool:
        """Renombra una zona o plantilla y actualiza todas las reglas que la usan."""
        if nuevo is None:
            nuevo = ctk.CTkInputDialog(title="Renombrar",
                                       text=f"Nuevo nombre para {tipo} {viejo!r}:").get_input()
        if nuevo is None or not nuevo.strip() or nuevo.strip() == viejo:
            return False
        flow = self.task.flow
        try:
            if tipo == "zona":
                nuevo = flow.rename_zone(viejo, nuevo)
            else:
                nuevo = flow.rename_template(viejo, nuevo)
                self.engine.templates.remove(viejo)  # se recarga con el nombre nuevo al dar Play
        except ValueError as exc:
            messagebox.showerror("Renombrar", str(exc), parent=self)
            return False
        self._log(f"{tipo.capitalize()} renombrada: {viejo} → {nuevo}")
        self._after_assets_change()
        return True

    def _delete_zone(self, nombre: str) -> None:
        if nombre in self.task.flow.requirements() and not messagebox.askyesno(
                "Eliminar zona", f"{nombre!r} se usa en alguna regla. ¿Eliminarla igualmente?", parent=self):
            return
        self.task.flow.zones = [z for z in self.task.flow.zones if z.name != nombre]
        self._after_assets_change()

    def _delete_template(self, nombre: str) -> None:
        usadas = {t for ts in self.task.flow.requirements().values() for t in ts}
        if nombre in usadas and not messagebox.askyesno(
                "Eliminar plantilla", f"{nombre!r} se usa en alguna regla. ¿Eliminarla igualmente?",
                parent=self):
            return
        self.task.flow.templates.pop(nombre, None)
        self.engine.templates.remove(nombre)
        self._after_assets_change()

    # ======================================================================= #
    # Ajustes
    # ======================================================================= #
    def _refresh_windows(self) -> None:
        self.window_combo.configure(values=[FULL_SCREEN] + list_windows())

    def _on_window_change(self, valor: str) -> None:
        if self._loading:
            return
        titulo = None if valor.strip() in ("", FULL_SCREEN) else valor.strip()
        if titulo != self.task.settings.window_title:
            self.task.settings.window_title = titulo
            self._mark_dirty()

    def _on_match_change(self, v: float) -> None:
        if self._loading:
            return
        self.task.settings.match_threshold = round(v, 3)
        self.engine.config.match_threshold = round(v, 3)  # se aplica en caliente
        self._mark_dirty()

    def _on_delta_change(self, v: float) -> None:
        if self._loading:
            return
        self.task.settings.delta_threshold = round(v, 3)
        self.engine.set_delta_threshold(round(v, 3))  # se aplica en caliente
        self._mark_dirty()

    def _on_pre_change(self, v: str) -> None:
        if self._loading:
            return
        self.task.settings.preprocess = v
        self._mark_dirty()
        if self.engine.is_alive():
            self._log("El preprocesado nuevo se aplicará en el próximo Play")

    def _on_fps_change(self, v: float) -> None:
        if self._loading:
            return
        self.task.settings.max_fps = round(v)
        self._mark_dirty()

    def _on_esc_change(self) -> None:
        self.task.settings.esc_panic = self.esc_var.get()
        self.hotkeys.esc_panic = self.esc_var.get()
        self._mark_dirty()

    # ======================================================================= #
    # Ejecución
    # ======================================================================= #
    def _play(self) -> None:
        estado = self.engine.state
        if estado is EngineState.RUNNING:
            self.engine.pause()
            return
        if estado is EngineState.PAUSED:
            self.engine.resume()
            return
        if self.engine.is_alive():
            return  # todavía deteniéndose

        errores = self.task.flow.validate()
        if errores:
            messagebox.showerror("No se puede iniciar", "\n".join(errores), parent=self)
            return
        self.engine.config = self.task.settings.apply_to(self.engine.config)
        self.engine.load_flow(self.task.copy_flow())
        self.engine.set_delta_threshold(self.task.settings.delta_threshold)
        if not self.engine.start():
            self.tabs.set("Registro")

    def _stop(self) -> None:
        self.engine.stop(timeout=0)  # no bloquea la GUI; el estado llega por eventos

    def _panic(self) -> None:
        self.engine.panic()

    def _panic_from_hotkey(self) -> None:
        if not self.engine.is_alive():
            return  # bot parado (p. ej. Esc para cancelar un recorte): nada que detener
        self.engine.panic()          # inmediato, desde el hilo de pynput
        self._ui_queue.put("panic")  # aviso para la GUI

    # ------------------------------ macros ------------------------------ #
    def _record_macro(self) -> None:
        if self._recorder is not None or not self._require_idle("Grabar macro"):
            return
        win = self._target_rect()
        if win is None:
            return
        if not messagebox.askokcancel(
                "Grabar macro",
                "El panel se minimizará y se grabarán tus clics, la rueda del ratón y el "
                "teclado (posiciones relativas a la ventana objetivo).\n\n"
                "Pulsa F10 para terminar.", parent=self):
            return
        self._recorder = MacroRecorder(win, on_stop=lambda: self._ui_queue.put("macro_stop"))
        self.iconify()
        self.after(400, self._recorder.start)  # no grabar el clic en "Aceptar"
        self._log("⏺ Grabando macro… (F10 para terminar)")

    def _finish_macro(self) -> None:
        rec, self._recorder = self._recorder, None
        if rec is None:
            return
        macro = rec.stop()
        self.deiconify()
        self.lift()
        if not macro.events:
            self._log("Macro vacía: no se guardó nada")
            return
        ruta = filedialog.asksaveasfilename(
            parent=self, title="Guardar macro", initialdir=self.macros_dir,
            initialfile="macro.macro", defaultextension=".macro", filetypes=[("Macro", "*.macro")])
        if not ruta:
            self._log("Macro descartada")
            return
        try:
            macro.save(ruta)
        except (OSError, MacroError) as exc:
            messagebox.showerror("Guardar macro", str(exc), parent=self)
            return
        self._log(f"Macro guardada: {ruta} ({len(macro.events)} eventos, {macro.duration:.1f} s). "
                  "Úsala con la acción 'Reproducir macro'.")

    def _recalibrate(self) -> None:
        self.engine.recalibrate()
        if not self.engine.is_alive():
            self._log("El Baseline se tomará al pulsar Play")

    # ======================================================================= #
    # Bucle de eventos (60 FPS)
    # ======================================================================= #
    def _poll(self) -> None:
        try:
            for ev in self.engine.poll_events(200):
                self._handle_event(ev)
            while True:
                orden = self._ui_queue.get_nowait()
                if orden == "toggle" and self._recorder is None:  # no arrancar mientras se graba
                    self._play()
                elif orden == "macro_stop":
                    self._finish_macro()
                elif orden == "panic":
                    self._log("⛔ Pánico activado por hotkey")
                elif orden in ("snip_zones", "snip_templates") and self._can_snip_from_hotkey():
                    (self._snip_zones if orden == "snip_zones" else self._snip_templates)()
        except queue.Empty:
            pass
        except Exception:  # noqa: BLE001 - el bucle de sondeo nunca debe morir
            log.exception("Error procesando eventos")
        finally:
            ahora = time.monotonic()
            if ahora >= self._next_gc:
                self._next_gc = ahora + GC_INTERVAL_S
                gc.collect()  # en el hilo de Tk: los finalizadores de Tk son seguros aquí
            self.after(self.POLL_MS, self._poll)

    def _handle_event(self, ev: EngineEvent) -> None:
        t = ev.type
        if t is EventType.STATE:
            self._set_state_ui(ev.payload)
        elif t is EventType.OBSERVATION:
            self._show_observation(ev.payload)
        elif t is EventType.ERROR:
            self._log(f"✖ {ev.payload}", ev.timestamp)
        elif t is EventType.RULE_FIRED:
            p = ev.payload
            self._log(f"▶ {p['loop']} › {p['proceso']} › {p['regla']}", ev.timestamp)
        elif t is EventType.CALIBRATED:
            self._log(f"Baseline tomado: {ev.payload['zona']}", ev.timestamp)
        elif t is EventType.WINDOW_LOST:
            self._log(f"⚠ Ventana {ev.payload!r} no disponible; esperando…", ev.timestamp)
        elif t is EventType.FINISHED:
            fin = "completada" if ev.payload.get("completado") else "detenida"
            self._log(f"■ Ejecución {fin}", ev.timestamp)
        else:
            self._log(str(ev.payload), ev.timestamp)

    def _set_state_ui(self, estado: EngineState) -> None:
        texto, color, boton = STATE_STYLE[estado]
        self.status.configure(text=texto, text_color=color)
        for b in (self.play_btn, self.home_play):
            b.configure(text=boton)

    def _show_observation(self, obs: dict[str, dict[str, Any]]) -> None:
        st = self.task.settings
        lineas = []
        for nombre, o in obs.items():
            partes = []
            if o["delta"] is not None:
                ok = "✔ BASELINE" if o["baseline"] else "✖ cambió"
                partes.append(f"Δ {o['delta']:.4f} {ok}")
            for tpl, score in o["scores"].items():
                marca = "✔" if score >= st.match_threshold else "·"
                partes.append(f"{tpl} {score:.3f} {marca}")
            lineas.append(f"{nombre:<14}" + "   |   ".join(partes))
        self._set_monitor("\n".join(lineas) or "(sin zonas observadas)")

    def _set_monitor(self, texto: str) -> None:
        self.monitor.configure(state="normal")
        self.monitor.delete("1.0", "end")
        self.monitor.insert("1.0", texto)
        self.monitor.configure(state="disabled")

    def _log(self, texto: str, ts: float | None = None) -> None:
        hora = time.strftime("%H:%M:%S", time.localtime(ts or time.time()))
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{hora}] {texto}\n")
        self._log_lines += 1
        if self._log_lines > MAX_LOG_LINES:
            self.log_box.delete("1.0", "2.0")
            self._log_lines -= 1
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        log.info(texto)

    # ======================================================================= #
    def _on_close(self) -> None:
        if not self._confirm_discard():
            return
        self.hotkeys.stop()
        if self._recorder is not None:
            self._recorder.stop()
        self.engine.stop(timeout=2.0)
        self.destroy()
        gc.enable()
