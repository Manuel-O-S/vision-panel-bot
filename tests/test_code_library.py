"""Pruebas: código personalizado, biblioteca de acciones (A1, A2…) y tarjetas."""
from __future__ import annotations

import threading
import unittest

from core.actions import ActionContext, ActionError, DefaultActionRunner, expand_actions
from core.geometry import Rect
from core.rules import BASELINE, And, Flow, Not, Or, ZoneIs, parse_expression
from core.user_code import UserCodeError, build_namespace, check_syntax, run_user_code


def _ctx(logs=None, stop=None):
    return ActionContext(
        window=Rect(100, 100, 400, 200),
        zones={"Zona 1": Rect(100, 100, 200, 100)},
        observations={},
        stop_event=stop or threading.Event(),
        rule="R",
        log=(logs.append if logs is not None else (lambda _m: None)),
    )


class TestUserCode(unittest.TestCase):
    def _ns(self, ctx, clicks, teclas):
        return build_namespace(ctx, click=lambda x, y, b: clicks.append((x, y, b)),
                               key=teclas.append, write=teclas.append, log=ctx.log)

    def test_api(self):
        logs, clicks, teclas = [], [], []
        ctx = _ctx(logs)
        run_user_code(
            'clic_rel(0.5, 0.5, zona="Zona 1")\n'
            'clic_rel(0, 0)\n'
            'tecla("enter")\n'
            'log(f"ventana {ventana.width}x{ventana.height}, delay {delay_entropico(1, 2)}")\n'
            'assert 1 <= delay_entropico(1, 2) <= 2\n', self._ns(ctx, clicks, teclas))
        self.assertEqual(clicks, [(200, 150, "left"), (100, 100, "left")])
        self.assertEqual(teclas, ["enter"])
        self.assertTrue(logs[0].startswith("ventana 400x200"))

    def test_errores_con_linea_no_rompen(self):
        ns = self._ns(_ctx(), [], [])
        self.assertIsNone(check_syntax("x = 1\n"))
        err = check_syntax("x = 1\nif x\n    pass\n")
        self.assertEqual(err.line, 2)
        with self.assertRaises(UserCodeError) as cm:
            run_user_code("a = 1\nb = a / 0\n", ns)
        self.assertEqual(cm.exception.line, 2)
        self.assertIn("ZeroDivisionError", str(cm.exception))
        with self.assertRaises(UserCodeError):
            run_user_code("raise SystemExit", ns)          # exit() no mata el hilo
        with self.assertRaises(UserCodeError):
            run_user_code("def f(:\n  pass", ns)

    def test_respeta_el_panico(self):
        stop, clicks = threading.Event(), []
        ctx = _ctx(stop=stop)
        stop.set()
        run_user_code("while not debe_parar():\n    pass\nclic(1, 2)\nesperar(5, 6)\n",
                      self._ns(ctx, clicks, []))
        self.assertEqual(clicks, [])  # tras Pánico ya no hace clics

    def test_accion_codigo_en_el_runner(self):
        logs = []
        runner = DefaultActionRunner()
        runner({"tipo": "codigo", "codigo": "log('hola')"}, _ctx(logs))
        self.assertEqual(logs, ["hola"])
        with self.assertRaises(ActionError) as cm:
            runner({"tipo": "codigo", "codigo": "x = (\n"}, _ctx())
        self.assertIn("sintaxis", str(cm.exception))


class TestLibrary(unittest.TestCase):
    def test_expandir(self):
        lib = {"A1": [{"tipo": "tecla", "tecla": "a"}, {"tipo": "accion", "nombre": "A2"}],
               "A2": [{"tipo": "tecla", "tecla": "b"}]}
        pasos = list(expand_actions([{"tipo": "accion", "nombre": "A1"}, {"tipo": "tecla", "tecla": "c"}], lib))
        self.assertEqual([p["tecla"] for p in pasos], ["a", "b", "c"])

    def test_circular_y_faltante(self):
        lib = {"A1": [{"tipo": "accion", "nombre": "A2"}], "A2": [{"tipo": "accion", "nombre": "A1"}]}
        with self.assertRaisesRegex(ActionError, "circular"):
            list(expand_actions([{"tipo": "accion", "nombre": "A1"}], lib))
        with self.assertRaisesRegex(ActionError, "no existe"):
            list(expand_actions([{"tipo": "accion", "nombre": "A9"}], lib))

    def test_flow_biblioteca(self):
        f = Flow("f")
        self.assertEqual(f.next_action_name(), "A1")
        f.actions["A1"] = [{"tipo": "click", "zona": "Zona 1"}]
        f.actions["A2"] = [{"tipo": "accion", "nombre": "A1"}, {"tipo": "accion", "nombre": "A7"}]
        self.assertEqual(f.next_action_name(), "A3")
        self.assertEqual(f.action_usage("A1"), 1)
        self.assertTrue(any("'A7'" in e for e in f.validate()))
        self.assertEqual(Flow.from_dict(f.to_dict()).actions, f.actions)
        self.assertEqual(Flow.from_dict({"nombre": "vieja"}).actions, {})  # tareas antiguas


class TestCards(unittest.TestCase):
    def test_formas(self):
        from gui.block_editor import card_condition, card_form
        a, b = ZoneIs("Zona 1", BASELINE), ZoneIs("Zona 2", "Plantilla B")
        for op, cond in ((None, a), ("Y", And((a, b))), ("O", Or((a, b))), ("NOT", And((a, Not(b))))):
            forma = card_form(cond)
            self.assertEqual(forma, (a, op, None if op is None else b))
            self.assertEqual(card_condition(*forma), cond)
        self.assertIsNone(card_form(parse_expression("(A == B O C == D) Y E == F")))
        self.assertIsNone(card_form(Not(a)))


if __name__ == "__main__":
    unittest.main()
