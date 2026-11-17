"""
block_editor.py - Editor visual de flujos (no-code, estilo App Inventor / Scratch).

    ┌ Árbol ──────────────┐ ┌ Propiedades ───────────────────────────────┐
    │ 🔁 Loop 1 (continuo) │ │ Regla "Acción A3"                           │
    │   ⚙ Proceso 1        │ │  SI ┌ Y ──────────────────────────────────┐ │
    │     ◆ Acción A3      │ │     │ [NO] [Zona 1 ▼] es [Baseline ▼]     │ │
    │     ◆ Regla 2        │ │     │ [NO] [Zona 2 ▼] es [Plantilla B ▼]  │ │
    └─────────────────────┘ │     └─────────────────────────────────────┘ │
                            │  ENTONCES  1. Clic en Zona 2 ...           │
                            └────────────────────────────────────────────┘

Edita directamente los objetos de ``core.rules`` (Flow/Loop/Process/Rule).
Cada cambio válido se aplica al instante y se notifica con ``on_change``; los
valores inválidos se marcan en rojo y no se aplican.
"""
from __future__ import annotations

import copy
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

import customtkinter as ctk

from core.rules import (
    BASELINE, And, Condition, Flow, Loop, Not, Or, Process, Rule, Trigger, ZoneIs,
    condition_to_text, parse_expression,
)

__all__ = ["FlowEditor"]

COLOR_LOOP = "#C88330"
COLOR_PROCESS = "#4C97FF"
COLOR_RULE = "#59C059"
COLOR_AND = "#5b3f99"
COLOR_OR = "#99601f"
COLOR_LEAF = "#2b2b2b"
COLOR_ERROR = "#e05555"
COLOR_OK_BORDER = "#565b5e"

BASELINE_LABEL = "Baseline"
WINDOW_LABEL = "(ventana completa)"
NO_TEMPLATE = "(ninguna)"


def _unique(prefix: str, usados: set[str]) -> str:
    i = 1
    while f"{prefix} {i}" in usados:
        i += 1
    return f"{prefix} {i}"


# =========================================================================== #
# Campos con validación
# =========================================================================== #
def _bind_entry(entry: ctk.CTkEntry, var: tk.StringVar, parse: Callable[[str], Any],
                apply: Callable[[Any], None]) -> None:
    """Valida en cada pulsación: si ``parse`` lanza ValueError el borde se pone rojo."""
    def _on_change(*_a: object) -> None:
        try:
            valor = parse(var.get())
        except (ValueError, TypeError):
            entry.configure(border_color=COLOR_ERROR)
            return
        entry.configure(border_color=COLOR_OK_BORDER)
        apply(valor)
    var.trace_add("write", _on_change)


def _parse_name(s: str) -> str:
    s = s.strip()
    if not s:
        raise ValueError("vacío")
    return s


def _float_in(lo: float, hi: float, optional: bool = False) -> Callable[[str], float | None]:
    def _p(s: str) -> float | None:
        s = s.strip().replace(",", ".")
        if not s and optional:
            return None
        v = float(s)
        if not lo <= v <= hi:
            raise ValueError
        return v
    return _p


def _int_min1(s: str) -> int:
    v = int(s.strip())
    if v < 1:
        raise ValueError
    return v


def _entry(parent: Any, value: Any, parse: Callable[[str], Any], apply: Callable[[Any], None],
           width: int = 70, placeholder: str = "") -> ctk.CTkEntry:
    var = tk.StringVar(value="" if value is None else str(value))
    e = ctk.CTkEntry(parent, textvariable=var, width=width, placeholder_text=placeholder)
    _bind_entry(e, var, parse, apply)
    e._var = var  # type: ignore[attr-defined]  # mantener viva la variable
    return e


def _range_fields(parent: Any, label: str, current: tuple[float, float],
                  apply: Callable[[tuple[float, float]], None]) -> ctk.CTkFrame:
    """Dos campos [mín, máx] en segundos; sólo aplica si 0 <= mín <= máx."""
    fila = ctk.CTkFrame(parent, fg_color="transparent")
    ctk.CTkLabel(fila, text=label).pack(side="left", padx=(0, 6))
    valores = list(current)
    entradas: list[ctk.CTkEntry] = []

    def _set(i: int, v: float) -> None:
        valores[i] = v
        ok = 0 <= valores[0] <= valores[1]
        for e in entradas:
            e.configure(border_color=COLOR_OK_BORDER if ok else COLOR_ERROR)
        if ok:
            apply((valores[0], valores[1]))

    for i, texto in ((0, "mín"), (1, "máx")):
        ctk.CTkLabel(fila, text=texto, text_color="gray70").pack(side="left", padx=(6, 2))
        e = _entry(fila, current[i], _float_in(0, 3600), lambda v, i=i: _set(i, v), width=64)
        e.pack(side="left")
        entradas.append(e)
    ctk.CTkLabel(fila, text="s", text_color="gray70").pack(side="left", padx=(4, 0))
    return fila


def _section(parent: Any, title: str, color: str) -> ctk.CTkFrame:
    ctk.CTkLabel(parent, text=title, font=ctk.CTkFont(size=13, weight="bold"),
                 text_color=color).pack(anchor="w", padx=8, pady=(14, 4))
    f = ctk.CTkFrame(parent)
    f.pack(fill="x", padx=8, pady=(0, 4))
    return f


# =========================================================================== #
# Constructor de condiciones
# =========================================================================== #
@dataclass
class _Leaf:
    zone: str
    target: str
    negate: bool = False
    threshold: float | None = None


@dataclass
class _Group:
    op: str = "Y"
    children: list[Any] = field(default_factory=list)
    negate: bool = False


def _to_edit(cond: Condition) -> _Group:
    def conv(c: Condition, negate: bool = False) -> Any:
        if isinstance(c, Not):
            return conv(c.item, not negate)
        if isinstance(c, ZoneIs):
            return _Leaf(c.zone, c.target, negate, c.threshold)
        if isinstance(c, (And, Or)):
            return _Group("Y" if isinstance(c, And) else "O", [conv(x) for x in c.items], negate)
        raise TypeError(c)
    nodo = conv(cond)
    return nodo if isinstance(nodo, _Group) else _Group("Y", [nodo])


def _from_edit(nodo: Any) -> Condition:
    if isinstance(nodo, _Leaf):
        if not nodo.zone:
            raise ValueError("Hay una condición sin zona")
        c: Condition = ZoneIs(nodo.zone, nodo.target, nodo.threshold)
    else:
        if not nodo.children:
            raise ValueError("Hay un grupo vacío: añade una condición o elimínalo")
        items = tuple(_from_edit(h) for h in nodo.children)
        c = items[0] if len(items) == 1 else (And(items) if nodo.op == "Y" else Or(items))
    return Not(c) if nodo.negate else c


class ConditionBuilder(ctk.CTkFrame):
    def __init__(self, master: Any, rule: Rule, zones: list[str], templates: list[str],
                 on_change: Callable[[], None]) -> None:
        super().__init__(master, fg_color="transparent")
        self.rule = rule
        self.zones = zones
        self.templates = templates
        self.on_change = on_change
        self.root = _to_edit(rule.condition)

        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="x")
        self.preview = ctk.CTkLabel(self, text="", anchor="w", justify="left",
                                    text_color="gray70", wraplength=640)
        self.preview.pack(fill="x", padx=4, pady=(6, 0))
        self.error = ctk.CTkLabel(self, text="", text_color=COLOR_ERROR, anchor="w")
        self.error.pack(fill="x", padx=4)

        texto = ctk.CTkFrame(self, fg_color="transparent")
        texto.pack(fill="x", pady=(4, 0))
        self.text_var = tk.StringVar()
        ctk.CTkEntry(texto, textvariable=self.text_var,
                     placeholder_text='Modo texto: (Zona 1 == Baseline) Y (Zona 2 == "Plantilla B")'
                     ).pack(side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkButton(texto, text="Aplicar texto", width=110, command=self._apply_text).pack(side="left")

        self._render()
        self._update_preview(rule.condition)

    # ---------------------------- render ----------------------------- #
    def _render(self) -> None:
        for w in self.body.winfo_children():
            w.destroy()
        self._render_group(self.body, self.root, None, 0)

    def _render_group(self, container: Any, g: _Group, parent: _Group | None, depth: int) -> None:
        color = COLOR_AND if g.op == "Y" else COLOR_OR
        frame = ctk.CTkFrame(container, fg_color=color, corner_radius=8)
        frame.pack(fill="x", padx=(0 if depth == 0 else 18, 0), pady=3)
        head = ctk.CTkFrame(frame, fg_color="transparent")
        head.pack(fill="x", padx=6, pady=(6, 2))

        neg = tk.BooleanVar(value=g.negate)
        ctk.CTkCheckBox(head, text="NO", width=50, variable=neg,
                        command=lambda: self._set(g, "negate", neg.get())).pack(side="left")
        op = ctk.CTkSegmentedButton(head, values=["Y", "O"], width=80,
                                    command=lambda v: (self._set(g, "op", v), self._render()))
        op.set(g.op)
        op.pack(side="left", padx=6)
        ctk.CTkLabel(head, text="se cumplen TODAS" if g.op == "Y" else "se cumple ALGUNA",
                     text_color="gray85").pack(side="left", padx=4)
        if parent is not None:
            ctk.CTkButton(head, text="✕", width=28, fg_color="transparent", hover_color="#7a2f2f",
                          command=lambda: self._remove(parent, g)).pack(side="right")
        ctk.CTkButton(head, text="+ Grupo", width=70, fg_color="#3a3a3a",
                      command=lambda: self._add(g, _Group("O", [self._new_leaf()]))).pack(side="right", padx=3)
        ctk.CTkButton(head, text="+ Condición", width=90, fg_color="#3a3a3a",
                      command=lambda: self._add(g, self._new_leaf())).pack(side="right", padx=3)

        hijos = ctk.CTkFrame(frame, fg_color="transparent")
        hijos.pack(fill="x", padx=6, pady=(0, 6))
        for h in g.children:
            if isinstance(h, _Group):
                self._render_group(hijos, h, g, depth + 1)
            else:
                self._render_leaf(hijos, h, g)

    def _render_leaf(self, container: Any, leaf: _Leaf, parent: _Group) -> None:
        fila = ctk.CTkFrame(container, fg_color=COLOR_LEAF, corner_radius=6)
        fila.pack(fill="x", pady=2)
        neg = tk.BooleanVar(value=leaf.negate)
        ctk.CTkCheckBox(fila, text="NO", width=50, variable=neg,
                        command=lambda: self._set(leaf, "negate", neg.get())).pack(side="left", padx=(6, 2), pady=4)

        zonas = self.zones or ["(sin zonas)"]
        zona = ctk.CTkOptionMenu(fila, values=zonas, width=130,
                                 command=lambda v: self._set(leaf, "zone", v if self.zones else ""))
        zona.set(leaf.zone or zonas[0])
        zona.pack(side="left", padx=4)
        ctk.CTkLabel(fila, text="es").pack(side="left", padx=4)

        objetivos = [BASELINE_LABEL] + self.templates
        obj = ctk.CTkOptionMenu(
            fila, values=objetivos, width=150,
            command=lambda v: self._set(leaf, "target", BASELINE if v == BASELINE_LABEL else v))
        obj.set(BASELINE_LABEL if leaf.target == BASELINE else leaf.target)
        obj.pack(side="left", padx=4)

        _entry(fila, leaf.threshold, _float_in(-1, 1, optional=True),
               lambda v: self._set(leaf, "threshold", v), width=70,
               placeholder="umbral").pack(side="left", padx=4)
        ctk.CTkButton(fila, text="✕", width=28, fg_color="transparent", hover_color="#7a2f2f",
                      command=lambda: self._remove(parent, leaf)).pack(side="right", padx=4)

    # --------------------------- edición ------------------------------ #
    def _new_leaf(self) -> _Leaf:
        return _Leaf(self.zones[0] if self.zones else "", BASELINE)

    def _set(self, nodo: Any, attr: str, valor: Any) -> None:
        setattr(nodo, attr, valor)
        self._commit()

    def _add(self, g: _Group, nodo: Any) -> None:
        g.children.append(nodo)
        self._render()
        self._commit()

    def _remove(self, parent: _Group, nodo: Any) -> None:
        parent.children.remove(nodo)
        self._render()
        self._commit()

    def _apply_text(self) -> None:
        try:
            cond = parse_expression(self.text_var.get())
        except ValueError as exc:
            self.error.configure(text=f"Texto inválido: {exc}")
            return
        self.root = _to_edit(cond)
        self.text_var.set("")
        self._render()
        self._commit()

    def _commit(self) -> None:
        try:
            cond = _from_edit(self.root)
        except ValueError as exc:
            self.error.configure(text=str(exc))
            return
        self.error.configure(text="")
        self.rule.condition = cond
        self.rule.reset()
        self._update_preview(cond)
        self.on_change()

    def _update_preview(self, cond: Condition) -> None:
        self.preview.configure(text=f"SI  {condition_to_text(cond)}")


# =========================================================================== #
# Editor de acciones
# =========================================================================== #
ACTION_LABELS = {
    "click": "Clic",
    "tecla": "Pulsar tecla",
    "texto": "Escribir texto",
    "esperar": "Esperar",
    "macro": "Reproducir macro",
    "exe": "Ejecutable (.exe)",
}
_LABEL_TO_TYPE = {v: k for k, v in ACTION_LABELS.items()}
ACTION_DEFAULTS: dict[str, dict[str, Any]] = {
    "click": {"tipo": "click", "zona": None, "plantilla": None, "x": 0.5, "y": 0.5, "boton": "left"},
    "tecla": {"tipo": "tecla", "tecla": "space"},
    "texto": {"tipo": "texto", "texto": ""},
    "esperar": {"tipo": "esperar", "min": 0.2, "max": 0.5},
    "macro": {"tipo": "macro", "archivo": "", "velocidad": 1.0},
    "exe": {"tipo": "exe", "ruta": ""},
}


class ActionsEditor(ctk.CTkFrame):
    def __init__(self, master: Any, rule: Rule, zones: list[str], templates: list[str],
                 on_change: Callable[[], None], base_dir: Path | None = None) -> None:
        super().__init__(master, fg_color="transparent")
        self.base_dir = base_dir or Path.cwd()
        self.rule = rule
        self.zones = zones
        self.templates = templates
        self.on_change = on_change
        self.list = ctk.CTkFrame(self, fg_color="transparent")
        self.list.pack(fill="x")
        ctk.CTkButton(self, text="+ Añadir acción", width=140,
                      command=self._add).pack(anchor="w", pady=(4, 0))
        self._render()

    def _render(self) -> None:
        for w in self.list.winfo_children():
            w.destroy()
        if not self.rule.actions:
            ctk.CTkLabel(self.list, text="(sin acciones: la regla sólo se registrará)",
                         text_color="gray60").pack(anchor="w", padx=4)
        for i, accion in enumerate(self.rule.actions):
            self._render_row(i, accion)

    def _render_row(self, i: int, a: dict[str, Any]) -> None:
        fila = ctk.CTkFrame(self.list, fg_color="#24402a", corner_radius=6)
        fila.pack(fill="x", pady=2)
        ctk.CTkLabel(fila, text=f"{i + 1}.", width=24).pack(side="left", padx=(6, 0), pady=4)
        tipo = ctk.CTkOptionMenu(fila, values=list(ACTION_LABELS.values()), width=140,
                                 command=lambda v: self._change_type(i, _LABEL_TO_TYPE[v]))
        tipo.set(ACTION_LABELS.get(a.get("tipo", ""), "Clic"))
        tipo.pack(side="left", padx=4)

        def setter(clave: str) -> Callable[[Any], None]:
            def _s(v: Any) -> None:
                a[clave] = v
                self.on_change()
            return _s

        t = a.get("tipo")
        if t == "click":
            zonas = [WINDOW_LABEL] + self.zones
            z = ctk.CTkOptionMenu(fila, values=zonas, width=130,
                                  command=lambda v: setter("zona")(None if v == WINDOW_LABEL else v))
            z.set(a.get("zona") or WINDOW_LABEL)
            z.pack(side="left", padx=4)
            plantillas = [NO_TEMPLATE] + self.templates
            p = ctk.CTkOptionMenu(fila, values=plantillas, width=130,
                                  command=lambda v: setter("plantilla")(None if v == NO_TEMPLATE else v))
            p.set(a.get("plantilla") or NO_TEMPLATE)
            p.pack(side="left", padx=4)
            for clave in ("x", "y"):
                ctk.CTkLabel(fila, text=clave).pack(side="left", padx=(4, 1))
                _entry(fila, a.get(clave, 0.5), _float_in(0, 1), setter(clave), width=52).pack(side="left")
            b = ctk.CTkOptionMenu(fila, values=["left", "right", "middle"], width=80,
                                  command=setter("boton"))
            b.set(a.get("boton", "left"))
            b.pack(side="left", padx=4)
        elif t == "tecla":
            _entry(fila, a.get("tecla", ""), _parse_name, setter("tecla"), width=120,
                   placeholder="space, enter, a...").pack(side="left", padx=4)
        elif t == "texto":
            _entry(fila, a.get("texto", ""), str, setter("texto"), width=260).pack(side="left", padx=4)
        elif t == "esperar":
            _range_fields(fila, "", (float(a.get("min", 0)), float(a.get("max", 0))),
                          lambda r: (a.update(min=r[0], max=r[1]), self.on_change())).pack(side="left", padx=4)
        elif t == "macro":
            archivo = _entry(fila, a.get("archivo", ""), _parse_name, setter("archivo"), width=230,
                             placeholder="macros/mi_macro.macro")
            archivo.pack(side="left", padx=4)
            ctk.CTkButton(fila, text="…", width=30, fg_color="#3a3a3a",
                          command=lambda: self._pick_macro(archivo)).pack(side="left")
            ctk.CTkLabel(fila, text="velocidad").pack(side="left", padx=(8, 2))
            _entry(fila, a.get("velocidad", 1.0), _float_in(0.1, 10), setter("velocidad"),
                   width=52).pack(side="left")
        elif t == "exe":
            _entry(fila, a.get("ruta", ""), str, setter("ruta"), width=220,
                   placeholder="C:/ruta/programa.exe").pack(side="left", padx=4)
            ctk.CTkLabel(fila, text="reservado: aún no se ejecuta",
                         text_color="gray60").pack(side="left", padx=4)

        for texto, cmd in (("✕", lambda: self._remove(i)), ("↓", lambda: self._move(i, 1)),
                           ("↑", lambda: self._move(i, -1))):
            ctk.CTkButton(fila, text=texto, width=28, fg_color="transparent",
                          command=cmd).pack(side="right", padx=1)

    def _pick_macro(self, entry: ctk.CTkEntry) -> None:
        carpeta = self.base_dir / "macros"
        ruta = filedialog.askopenfilename(parent=self, title="Elegir macro",
                                          initialdir=carpeta if carpeta.is_dir() else self.base_dir,
                                          filetypes=[("Macro", "*.macro"), ("Todos", "*.*")])
        if not ruta:
            return
        p = Path(ruta)
        try:
            p = p.resolve().relative_to(self.base_dir.resolve())
        except ValueError:
            pass
        entry._var.set(p.as_posix())  # type: ignore[attr-defined]  # dispara la validación

    def _add(self) -> None:
        self.rule.actions.append(copy.deepcopy(ACTION_DEFAULTS["click"]))
        self._render()
        self.on_change()

    def _change_type(self, i: int, tipo: str) -> None:
        self.rule.actions[i] = copy.deepcopy(ACTION_DEFAULTS[tipo])
        self._render()
        self.on_change()

    def _remove(self, i: int) -> None:
        del self.rule.actions[i]
        self._render()
        self.on_change()

    def _move(self, i: int, d: int) -> None:
        j = i + d
        if 0 <= j < len(self.rule.actions):
            acts = self.rule.actions
            acts[i], acts[j] = acts[j], acts[i]
            self._render()
            self.on_change()


# =========================================================================== #
# Editor completo
# =========================================================================== #
class FlowEditor(ctk.CTkFrame):
    def __init__(self, master: Any, on_change: Callable[[], None], base_dir: Path | None = None) -> None:
        super().__init__(master, fg_color="transparent")
        self.on_change = on_change
        self.base_dir = base_dir or Path.cwd()
        self.flow: Flow | None = None
        self._nodes: dict[str, tuple[Any, list[Any]]] = {}  # iid -> (objeto, lista contenedora)
        self._current: str | None = None

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        izq = ctk.CTkFrame(self, width=290)
        izq.grid(row=0, column=0, sticky="nsw", padx=(0, 8))
        izq.grid_propagate(False)
        izq.grid_rowconfigure(1, weight=1)
        izq.grid_columnconfigure(0, weight=1)

        botones = ctk.CTkFrame(izq, fg_color="transparent")
        botones.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
        for i, (texto, cmd, color) in enumerate((
            ("+ Loop", self._add_loop, COLOR_LOOP),
            ("+ Proceso", self._add_process, COLOR_PROCESS),
            ("+ Regla", self._add_rule, "#3d8a3d"),
        )):
            ctk.CTkButton(botones, text=texto, width=84, fg_color=color, command=cmd
                          ).grid(row=0, column=i, padx=2, pady=2)
        for i, (texto, cmd) in enumerate((("↑", lambda: self._move(-1)), ("↓", lambda: self._move(1)),
                                          ("Duplicar", self._duplicate))):
            ctk.CTkButton(botones, text=texto, width=84, fg_color="#3a3a3a", command=cmd
                          ).grid(row=1, column=i, padx=2, pady=2)
        ctk.CTkButton(botones, text="🗑 Eliminar", fg_color="#8a3b3b", command=self._delete
                      ).grid(row=2, column=0, columnspan=3, sticky="ew", padx=2, pady=2)

        self._style_tree()
        self.tree = ttk.Treeview(izq, show="tree", selectmode="browse", style="Flow.Treeview")
        self.tree.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 6))
        self.tree.tag_configure("loop", foreground=COLOR_LOOP)
        self.tree.tag_configure("process", foreground=COLOR_PROCESS)
        self.tree.tag_configure("rule", foreground=COLOR_RULE)
        self.tree.tag_configure("off", foreground="gray50")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._on_select())

        self.panel = ctk.CTkScrollableFrame(self)
        self.panel.grid(row=0, column=1, sticky="nsew")

    @staticmethod
    def _style_tree() -> None:
        st = ttk.Style()
        st.theme_use("clam")
        st.configure("Flow.Treeview", background="#1d1e1e", fieldbackground="#1d1e1e",
                     foreground="#dce4ee", borderwidth=0, rowheight=28, font=("Segoe UI", 10))
        st.map("Flow.Treeview", background=[("selected", "#1f538d")],
               foreground=[("selected", "white")])

    # ------------------------------ datos ----------------------------- #
    def set_flow(self, flow: Flow) -> None:
        self.flow = flow
        self._current = None
        self.refresh()

    @property
    def zones(self) -> list[str]:
        return [z.name for z in self.flow.zones] if self.flow else []

    @property
    def templates(self) -> list[str]:
        return list(self.flow.templates) if self.flow else []

    def refresh(self, keep: Any = None) -> None:
        """Reconstruye el árbol (p. ej. tras cambiar zonas o plantillas)."""
        objetivo = keep if keep is not None else (self._nodes.get(self._current, (None,))[0]
                                                  if self._current else None)
        self.tree.delete(*self.tree.get_children())
        self._nodes.clear()
        if self.flow is None:
            self._show(None)
            return
        seleccion = None
        for loop in self.flow.loops:
            li = self._insert("", loop, self.flow.loops)
            for proc in loop.processes:
                pi = self._insert(li, proc, loop.processes)
                for regla in proc.rules:
                    ri = self._insert(pi, regla, proc.rules)
                    seleccion = ri if regla is objetivo else seleccion
                seleccion = pi if proc is objetivo else seleccion
            seleccion = li if loop is objetivo else seleccion
        if seleccion is None and self.flow.loops:
            seleccion = self.tree.get_children()[0]
        if seleccion:
            self.tree.see(seleccion)
            self.tree.selection_set(seleccion)
            self._on_select(force=True)
        else:
            self._show(None)

    def _insert(self, parent: str, obj: Any, container: list[Any]) -> str:
        iid = self.tree.insert(parent, "end", text=self._label(obj), open=True, tags=self._tags(obj))
        self._nodes[iid] = (obj, container)
        return iid

    @staticmethod
    def _label(obj: Any) -> str:
        if isinstance(obj, Loop):
            modo = "continuo" if obj.continuous else f"×{obj.iterations}"
            return f"🔁 {obj.name}  ({modo})"
        if isinstance(obj, Process):
            return f"⚙ {obj.name}" + ("" if obj.enabled else "  (inactivo)")
        return f"◆ {obj.name}" + ("" if obj.enabled else "  (inactiva)")

    @staticmethod
    def _tags(obj: Any) -> tuple[str, ...]:
        base = "loop" if isinstance(obj, Loop) else "process" if isinstance(obj, Process) else "rule"
        return (base,) if getattr(obj, "enabled", True) else (base, "off")

    def _relabel(self) -> None:
        if self._current and self._current in self._nodes:
            obj = self._nodes[self._current][0]
            self.tree.item(self._current, text=self._label(obj), tags=self._tags(obj))

    def _changed(self) -> None:
        self._relabel()
        self.on_change()

    # --------------------------- selección ---------------------------- #
    def _selected(self) -> tuple[str, Any, list[Any]] | None:
        sel = self.tree.selection()
        if not sel or sel[0] not in self._nodes:
            return None
        obj, cont = self._nodes[sel[0]]
        return sel[0], obj, cont

    def _parent_obj(self, iid: str) -> Any:
        p = self.tree.parent(iid)
        return self._nodes[p][0] if p else None

    def _on_select(self, force: bool = False) -> None:
        sel = self._selected()
        iid = sel[0] if sel else None
        if iid == self._current and not force:
            return
        self._current = iid
        self._show(sel[1] if sel else None)

    # ----------------------------- árbol ------------------------------ #
    def _add_loop(self) -> None:
        if self.flow is None:
            return
        loop = Loop(_unique("Loop", {l.name for l in self.flow.loops}), [Process("Proceso 1")],
                    delay=(0.3, 0.8))
        self.flow.loops.append(loop)
        self.refresh(keep=loop)
        self.on_change()

    def _add_process(self) -> None:
        if self.flow is None:
            return
        sel = self._selected()
        loop: Loop | None = None
        if sel:
            obj = sel[1]
            loop = obj if isinstance(obj, Loop) else self._parent_obj(sel[0])
            if isinstance(loop, Process):
                loop = self._parent_obj(self.tree.parent(sel[0]))
        if loop is None:
            if not self.flow.loops:
                self._add_loop()
                return
            loop = self.flow.loops[0]
        proc = Process(_unique("Proceso", {p.name for l in self.flow.loops for p in l.processes}))
        loop.processes.append(proc)
        self.refresh(keep=proc)
        self.on_change()

    def _add_rule(self) -> None:
        if self.flow is None:
            return
        sel = self._selected()
        proc: Process | None = None
        if sel:
            obj = sel[1]
            proc = obj if isinstance(obj, Process) else self._parent_obj(sel[0]) if isinstance(obj, Rule) else None
            if isinstance(obj, Loop) and obj.processes:
                proc = obj.processes[-1]
        if proc is None:
            messagebox.showinfo("Nueva regla", "Selecciona primero un Proceso.", parent=self)
            return
        zona = self.zones[0] if self.zones else "Zona 1"
        regla = Rule(_unique("Regla", {r.name for r in self.flow.iter_rules()}),
                     ZoneIs(zona, BASELINE), delay=(0.15, 0.45))
        proc.rules.append(regla)
        self.refresh(keep=regla)
        self.on_change()

    def _move(self, d: int) -> None:
        sel = self._selected()
        if not sel:
            return
        _, obj, cont = sel
        i = cont.index(obj)
        j = i + d
        if 0 <= j < len(cont):
            cont[i], cont[j] = cont[j], cont[i]
            self.refresh(keep=obj)
            self.on_change()

    def _duplicate(self) -> None:
        sel = self._selected()
        if not sel or self.flow is None:
            return
        _, obj, cont = sel
        nuevo = type(obj).from_dict(obj.to_dict())
        nuevo.name = f"{obj.name} (copia)"
        cont.insert(cont.index(obj) + 1, nuevo)
        self.refresh(keep=nuevo)
        self.on_change()

    def _delete(self) -> None:
        sel = self._selected()
        if not sel:
            return
        _, obj, cont = sel
        hijos = len(getattr(obj, "processes", None) or getattr(obj, "rules", None) or [])
        msg = f"¿Eliminar {obj.name!r}" + (f" y todo su contenido ({hijos})?" if hijos else "?")
        if not messagebox.askyesno("Eliminar", msg, parent=self):
            return
        i = cont.index(obj)
        cont.remove(obj)
        vecino = cont[min(i, len(cont) - 1)] if cont else None
        self._current = None
        self.refresh(keep=vecino)
        self.on_change()

    # ---------------------------- paneles ----------------------------- #
    def _show(self, obj: Any) -> None:
        for w in self.panel.winfo_children():
            w.destroy()
        if obj is None:
            ctk.CTkLabel(self.panel, text="Selecciona un elemento del árbol o crea un Loop.",
                         text_color="gray60").pack(pady=30)
        elif isinstance(obj, Loop):
            self._panel_loop(obj)
        elif isinstance(obj, Process):
            self._panel_process(obj)
        else:
            self._panel_rule(obj)

    def _name_field(self, parent: Any, obj: Any) -> None:
        fila = ctk.CTkFrame(parent, fg_color="transparent")
        fila.pack(fill="x", padx=8, pady=6)
        ctk.CTkLabel(fila, text="Nombre", width=110, anchor="w").pack(side="left")
        _entry(fila, obj.name, _parse_name, lambda v: (setattr(obj, "name", v), self._changed()),
               width=260).pack(side="left")

    def _switch(self, parent: Any, texto: str, valor: bool, apply: Callable[[bool], None]) -> None:
        var = tk.BooleanVar(value=valor)
        ctk.CTkSwitch(parent, text=texto, variable=var,
                      command=lambda: (apply(var.get()), self._changed())).pack(anchor="w", padx=8, pady=4)

    def _panel_loop(self, loop: Loop) -> None:
        f = _section(self.panel, "LOOP", COLOR_LOOP)
        self._name_field(f, loop)

        fila = ctk.CTkFrame(f, fg_color="transparent")
        fila.pack(fill="x", padx=8, pady=6)
        continuo = tk.BooleanVar(value=loop.continuous)
        it_entry = _entry(fila, loop.iterations or 10, _int_min1,
                          lambda v: (setattr(loop, "iterations", v) if not continuo.get() else None,
                                     self._changed()), width=70)

        def _toggle() -> None:
            if continuo.get():
                loop.iterations = None
                it_entry.configure(state="disabled")
            else:
                try:
                    loop.iterations = max(1, int(it_entry.get()))
                except ValueError:
                    loop.iterations = 10
                it_entry.configure(state="normal")
            self._changed()

        ctk.CTkSwitch(fila, text="Continuo", variable=continuo, command=_toggle).pack(side="left")
        ctk.CTkLabel(fila, text="  Iteraciones").pack(side="left", padx=(16, 6))
        it_entry.pack(side="left")
        if continuo.get():
            it_entry.configure(state="disabled")

        _range_fields(f, "Delay entre iteraciones", loop.delay,
                      lambda r: (setattr(loop, "delay", r), self._changed())).pack(anchor="w", padx=8, pady=6)
        ctk.CTkLabel(f, text="Cada iteración: captura → evalúa los procesos → espera el delay entrópico.",
                     text_color="gray60").pack(anchor="w", padx=8, pady=(0, 8))

    def _panel_process(self, proc: Process) -> None:
        f = _section(self.panel, "PROCESO", COLOR_PROCESS)
        self._name_field(f, proc)
        self._switch(f, "Activo", proc.enabled, lambda v: setattr(proc, "enabled", v))
        self._switch(f, "Exclusivo (sólo la primera regla verdadera por ciclo)", proc.exclusive,
                     lambda v: setattr(proc, "exclusive", v))
        ctk.CTkLabel(f, text=f"{len(proc.rules)} regla(s). Usa '+ Regla' para añadir.",
                     text_color="gray60").pack(anchor="w", padx=8, pady=(0, 8))

    def _panel_rule(self, rule: Rule) -> None:
        f = _section(self.panel, "REGLA", COLOR_RULE)
        self._name_field(f, rule)
        self._switch(f, "Activa", rule.enabled, lambda v: setattr(rule, "enabled", v))

        fila = ctk.CTkFrame(f, fg_color="transparent")
        fila.pack(fill="x", padx=8, pady=4)
        ctk.CTkLabel(fila, text="Disparo", width=110, anchor="w").pack(side="left")
        etiquetas = {"Por flanco (una vez)": Trigger.EDGE, "Por nivel (cada ciclo)": Trigger.LEVEL}
        seg = ctk.CTkSegmentedButton(
            fila, values=list(etiquetas),
            command=lambda v: (setattr(rule, "trigger", etiquetas[v]), rule.reset(), self._changed()))
        seg.set(next(k for k, v in etiquetas.items() if v is rule.trigger))
        seg.pack(side="left")
        _range_fields(f, "Delay antes de actuar", rule.delay,
                      lambda r: (setattr(rule, "delay", r), self._changed())).pack(anchor="w", padx=8, pady=(6, 10))

        c = _section(self.panel, "SI  (condición)", "#b48cff")
        ConditionBuilder(c, rule, self.zones, self.templates, self._changed).pack(fill="x", padx=8, pady=8)

        a = _section(self.panel, "ENTONCES  (acciones, en orden)", COLOR_RULE)
        ActionsEditor(a, rule, self.zones, self.templates, self._changed,
                      base_dir=self.base_dir).pack(fill="x", padx=8, pady=8)
