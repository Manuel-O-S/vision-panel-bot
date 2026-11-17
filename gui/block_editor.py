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
from gui.code_editor import CodeEditor

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
# Claves del menú (no siempre = "tipo": "mantener_der" es {"tipo": "mantener", "boton": "right"}).
ACTION_LABELS = {
    "click": "Clic",
    "mantener": "Mantener clic",
    "mantener_der": "Mantener clic derecho",
    "autoclick": "Autoclick",
    "autoclick_der": "Autoclick derecho",
    "tecla": "Pulsar tecla",
    "texto": "Escribir texto",
    "esperar": "Esperar",
    "macro": "Reproducir macro",
    "accion": "Ejecutar acción (A1…)",
    "codigo": "Código Python",
    "exe": "Ejecutable (.exe)",
}
_LABEL_TO_TYPE = {v: k for k, v in ACTION_LABELS.items()}
ACTION_DEFAULTS: dict[str, dict[str, Any]] = {
    "click": {"tipo": "click", "zona": None, "plantilla": None, "x": 0.5, "y": 0.5, "boton": "left"},
    "mantener": {"tipo": "mantener", "zona": None, "plantilla": None, "x": 0.5, "y": 0.5,
                 "boton": "left", "min": 0.5, "max": 1.0},
    "mantener_der": {"tipo": "mantener", "zona": None, "plantilla": None, "x": 0.5, "y": 0.5,
                     "boton": "right", "min": 0.5, "max": 1.0},
    "autoclick": {"tipo": "autoclick", "zona": None, "plantilla": None, "x": 0.5, "y": 0.5,
                  "boton": "left", "cps": 10, "min": 1.0, "max": 2.0},
    "autoclick_der": {"tipo": "autoclick", "zona": None, "plantilla": None, "x": 0.5, "y": 0.5,
                      "boton": "right", "cps": 10, "min": 1.0, "max": 2.0},
    "tecla": {"tipo": "tecla", "tecla": "space"},
    "texto": {"tipo": "texto", "texto": ""},
    "esperar": {"tipo": "esperar", "min": 0.2, "max": 0.5},
    "macro": {"tipo": "macro", "archivo": "", "velocidad": 1.0},
    "accion": {"tipo": "accion", "nombre": ""},
    "codigo": {"tipo": "codigo", "codigo": ""},
    "exe": {"tipo": "exe", "ruta": ""},
}
NO_LIBRARY = "(biblioteca vacía)"


def action_menu_key(a: dict[str, Any]) -> str:
    """Acción -> clave de ACTION_LABELS (distingue las variantes de botón derecho)."""
    t = str(a.get("tipo", ""))
    if t in ("mantener", "autoclick") and a.get("boton") == "right":
        return f"{t}_der"
    return t if t in ACTION_LABELS else "click"


def _first_line(code: str) -> str:
    lineas = [l.strip() for l in code.splitlines() if l.strip() and not l.strip().startswith("#")]
    if not lineas:
        return "(vacío)"
    extra = f"  (+{len(lineas) - 1} líneas)" if len(lineas) > 1 else ""
    return (lineas[0][:40] + "…" if len(lineas[0]) > 40 else lineas[0]) + extra


class ActionsEditor(ctk.CTkFrame):
    """Lista editable de acciones (de una regla o de la biblioteca A1, A2…)."""

    def __init__(self, master: Any, actions: list[dict[str, Any]], zones: list[str],
                 templates: list[str], on_change: Callable[[], None], base_dir: Path | None = None,
                 library: list[str] | None = None, owner: str = "", exclude: str | None = None,
                 empty_text: str = "(sin acciones: la regla sólo se registrará)") -> None:
        super().__init__(master, fg_color="transparent")
        self.base_dir = base_dir or Path.cwd()
        self.actions = actions
        self.zones = zones
        self.templates = templates
        self.library = [n for n in (library or []) if n != exclude]  # A1 no puede invocarse a sí misma
        self.owner = owner
        self.empty_text = empty_text
        self.on_change = on_change
        self.list = ctk.CTkFrame(self, fg_color="transparent")
        self.list.pack(fill="x")
        botones = ctk.CTkFrame(self, fg_color="transparent")
        botones.pack(anchor="w", pady=(4, 0))
        ctk.CTkButton(botones, text="+ Añadir acción", width=140, command=self._add).pack(side="left")
        ctk.CTkButton(botones, text="</> Insertar código", width=150, fg_color="#6b4fb0",
                      command=self._insert_code).pack(side="left", padx=8)
        self._render()

    def _render(self) -> None:
        for w in self.list.winfo_children():
            w.destroy()
        if not self.actions:
            ctk.CTkLabel(self.list, text=self.empty_text, text_color="gray60").pack(anchor="w", padx=4)
        for i, accion in enumerate(self.actions):
            self._render_row(i, accion)

    def _insert_code(self) -> None:
        self.actions.append(copy.deepcopy(ACTION_DEFAULTS["codigo"]))
        i = len(self.actions) - 1
        self._render()
        self.on_change()
        self._edit_code(i)

    def _edit_code(self, i: int) -> None:
        accion = self.actions[i]

        def guardar(codigo: str) -> None:
            accion["codigo"] = codigo
            self._render()
            self.on_change()

        CodeEditor(self, accion.get("codigo", ""), guardar,
                   title=f"Insertar código — {self.owner or 'acción'} #{i + 1}")

    def _render_row(self, i: int, a: dict[str, Any]) -> None:
        fila = ctk.CTkFrame(self.list, fg_color="#24402a", corner_radius=6)
        fila.pack(fill="x", pady=2)
        ctk.CTkLabel(fila, text=f"{i + 1}.", width=24).pack(side="left", padx=(6, 0), pady=4)
        tipo = ctk.CTkOptionMenu(fila, values=list(ACTION_LABELS.values()), width=160,
                                 command=lambda v: self._change_type(i, _LABEL_TO_TYPE[v]))
        tipo.set(ACTION_LABELS[action_menu_key(a)])
        tipo.pack(side="left", padx=4)

        def setter(clave: str) -> Callable[[Any], None]:
            def _s(v: Any) -> None:
                a[clave] = v
                self.on_change()
            return _s

        t = a.get("tipo")
        if t in ("mantener", "autoclick"):
            self._point_fields(fila, a, setter)
            # 2.ª línea: tiempos (la fila ya es ancha con zona/plantilla/x/y)
            sub = ctk.CTkFrame(self.list, fg_color="#24402a", corner_radius=6)
            sub.pack(fill="x", pady=(0, 2), padx=(30, 0))
            ctk.CTkLabel(sub, text="Botón " + ("derecho" if a.get("boton") == "right" else "izquierdo"),
                         text_color="gray70").pack(side="left", padx=(8, 12), pady=3)
            if t == "autoclick":
                ctk.CTkLabel(sub, text="clics/seg").pack(side="left", padx=(0, 2))
                _entry(sub, a.get("cps", 10), _float_in(1, 30), setter("cps"), width=52).pack(side="left")
            _range_fields(sub, "  durante (s)" if t == "autoclick" else "mantener (s)",
                          (float(a.get("min", 0)), float(a.get("max", 0))),
                          lambda r: (a.update(min=r[0], max=r[1]), self.on_change())).pack(side="left", padx=4)
            ctk.CTkLabel(sub, text="tiempos aleatorios (entrópicos)",
                         text_color="gray55").pack(side="left", padx=6)
        elif t == "click":
            self._point_fields(fila, a, setter)
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
        elif t == "accion":
            opciones = self.library or [NO_LIBRARY]
            m = ctk.CTkOptionMenu(fila, values=opciones, width=130,
                                  command=lambda v: setter("nombre")("" if v == NO_LIBRARY else v))
            m.set(a.get("nombre") or opciones[0])
            m.pack(side="left", padx=4)
            if not a.get("nombre") and self.library:
                a["nombre"] = self.library[0]
            ctk.CTkLabel(fila, text="(se define en la pestaña Acciones)",
                         text_color="gray60").pack(side="left", padx=4)
        elif t == "codigo":
            ctk.CTkLabel(fila, text=_first_line(a.get("codigo", "")), anchor="w",
                         font=ctk.CTkFont(family="Consolas", size=12)).pack(side="left", padx=6)
            ctk.CTkButton(fila, text="✎ Editar código", width=110, fg_color="#6b4fb0",
                          command=lambda: self._edit_code(i)).pack(side="left", padx=4)
        elif t == "exe":
            _entry(fila, a.get("ruta", ""), str, setter("ruta"), width=220,
                   placeholder="C:/ruta/programa.exe").pack(side="left", padx=4)
            ctk.CTkLabel(fila, text="reservado: aún no se ejecuta",
                         text_color="gray60").pack(side="left", padx=4)

        for texto, cmd in (("✕", lambda: self._remove(i)), ("↓", lambda: self._move(i, 1)),
                           ("↑", lambda: self._move(i, -1))):
            ctk.CTkButton(fila, text=texto, width=28, fg_color="transparent",
                          command=cmd).pack(side="right", padx=1)

    def _point_fields(self, fila: Any, a: dict[str, Any],
                      setter: Callable[[str], Callable[[Any], None]]) -> None:
        """Dónde actuar: [zona][plantilla] x [..] y [..] (común a clic, mantener y autoclick)."""
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
        self.actions.append(copy.deepcopy(ACTION_DEFAULTS["click"]))
        self._render()
        self.on_change()

    def _change_type(self, i: int, tipo: str) -> None:
        self.actions[i] = copy.deepcopy(ACTION_DEFAULTS[tipo])
        self._render()
        self.on_change()

    def _remove(self, i: int) -> None:
        del self.actions[i]
        self._render()
        self.on_change()

    def _move(self, i: int, d: int) -> None:
        j = i + d
        if 0 <= j < len(self.actions):
            acts = self.actions
            acts[i], acts[j] = acts[j], acts[i]
            self._render()
            self.on_change()


# =========================================================================== #
# Tarjetas de regla ("No-Code ligero")
# =========================================================================== #
CARD_OPS = {"(sola)": None, "Y (AND)": "Y", "O (OR)": "O", "NOT (Y NO)": "NOT"}
_OP_LABEL = {v: k for k, v in CARD_OPS.items()}
NO_ACTION = "(ninguna)"


def card_form(cond: Condition) -> tuple[ZoneIs, str | None, ZoneIs | None] | None:
    """Condición -> (C1, operador, C2) si cabe en una tarjeta; ``None`` si es avanzada."""
    if isinstance(cond, ZoneIs):
        return cond, None, None
    if isinstance(cond, (And, Or)) and len(cond.items) == 2:
        a, b = cond.items
        if isinstance(a, ZoneIs) and isinstance(b, ZoneIs):
            return a, "Y" if isinstance(cond, And) else "O", b
        if isinstance(cond, And) and isinstance(a, ZoneIs) and isinstance(b, Not) \
                and isinstance(b.item, ZoneIs):
            return a, "NOT", b.item
    return None


def card_condition(a: ZoneIs, op: str | None, b: ZoneIs | None) -> Condition:
    if op is None or b is None:
        return a
    if op == "Y":
        return And((a, b))
    if op == "O":
        return Or((a, b))
    return And((a, Not(b)))


class RuleCard(tk.Frame):
    """Una regla en una tarjeta compacta, con widgets nativos de Tk/ttk (ligeros):

        ◆ [Nombre        ]                          [✓ Activa] [✎ Detalle] [🗑]
        SI        [Zona ▼][Estado ▼]  [Y (AND) ▼]  [Zona ▼][Estado ▼]
        ENTONCES  [Acción A1 ▼] [</> Insertar código]   Delay mín [1.0] máx [3.5] s

    Los widgets de CustomTkinter se dibujan sobre un canvas propio y son ~10×
    más caros de crear; con decenas de reglas el panel se volvía lento.
    """

    BG = "#232a23"
    FG = "#dce4ee"
    MUTED = "#8b949e"

    def __init__(self, master: Any, editor: FlowEditor, proc: Process, rule: Rule) -> None:
        super().__init__(master, bg=self.BG, highlightthickness=1, highlightbackground="#3d5a3d",
                         padx=8, pady=6)
        _style_card_widgets(self)
        self.editor = editor
        self.proc = proc
        self.rule = rule
        self.zones = editor.zones or ["(sin zonas)"]
        self.states = [BASELINE_LABEL] + editor.templates

        # --- cabecera ---
        cab = self._row()
        self._label(cab, "◆", fg=COLOR_RULE)
        self._entry(cab, rule.name, _parse_name, self._rename, width=26)
        self._button(cab, "🗑", self._delete, bg="#4a2a2a").pack(side="right")
        self._button(cab, "✎ Detalle", lambda: editor.refresh(keep=rule)).pack(side="right", padx=4)
        activa = tk.BooleanVar(value=rule.enabled)
        tk.Checkbutton(cab, text="Activa", variable=activa, bg=self.BG, fg=self.FG,
                       selectcolor="#1f538d", activebackground=self.BG, activeforeground=self.FG,
                       command=lambda: (setattr(rule, "enabled", activa.get()), self._changed())
                       ).pack(side="right", padx=6)
        self._keep = [activa]

        # --- SI ---
        si = self._row()
        self._label(si, "SI", fg="#b48cff", bold=True, width=9)
        forma = card_form(rule.condition)
        if forma is None:
            self._label(si, condition_to_text(rule.condition), wrap=520)
            self._label(si, "(avanzada: edítala en Detalle)", fg="#e0a050")
        else:
            self.a, self.op, self.b = forma
            self._combo(si, self.zones, self.a.zone, lambda v: self._set_leaf("a", zone=v), 12)
            self._combo(si, self.states, self._state_label(self.a),
                        lambda v: self._set_leaf("a", target=self._state_value(v)), 14)
            self._combo(si, list(CARD_OPS), _OP_LABEL[self.op], self._set_op, 11)
            self.b_frame = tk.Frame(si, bg=self.BG)
            self.b_frame.pack(side="left")
            self._render_b()

        # --- ENTONCES ---
        ent = self._row(pady=(2, 0))
        self._label(ent, "ENTONCES", fg=COLOR_RULE, bold=True, width=9)
        self.action_combo = self._combo(ent, [NO_ACTION], NO_ACTION, self._pick_action, 16)
        self.code_btn = self._button(ent, "", self._edit_code, bg="#4b3a7a")
        self.code_btn.pack(side="left", padx=4)
        self._label(ent, "   Delay  mín")
        valores = list(rule.delay)
        self._delay_entries: list[tk.Entry] = []
        for i in (0, 1):
            if i == 1:
                self._label(ent, "máx")
            self._delay_entries.append(
                self._entry(ent, valores[i], _float_in(0, 3600), lambda v, i=i: self._set_delay(i, v), 6))
        self._label(ent, "s")
        self._sync_actions()

    # --------------------------- widgets base --------------------------- #
    def _row(self, pady: tuple[int, int] = (2, 2)) -> tk.Frame:
        f = tk.Frame(self, bg=self.BG)
        f.pack(fill="x", pady=pady)
        return f

    def _label(self, parent: Any, text: str, fg: str | None = None, bold: bool = False,
               width: int | None = None, wrap: int = 0) -> tk.Label:
        lbl = tk.Label(parent, text=text, bg=self.BG, fg=fg or self.MUTED, anchor="w",
                       font=("Segoe UI", 10, "bold" if bold else "normal"), wraplength=wrap,
                       justify="left", **({"width": width} if width else {}))
        lbl.pack(side="left", padx=(0, 4))
        return lbl

    def _button(self, parent: Any, text: str, cmd: Callable[[], None], bg: str = "#3a3a3a") -> tk.Button:
        return tk.Button(parent, text=text, command=cmd, bg=bg, fg=self.FG, relief="flat", bd=0,
                         activebackground="#1f538d", activeforeground="white", padx=8, pady=2,
                         cursor="hand2", font=("Segoe UI", 10))

    def _combo(self, parent: Any, values: list[str], current: str, cmd: Callable[[str], None],
               width: int) -> ttk.Combobox:
        c = ttk.Combobox(parent, values=values, width=width, state="readonly", style="Card.TCombobox")
        c.set(current)
        c.bind("<<ComboboxSelected>>", lambda _e: (c.selection_clear(), cmd(c.get())))
        c.pack(side="left", padx=2)
        return c

    def _entry(self, parent: Any, value: Any, parse: Callable[[str], Any], apply: Callable[[Any], None],
               width: int) -> tk.Entry:
        var = tk.StringVar(value=str(value))
        e = tk.Entry(parent, textvariable=var, width=width, bg="#343638", fg=self.FG,
                     insertbackground=self.FG, relief="flat", highlightthickness=1,
                     highlightbackground="#565b5e", highlightcolor="#1f538d", font=("Segoe UI", 10))

        def _on_change(*_a: object) -> None:
            try:
                v = parse(var.get())
            except (ValueError, TypeError):
                e.configure(highlightbackground=COLOR_ERROR, highlightcolor=COLOR_ERROR)
                return
            e.configure(highlightbackground="#565b5e", highlightcolor="#1f538d")
            apply(v)

        var.trace_add("write", _on_change)
        e._var = var  # type: ignore[attr-defined]
        e.pack(side="left", padx=2, ipady=2)
        return e

    # ---------------------------- condición ---------------------------- #
    @staticmethod
    def _state_label(z: ZoneIs) -> str:
        return BASELINE_LABEL if z.target == BASELINE else z.target

    @staticmethod
    def _state_value(label: str) -> str:
        return BASELINE if label == BASELINE_LABEL else label

    def _render_b(self) -> None:
        for w in self.b_frame.winfo_children():
            w.destroy()
        if self.op is None:
            return
        if self.b is None:
            self.b = ZoneIs(self.zones[-1] if self.editor.zones else self.a.zone,
                            self.editor.templates[0] if self.editor.templates else BASELINE)
        self._combo(self.b_frame, self.zones, self.b.zone, lambda v: self._set_leaf("b", zone=v), 12)
        self._combo(self.b_frame, self.states, self._state_label(self.b),
                    lambda v: self._set_leaf("b", target=self._state_value(v)), 14)

    def _set_leaf(self, which: str, **cambios: str) -> None:
        hoja: ZoneIs = getattr(self, which)
        datos = {"zone": hoja.zone, "target": hoja.target, "threshold": hoja.threshold, **cambios}
        setattr(self, which, ZoneIs(**datos))
        self._commit_condition()

    def _set_op(self, label: str) -> None:
        self.op = CARD_OPS[label]
        self._render_b()
        self._commit_condition()

    def _commit_condition(self) -> None:
        self.rule.condition = card_condition(self.a, self.op, self.b)
        self.rule.reset()
        self._changed()

    def _set_delay(self, i: int, v: float) -> None:
        valores = list(self.rule.delay)
        valores[i] = v
        try:
            otro = float(self._delay_entries[1 - i].get().replace(",", "."))
            valores[1 - i] = otro
        except (ValueError, IndexError):
            pass
        ok = 0 <= valores[0] <= valores[1]
        for e in self._delay_entries:
            e.configure(highlightbackground="#565b5e" if ok else COLOR_ERROR)
        if ok:
            self.rule.delay = (valores[0], valores[1])
            self._changed()

    # ----------------------------- acciones ---------------------------- #
    def _code_action(self) -> dict[str, Any] | None:
        return next((a for a in self.rule.actions if a.get("tipo") == "codigo"), None)

    def _sync_actions(self) -> None:
        library = list(self.editor.flow.actions) if self.editor.flow else []
        otras = [a for a in self.rule.actions if a.get("tipo") != "codigo"]
        if not otras:
            actual = NO_ACTION
        elif len(otras) == 1 and otras[0].get("tipo") == "accion":
            actual = otras[0].get("nombre") or NO_ACTION
        else:
            actual = f"(personalizada: {len(otras)})"
        valores = [NO_ACTION] + library + ([actual] if actual not in library and actual != NO_ACTION else [])
        self.action_combo.configure(values=valores)
        self.action_combo.set(actual)
        codigo = self._code_action()
        self.code_btn.configure(text="</> Código ✔" if codigo and codigo.get("codigo", "").strip()
                                else "</> Insertar código")

    def _pick_action(self, valor: str) -> None:
        if valor.startswith("(personalizada"):
            return
        otras = [a for a in self.rule.actions if a.get("tipo") != "codigo"]
        personalizada = otras and not (len(otras) == 1 and otras[0].get("tipo") == "accion")
        if personalizada and not messagebox.askyesno(
                "Cambiar acción", "La regla tiene acciones personalizadas. ¿Reemplazarlas?", parent=self):
            self._sync_actions()
            return
        codigos = [a for a in self.rule.actions if a.get("tipo") == "codigo"]
        nuevas = [] if valor == NO_ACTION else [{"tipo": "accion", "nombre": valor}]
        self.rule.actions[:] = nuevas + codigos
        self._sync_actions()
        self._changed()

    def _edit_code(self) -> None:
        accion = self._code_action()

        def guardar(codigo: str) -> None:
            actual = self._code_action()
            if not codigo.strip():
                if actual is not None:
                    self.rule.actions.remove(actual)
            elif actual is None:
                self.rule.actions.append({"tipo": "codigo", "codigo": codigo})
            else:
                actual["codigo"] = codigo
            self._sync_actions()
            self._changed()

        CodeEditor(self, accion.get("codigo", "") if accion else "", guardar,
                   title=f"Insertar código — {self.rule.name}")

    # ------------------------------ varios ----------------------------- #
    def _rename(self, nombre: str) -> None:
        self.rule.name = nombre
        self._changed()

    def _changed(self) -> None:
        self.editor.relabel(self.rule)
        self.editor.on_change()

    def _delete(self) -> None:
        if messagebox.askyesno("Eliminar", f"¿Eliminar la regla {self.rule.name!r}?", parent=self):
            self.proc.rules.remove(self.rule)
            self.editor.refresh(keep=self.proc)
            self.editor.on_change()


_CARD_STYLED: set[str] = set()


def _style_card_widgets(widget: tk.Misc) -> None:
    """Estilo oscuro para los ttk.Combobox de las tarjetas (una vez por intérprete Tk)."""
    root = str(widget.winfo_toplevel().tk)
    if root in _CARD_STYLED:
        return
    _CARD_STYLED.add(root)
    st = ttk.Style(widget)
    st.configure("Card.TCombobox", fieldbackground="#1f3b5c", background="#2a4d75",
                 foreground="#dce4ee", arrowcolor="#dce4ee", bordercolor="#2a4d75",
                 lightcolor="#2a4d75", darkcolor="#2a4d75", padding=3)
    st.map("Card.TCombobox",
           fieldbackground=[("readonly", "#1f3b5c")], foreground=[("readonly", "#dce4ee")],
           selectbackground=[("readonly", "#1f3b5c")], selectforeground=[("readonly", "#dce4ee")],
           background=[("active", "#1f538d")])
    top = widget.winfo_toplevel()
    top.option_add("*TCombobox*Listbox.background", "#2b2b2b")
    top.option_add("*TCombobox*Listbox.foreground", "#dce4ee")
    top.option_add("*TCombobox*Listbox.selectBackground", "#1f538d")
    top.option_add("*TCombobox*Listbox.font", ("Segoe UI", 10))


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

    def relabel(self, obj: Any) -> None:
        """Actualiza el texto del árbol para ``obj`` sin reconstruirlo."""
        for iid, (o, _cont) in self._nodes.items():
            if o is obj:
                self.tree.item(iid, text=self._label(obj), tags=self._tags(obj))
                return

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

        r = _section(self.panel, "REGLAS  (vista rápida en tarjetas)", COLOR_RULE)
        r.configure(fg_color="transparent")
        for regla in proc.rules:
            RuleCard(r, self, proc, regla).pack(fill="x", pady=4, padx=2)
        if not proc.rules:
            ctk.CTkLabel(r, text="Sin reglas todavía.", text_color="gray60").pack(anchor="w", padx=4)
        ctk.CTkButton(r, text="+ Regla rápida", width=150, fg_color="#3d8a3d",
                      command=lambda: self._quick_rule(proc)).pack(anchor="w", pady=(6, 4))

    def _quick_rule(self, proc: Process) -> None:
        if self.flow is None:
            return
        zona = self.zones[0] if self.zones else "Zona 1"
        proc.rules.append(Rule(_unique("Regla", {r.name for r in self.flow.iter_rules()}),
                               ZoneIs(zona, BASELINE), delay=(1.0, 3.5)))
        self.refresh(keep=proc)
        self.on_change()

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
        ActionsEditor(a, rule.actions, self.zones, self.templates, self._changed,
                      base_dir=self.base_dir, library=list(self.flow.actions) if self.flow else [],
                      owner=rule.name).pack(fill="x", padx=8, pady=8)
