"""
code_editor.py - Mini-IDE flotante para "Insertar Código" en una acción.

- Fuente monoespaciada, números de línea, Tab = 4 espacios, Ctrl+S guarda.
- Resaltado ligero (palabras clave, API del bot, cadenas, comentarios) con
  retardo de 150 ms para no penalizar la escritura.
- "Verificar" compila sin ejecutar y marca la línea con el error.
- El código NO se ejecuta aquí: se guarda en la acción y el motor lo ejecuta
  en su propio hilo dentro de un try-except (core/user_code.py).
"""
from __future__ import annotations

import keyword
import re
import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox
from typing import Any

import customtkinter as ctk

from core.user_code import SNIPPETS, check_syntax

__all__ = ["CodeEditor"]

FONT = ("Consolas", 13)
API_NAMES = ("ventana", "zonas", "obs", "regla", "clic", "clic_rel", "tecla", "escribir",
             "esperar", "debe_parar", "delay_entropico", "log", "math", "time")
_RE_KW = re.compile(r"\b(" + "|".join(keyword.kwlist) + r")\b")
_RE_API = re.compile(r"\b(" + "|".join(API_NAMES) + r")\b")
_RE_STR = re.compile(r"(\"[^\"\n]*\"|'[^'\n]*')")
_RE_NUM = re.compile(r"\b\d+(\.\d+)?\b")
_RE_COMMENT = re.compile(r"#[^\n]*")

AYUDA = ("API: clic(x, y) · clic_rel(rx, ry, zona=\"Zona 1\") · tecla(\"enter\") · escribir(\"hola\") · "
         "esperar(min, max) · debe_parar() · log(\"msg\") · obs · zonas · ventana\n"
         "Se ejecuta en el hilo del motor dentro de try-except: un error se muestra en Registro "
         "sin detener el bot. No es un sandbox: tiene los permisos de la app.")


class CodeEditor(ctk.CTkToplevel):
    def __init__(self, master: Any, code: str, on_save: Callable[[str], None],
                 title: str = "Insertar código") -> None:
        super().__init__(master)
        self.on_save = on_save
        self.title(title)
        self.geometry("860x560")
        self.minsize(560, 360)
        self.transient(master.winfo_toplevel())
        self._hl_job: str | None = None

        barra = ctk.CTkFrame(self, fg_color="transparent")
        barra.pack(fill="x", padx=10, pady=(10, 4))
        ctk.CTkButton(barra, text="✔ Verificar", width=100, command=self._verify).pack(side="left")
        ejemplos = ctk.CTkOptionMenu(barra, values=list(SNIPPETS), width=230,
                                     command=self._insert_snippet)
        ejemplos.set("Insertar ejemplo…")
        ejemplos.pack(side="left", padx=8)
        ctk.CTkButton(barra, text="Cancelar", width=90, fg_color="#555555",
                      command=self.destroy).pack(side="right")
        ctk.CTkButton(barra, text="💾 Guardar (Ctrl+S)", width=150, fg_color="#2f7a3e",
                      command=self._save).pack(side="right", padx=8)

        cuerpo = tk.Frame(self, bg="#1d1e1e")
        cuerpo.pack(fill="both", expand=True, padx=10, pady=4)
        self.lines = tk.Text(cuerpo, width=4, font=FONT, bg="#252526", fg="#6e7681", bd=0,
                             padx=6, state="disabled", takefocus=0, cursor="arrow")
        self.lines.pack(side="left", fill="y")
        scroll = ctk.CTkScrollbar(cuerpo, command=self._yview)
        scroll.pack(side="right", fill="y")
        self.text = tk.Text(cuerpo, font=FONT, bg="#1d1e1e", fg="#dce4ee", insertbackground="white",
                            bd=0, padx=8, pady=4, undo=True, wrap="none", tabs=("1c",),
                            selectbackground="#264f78", yscrollcommand=lambda *a: self._on_scroll(scroll, *a))
        self.text.pack(side="left", fill="both", expand=True)
        for tag, color in (("kw", "#c586c0"), ("api", "#4fc1ff"), ("str", "#ce9178"),
                           ("num", "#b5cea8"), ("comment", "#6a9955")):
            self.text.tag_configure(tag, foreground=color)
        self.text.tag_configure("error", background="#5a1d1d")

        self.status = ctk.CTkLabel(self, text="", anchor="w")
        self.status.pack(fill="x", padx=12)
        ctk.CTkLabel(self, text=AYUDA, text_color="gray60", justify="left", anchor="w",
                     wraplength=820).pack(fill="x", padx=12, pady=(0, 10))

        self.text.insert("1.0", code)
        self.text.edit_reset()
        self.text.bind("<Tab>", self._on_tab)
        self.text.bind("<KeyRelease>", lambda _e: self._schedule_highlight())
        self.bind("<Control-s>", lambda _e: self._save())
        self._refresh()
        self.after(100, self._focus)

    # ------------------------------ UI ------------------------------- #
    def _focus(self) -> None:
        self.lift()
        self.text.focus_set()
        try:
            self.grab_set()  # modal: evita editar la regla mientras tanto
        except tk.TclError:
            pass

    def _yview(self, *args: Any) -> None:
        self.text.yview(*args)
        self.lines.yview(*args)

    def _on_scroll(self, scroll: ctk.CTkScrollbar, first: str, last: str) -> None:
        scroll.set(first, last)
        self.lines.yview_moveto(first)

    def _on_tab(self, _e: tk.Event) -> str:
        self.text.insert("insert", "    ")
        return "break"

    def _insert_snippet(self, nombre: str) -> None:
        self.text.insert("insert", SNIPPETS[nombre])
        self._refresh()

    def _schedule_highlight(self) -> None:
        if self._hl_job is not None:
            self.after_cancel(self._hl_job)
        self._hl_job = self.after(150, self._refresh)

    def _refresh(self) -> None:
        self._hl_job = None
        n = int(self.text.index("end-1c").split(".")[0])
        self.lines.configure(state="normal")
        self.lines.delete("1.0", "end")
        self.lines.insert("1.0", "\n".join(str(i) for i in range(1, n + 1)))
        self.lines.configure(state="disabled")
        self.lines.yview_moveto(self.text.yview()[0])

        codigo = self.text.get("1.0", "end-1c")
        for tag in ("kw", "api", "str", "num", "comment", "error"):
            self.text.tag_remove(tag, "1.0", "end")
        for tag, regex in (("kw", _RE_KW), ("api", _RE_API), ("num", _RE_NUM),
                           ("str", _RE_STR), ("comment", _RE_COMMENT)):
            for m in regex.finditer(codigo):
                self.text.tag_add(tag, f"1.0+{m.start()}c", f"1.0+{m.end()}c")

    # --------------------------- acciones ----------------------------- #
    def _verify(self) -> bool:
        self._refresh()
        err = check_syntax(self.text.get("1.0", "end-1c"))
        if err is None:
            self.status.configure(text="✔ Sintaxis correcta", text_color="#59C059")
            return True
        if err.line:
            self.text.tag_add("error", f"{err.line}.0", f"{err.line}.end+1c")
            self.text.see(f"{err.line}.0")
        self.status.configure(text=f"✖ {err}", text_color="#e05555")
        return False

    def _save(self) -> None:
        if not self._verify() and not messagebox.askyesno(
                "Código con errores", "El código tiene un error de sintaxis y fallará al ejecutarse.\n"
                "¿Guardarlo igualmente?", parent=self):
            return
        self.on_save(self.text.get("1.0", "end-1c").rstrip() + "\n")
        self.destroy()
