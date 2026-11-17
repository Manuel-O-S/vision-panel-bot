"""Pruebas de /workflows: persistencia de tareas y hotkeys globales."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from core.engine import EngineConfig
from core.geometry import RelativeZone
from core.rules import Rule, TemplateRef, parse_expression
from core.vision import PreprocessMode, read_image
from workflows.task_manager import (
    Task, TaskError, TaskManager, TaskSettings, safe_filename, save_template_image,
)


class TestTaskManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.tm = TaskManager(self.dir / "tasks")

    def tearDown(self):
        self.tmp.cleanup()

    def _task(self) -> Task:
        t = Task.new("Mi tarea")
        t.settings = TaskSettings(window_title="Bloc de notas", match_threshold=0.8,
                                  delta_threshold=0.07, preprocess="canny", max_fps=20)
        t.flow.zones.append(RelativeZone("Zona 1", 0.1, 0.1, 0.2, 0.2, tolerance=0.04))
        t.flow.templates["Plantilla B"] = TemplateRef("assets/templates/B.png", (1920, 1080))
        t.flow.loops[0].processes[0].rules.append(Rule(
            "A3", parse_expression('Zona 1 == Baseline Y NOT Zona 1 == "Plantilla B"'),
            actions=[{"tipo": "click", "zona": "Zona 1", "x": 0.5, "y": 0.5}], delay=(0.1, 0.3)))
        return t

    def test_guardar_y_cargar(self):
        t = self._task()
        for ext in (".tarea", ".json"):
            ruta = self.tm.save(t, self.dir / f"x{ext}")
            cargada = self.tm.load(ruta)
            self.assertEqual(cargada.to_dict()["flujo"], t.to_dict()["flujo"])
            self.assertEqual(cargada.settings, t.settings)
        self.assertEqual(len(self.tm.list_tasks()), 0)  # se guardaron fuera de tasks/
        self.tm.save(t)
        self.assertEqual([p.name for p in self.tm.list_tasks()], ["Mi_tarea.tarea"])
        self.assertEqual(list(self.dir.glob("**/*.tmp")), [])  # sin temporales huérfanos

    def test_ajustes_al_motor(self):
        cfg = self._task().settings.apply_to(EngineConfig())
        self.assertEqual(cfg.window_title, "Bloc de notas")
        self.assertEqual(cfg.preprocess.mode, PreprocessMode.CANNY)
        self.assertEqual(cfg.max_fps, 20)

    def test_errores_legibles(self):
        malos = {
            "roto.tarea": "{ no es json",
            "lista.tarea": "[1, 2]",
            "vieja.tarea": json.dumps({"schema_version": 1, "name": "x"}),
            "incompleta.tarea": json.dumps({"schema_version": 2, "flujo": {"zonas": [{"nombre": "Z"}]}}),
            "umbral.tarea": json.dumps({"schema_version": 2, "flujo": {}, "ajustes": {"umbral_similitud": 7}}),
        }
        for nombre, contenido in malos.items():
            p = self.dir / nombre
            p.write_text(contenido, encoding="utf-8")
            with self.assertRaises(TaskError, msg=nombre):
                self.tm.load(p)
        with self.assertRaises(TaskError):
            self.tm.save(self._task(), self.dir / "x.txt")

    def test_copia_independiente(self):
        t = self._task()
        copia = t.copy_flow()
        copia.loops[0].processes[0].rules[0].name = "otra"
        self.assertEqual(t.flow.loops[0].processes[0].rules[0].name, "A3")

    def test_plantilla_con_tildes(self):
        img = np.random.default_rng(0).integers(0, 255, (20, 30, 3), dtype=np.uint8)
        carpeta = self.dir / "plantíllas ñ"
        rel = save_template_image(img, 'Botón: "Sí"', carpeta, self.dir)
        self.assertEqual(rel, "plantíllas ñ/Botón_Sí.png")
        np.testing.assert_array_equal(read_image(self.dir / rel), img)
        self.assertEqual(safe_filename("  ..  "), "sin_nombre")


def _listener_sin_arrancar():
    """Listener real (con su canonical()) pero sin instalar el hook de teclado."""
    from pynput import keyboard
    return keyboard.Listener()


class TestHotkeys(unittest.TestCase):
    def _manager(self, **kw):
        from workflows.hotkeys import HotkeyManager
        self.calls = []
        m = HotkeyManager(lambda: self.calls.append("toggle"), lambda: self.calls.append("panic"), **kw)
        m._listener = _listener_sin_arrancar()
        return m

    def _tap(self, m, keys, injected=False):
        for k in keys:
            m._press(k, injected)
        for k in reversed(keys):
            m._release(k, injected)

    def test_combinaciones(self):
        from pynput.keyboard import Key, KeyCode
        m = self._manager()
        self._tap(m, [Key.ctrl, Key.shift, Key.alt, KeyCode.from_char("p")])
        self._tap(m, [Key.ctrl, Key.alt, KeyCode.from_char("k")])
        self._tap(m, [Key.esc])
        self.assertEqual(self.calls, ["toggle", "panic", "panic"])

    def test_ignora_inyectadas_y_esc_desactivable(self):
        from pynput.keyboard import Key
        m = self._manager(esc_panic=False)
        self._tap(m, [Key.esc])
        m.esc_panic = True
        self._tap(m, [Key.esc], injected=True)  # Esc pulsado por el propio bot
        self.assertEqual(self.calls, [])
        self._tap(m, [Key.esc])
        self.assertEqual(self.calls, ["panic"])

    def test_callback_que_falla_no_rompe(self):
        from pynput.keyboard import Key
        from workflows.hotkeys import HotkeyManager
        m = HotkeyManager(lambda: None, lambda: 1 / 0)
        m._listener = _listener_sin_arrancar()
        self._tap(m, [Key.esc])  # no debe lanzar

    def test_describe(self):
        from workflows.hotkeys import DEFAULT_TOGGLE, describe
        self.assertEqual(describe(DEFAULT_TOGGLE), "Ctrl + Shift + Alt + P")


if __name__ == "__main__":
    unittest.main()
