"""Pruebas de macros (grabar/reproducir) y de renombrar zonas/plantillas."""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from actions.macro_recorder import Macro, MacroError, MacroRecorder, key_name, play_macro
from core.geometry import Rect, RelativeZone
from core.rules import (
    BASELINE, Flow, Loop, Process, Rule, TemplateRef, ZoneIs, condition_to_text, parse_expression,
)


class FakeInput:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a: self.calls.append((name, *a))


def _macro():
    return Macro((1000, 500), [
        {"t": 0.00, "tipo": "tecla", "tecla": "shift", "pulsado": True},
        {"t": 0.01, "tipo": "click", "rx": 0.5, "ry": 0.5, "boton": "left", "pulsado": True},
        {"t": 0.02, "tipo": "click", "rx": 0.5, "ry": 0.5, "boton": "left", "pulsado": False},
        {"t": 0.03, "tipo": "scroll", "rx": 0.1, "ry": 0.2, "dy": -2},
        {"t": 0.04, "tipo": "tecla", "tecla": "shift", "pulsado": False},
    ])


class TestMacros(unittest.TestCase):
    def test_reproduce_en_otra_resolucion(self):
        fake = FakeInput()
        ok = play_macro(_macro(), Rect(100, 50, 800, 400), threading.Event(), input_module=fake)
        self.assertTrue(ok)
        self.assertEqual(fake.calls, [
            ("key_down", "shift"),
            ("mouse_down", 500, 250, "left"),
            ("mouse_up", 500, 250, "left"),
            ("scroll", 180, 130, -2),
            ("key_up", "shift"),
        ])

    def test_panico_suelta_lo_pulsado(self):
        m = Macro((100, 100), [
            {"t": 0.0, "tipo": "tecla", "tecla": "w", "pulsado": True},
            {"t": 0.0, "tipo": "click", "rx": 0.1, "ry": 0.1, "boton": "right", "pulsado": True},
            {"t": 30.0, "tipo": "tecla", "tecla": "w", "pulsado": False},
        ])
        fake, stop = FakeInput(), threading.Event()
        threading.Timer(0.1, stop.set).start()
        t0 = time.monotonic()
        self.assertFalse(play_macro(m, Rect(0, 0, 100, 100), stop, input_module=fake))
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertIn(("key_up", "w"), fake.calls)
        self.assertIn(("mouse_up", 10, 10, "right"), fake.calls)

    def test_velocidad_y_humanizado(self):
        m = Macro((10, 10), [{"t": 0.2, "tipo": "tecla", "tecla": "a", "pulsado": True},
                             {"t": 0.4, "tipo": "tecla", "tecla": "a", "pulsado": False}])
        t0 = time.monotonic()
        play_macro(m, Rect(0, 0, 10, 10), threading.Event(), speed=2.0, input_module=FakeInput())
        self.assertAlmostEqual(time.monotonic() - t0, 0.2, delta=0.06)  # 0.4 s a ×2 (±10 %)

    def test_guardar_cargar_y_errores(self):
        with tempfile.TemporaryDirectory() as d:
            ruta = _macro().save(Path(d) / "sub" / "m.macro")
            self.assertEqual(Macro.load(ruta).to_dict(), _macro().to_dict())
            (Path(d) / "mal.macro").write_text("{", encoding="utf-8")
            with self.assertRaises(MacroError):
                Macro.load(Path(d) / "mal.macro")
        with self.assertRaises(MacroError):
            Macro.from_dict({"version": 1, "ventana": [1, 1], "eventos": [{"t": 0, "tipo": "x"}]})

    def test_nombres_de_teclas(self):
        from pynput.keyboard import Key, KeyCode
        self.assertEqual(key_name(Key.space), "space")
        self.assertEqual(key_name(Key.ctrl_l), "ctrlleft")
        self.assertEqual(key_name(Key.page_down), "pagedown")
        self.assertEqual(key_name(KeyCode(vk=0x41, char="\x01")), "a")   # Ctrl+A
        self.assertEqual(key_name(KeyCode(vk=0x31, char="!")), "1")      # Shift+1
        self.assertEqual(key_name(KeyCode.from_char("-")), "-")
        self.assertIsNone(key_name(Key.media_next))

    def test_grabacion_relativa_e_ignora_inyectados(self):
        from pynput.keyboard import Key, KeyCode
        from pynput.mouse import Button
        rec = MacroRecorder(Rect(100, 100, 200, 100))
        rec._t0 = time.perf_counter()
        rec._on_click(200, 150, Button.left, True)
        rec._on_click(200, 150, Button.left, True, injected=True)   # del bot: se ignora
        rec._on_scroll(150, 120, 0, 3)
        rec._on_press(KeyCode(vk=0x41, char="a"))
        rec._on_release(KeyCode(vk=0x41, char="a"))
        self.assertFalse(rec._on_press(Key.f10))                     # F10 termina y no se graba
        m = rec.stop()
        self.assertEqual([(e["tipo"], e.get("rx"), e.get("ry")) for e in m.events], [
            ("click", 0.5, 0.5), ("scroll", 0.25, 0.2), ("tecla", None, None), ("tecla", None, None)])
        self.assertEqual(m.window_size, (200, 100))


class TestRename(unittest.TestCase):
    def _flow(self):
        regla = Rule("R", parse_expression('Zona 1 == Baseline Y NOT (Zona 2 == "Plantilla B")'),
                     actions=[{"tipo": "click", "zona": "Zona 2", "plantilla": "Plantilla B"},
                              {"tipo": "click", "zona": "Zona 1"}])
        return Flow("f",
                    zones=[RelativeZone("Zona 1", 0, 0, .5, .5), RelativeZone("Zona 2", .5, .5, .5, .5)],
                    templates={"Plantilla A": TemplateRef("a.png"), "Plantilla B": TemplateRef("b.png"),
                               "Plantilla C": TemplateRef("c.png")},
                    loops=[Loop("L", [Process("P", [regla])])])

    def test_renombrar_zona_actualiza_reglas(self):
        f = self._flow()
        self.assertEqual(f.rename_zone("Zona 2", "Botón OK"), "Botón OK")
        r = next(f.iter_rules())
        self.assertEqual(condition_to_text(r.condition),
                         'Zona 1 == Baseline Y Botón OK != Plantilla B')
        self.assertEqual(r.actions[0]["zona"], "Botón OK")
        self.assertEqual(r.actions[1]["zona"], "Zona 1")
        self.assertEqual([z.name for z in f.zones], ["Zona 1", "Botón OK"])
        self.assertEqual(f.validate(), [])

    def test_renombrar_plantilla_conserva_orden(self):
        f = self._flow()
        f.rename_template("Plantilla B", "Vida baja")
        self.assertEqual(list(f.templates), ["Plantilla A", "Vida baja", "Plantilla C"])
        r = next(f.iter_rules())
        self.assertIn(("Zona 2", "Vida baja"), set(r.condition.requirements()))
        self.assertEqual(r.actions[0]["plantilla"], "Vida baja")
        self.assertEqual(f.validate(), [])

    def test_errores(self):
        f = self._flow()
        for viejo, nuevo in (("Zona 1", "Zona 2"), ("Zona 1", "  "), ("Zona 1", "Zona -1"), ("Nope", "x")):
            with self.assertRaises(ValueError, msg=(viejo, nuevo)):
                f.rename_zone(viejo, nuevo)
        with self.assertRaises(ValueError):
            f.rename_template("Plantilla A", "Plantilla C")
        self.assertEqual(f.rename_zone("Zona 1", "Zona 1"), "Zona 1")  # sin cambios
        self.assertEqual(ZoneIs("Zona 1", "baseline").target, BASELINE)


if __name__ == "__main__":
    unittest.main()
