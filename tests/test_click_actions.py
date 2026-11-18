"""Mantener clic y autoclick, con un ratón falso (no se toca el ratón real)."""
import threading
import time
import unittest

from core.actions import ActionContext, ActionError, DefaultActionRunner
from core.geometry import Rect


class FakeMouse:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, float]] = []
        self.pos = None

    def move(self, x, y, jitter_px=0.0):
        self.pos = (x, y)

    def human_delay(self, *a, **k):
        pass

    def button_down(self, button="left"):
        self.events.append(("down", button, time.perf_counter()))

    def button_up(self, button="left"):
        self.events.append(("up", button, time.perf_counter()))


class FakeRunner(DefaultActionRunner):
    def __init__(self, mouse: FakeMouse) -> None:
        super().__init__()
        self._mouse = mouse

    def _input(self):  # noqa: ANN202
        return self._mouse


def ctx(stop: threading.Event | None = None, logs: list | None = None) -> ActionContext:
    win = Rect(100, 100, 800, 600)
    return ActionContext(window=win, zones={"Zona 1": Rect(100, 100, 200, 100)}, observations={},
                         stop_event=stop or threading.Event(), rule="R",
                         log=(logs.append if logs is not None else (lambda _m: None)))


class TestMantener(unittest.TestCase):
    def test_mantiene_y_suelta(self):
        m = FakeMouse()
        t0 = time.perf_counter()
        FakeRunner(m)({"tipo": "mantener", "zona": "Zona 1", "boton": "right", "min": 0.1, "max": 0.15}, ctx())
        self.assertEqual([e[:2] for e in m.events], [("down", "right"), ("up", "right")])
        self.assertGreaterEqual(m.events[1][2] - m.events[0][2], 0.09)
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertEqual(m.pos, (200, 150))  # centro de Zona 1

    def test_panico_suelta_el_boton(self):
        m, stop = FakeMouse(), threading.Event()
        threading.Timer(0.05, stop.set).start()
        t0 = time.perf_counter()
        FakeRunner(m)({"tipo": "mantener", "min": 5, "max": 6}, ctx(stop))
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual([e[0] for e in m.events], ["down", "up"])


class TestAutoclick(unittest.TestCase):
    def test_ritmo_aprox_10cps(self):
        m, logs = FakeMouse(), []
        FakeRunner(m)({"tipo": "autoclick", "boton": "left", "cps": 10, "min": 1.0, "max": 1.0}, ctx(logs=logs))
        downs = [e for e in m.events if e[0] == "down"]
        ups = [e for e in m.events if e[0] == "up"]
        self.assertEqual(len(downs), len(ups))
        self.assertTrue(8 <= len(downs) <= 12, len(downs))
        intervalos = [b[2] - a[2] for a, b in zip(downs, downs[1:])]
        self.assertGreater(len(set(round(i, 3) for i in intervalos)), 3, "los intervalos deberían variar")
        self.assertIn("clics", logs[-1])

    def test_derecho_y_parada(self):
        m, stop = FakeMouse(), threading.Event()
        threading.Timer(0.2, stop.set).start()
        t0 = time.perf_counter()
        FakeRunner(m)({"tipo": "autoclick", "boton": "right", "cps": 10, "min": 5, "max": 5}, ctx(stop))
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertTrue(all(e[1] == "right" for e in m.events))
        self.assertEqual(m.events[-1][0], "up")

    def test_validaciones(self):
        r = FakeRunner(FakeMouse())
        with self.assertRaises(ActionError):
            r({"tipo": "autoclick", "cps": 0}, ctx())
        with self.assertRaises(ActionError):
            r({"tipo": "autoclick", "cps": 100}, ctx())
        with self.assertRaises(ActionError):
            r({"tipo": "mantener", "min": 2, "max": 1}, ctx())
        with self.assertRaises(ActionError):
            r({"tipo": "mantener", "boton": "lateral"}, ctx())


class TestPlantillaSinZona(unittest.TestCase):
    def test_usa_la_zona_con_mejor_coincidencia(self):
        from core.rules import ZoneObservation
        obs = {"Zona 1": ZoneObservation("Zona 1", scores={"P": 0.30}, locations={"P": (0, 0, 10, 10)}),
               "Zona 2": ZoneObservation("Zona 2", scores={"P": 0.95}, locations={"P": (500, 400, 20, 10)})}
        c = ActionContext(window=Rect(0, 0, 800, 600), zones={}, observations=obs,
                          stop_event=threading.Event(), rule="R")
        m = FakeMouse()
        FakeRunner(m)({"tipo": "mantener", "plantilla": "P", "min": 0, "max": 0}, c)
        self.assertEqual(m.pos, (510, 405))

    def test_error_claro_si_no_se_ve(self):
        with self.assertRaises(ActionError):
            FakeRunner(FakeMouse())({"tipo": "autoclick", "plantilla": "P"}, ctx())


class TestMenuEditor(unittest.TestCase):
    def test_claves_del_menu(self):
        from gui.block_editor import ACTION_DEFAULTS, ACTION_LABELS, action_menu_key
        for clave, accion in ACTION_DEFAULTS.items():
            self.assertEqual(action_menu_key(accion), clave)
            self.assertIn(clave, ACTION_LABELS)


if __name__ == "__main__":
    unittest.main()


class TestAvisosFlujo(unittest.TestCase):
    def test_condicion_repetida_y_plantilla_grande(self):
        from core.geometry import RelativeZone
        from core.rules import Flow, Rule, TemplateRef, ZoneIs, flow_warnings
        f = Flow.from_dict({"nombre": "t", "loops": [{"nombre": "L", "procesos": [{"nombre": "P", "reglas": []}]}]})
        f.zones += [RelativeZone("Zona 1", 0, 0, 0.1, 0.1), RelativeZone("Zona 2", 0.5, 0.5, 0.4, 0.4)]
        f.templates.update({"Grande": TemplateRef("g.png", ref_size=(1000, 1000)),
                            "Chica": TemplateRef("c.png", ref_size=(1000, 1000))})
        reglas = f.loops[0].processes[0].rules
        reglas += [Rule("A", ZoneIs("Zona 1", "Grande")), Rule("B", ZoneIs("Zona 1", "Grande")),
                   Rule("C", ZoneIs("Zona 2", "Chica"))]
        info = {"Grande": ((300, 50), (1000, 1000)), "Chica": ((50, 50), (1000, 1000))}.get
        avisos = flow_warnings(f, info)
        self.assertEqual(len(avisos), 2, avisos)
        self.assertIn("misma condición", avisos[0])
        self.assertIn("'Grande'", avisos[1])
        reglas[1].condition = ZoneIs("Zona 2", "Chica")
        reglas[2].enabled = False
        reglas[0].condition = ZoneIs("Zona 2", "Chica")
        reglas[1].condition = ZoneIs("Zona 1", "baseline")
        self.assertEqual(flow_warnings(f, info), [])


class TestAvisoVentana(unittest.TestCase):
    def test_recortes_con_otra_ventana(self):
        from core.rules import Flow, TemplateRef, flow_warnings
        f = Flow.from_dict({"nombre": "t"})
        f.templates["P"] = TemplateRef("p.png", ref_size=(1360, 768))
        info = {"P": ((10, 10), (1360, 768))}.get
        self.assertEqual(flow_warnings(f, info, current_size=(1360, 768)), [])
        avisos = flow_warnings(f, info, current_size=(1360, 697))
        self.assertEqual(len(avisos), 1)
        self.assertIn("1360×697", avisos[0])


class TestResumenPuntuaciones(unittest.TestCase):
    def test_resumen_al_terminar(self):
        from core.engine import Engine, EngineConfig, EventType
        e = Engine(EngineConfig(match_threshold=0.8))
        e._best_scores = {("Zona 2", "P"): 0.0, ("Zona 1", "Q"): 0.91}
        e._emit_best_scores()
        logs = [ev.payload for ev in e.poll_events(10) if ev.type is EventType.LOG]
        self.assertEqual(len(logs), 2)
        self.assertIn("0.910", logs[0])
        self.assertIn("✔", logs[0])
        self.assertIn("plano", logs[1])


class TestDondeEsteElRaton(unittest.TestCase):
    def test_no_mueve_el_puntero(self):
        for tipo in ("mantener", "autoclick"):
            m = FakeMouse()
            FakeRunner(m)({"tipo": tipo, "raton": True, "min": 0.05, "max": 0.05}, ctx())
            self.assertIsNone(m.pos, tipo)
            self.assertTrue(m.events and m.events[-1][0] == "up")

    def test_editor_opcion_raton(self):
        import copy
        import customtkinter as ctk
        from gui.block_editor import ACTION_DEFAULTS, MOUSE_LABEL, ActionsEditor
        root = ctk.CTk()
        root.withdraw()
        try:
            acts = [copy.deepcopy(ACTION_DEFAULTS["mantener"])]
            ed = ActionsEditor(root, acts, ["Zona 1"], ["P"], lambda: None)
            fila = ed.list.winfo_children()[0]
            menu = [w for w in fila.winfo_children() if isinstance(w, ctk.CTkOptionMenu)][1]
            menu._command(MOUSE_LABEL)
            self.assertTrue(acts[0]["raton"])
            self.assertIsNone(acts[0]["zona"])
        finally:
            root.destroy()


class TestReintentar(unittest.TestCase):
    """Flanco con 'reintentar': si la acción no cambió nada, se vuelve a disparar."""

    class Cond:
        def __init__(self) -> None:
            self.valor = True

        def evaluate(self, obs, thr):
            return self.valor

        def to_dict(self):
            return {"zona": "Zona 1", "es": "baseline"}

    def test_reintenta_si_sigue_verdadera(self):
        from core.rules import Rule
        c = self.Cond()
        r = Rule("Lanzar", c, retry=20)
        self.assertTrue(r.check({}, 0.9, now=0))
        self.assertFalse(r.is_retry)
        r.mark_done(now=8)                       # las acciones terminaron en t=8
        self.assertFalse(r.check({}, 0.9, now=20))  # sólo 12 s desde el final
        self.assertTrue(r.check({}, 0.9, now=28.5))  # 20 s sin cambios: reintento
        self.assertTrue(r.is_retry)
        r.mark_done(now=30)
        c.valor = False                           # funcionó: la condición cambió
        self.assertFalse(r.check({}, 0.9, now=60))
        c.valor = True                            # vuelve a ser verdadera: flanco normal
        self.assertTrue(r.check({}, 0.9, now=61))
        self.assertFalse(r.is_retry)

    def test_sin_reintento_por_defecto_y_guardado(self):
        from core.rules import Rule
        r = Rule("Lanzar", self.Cond())
        self.assertTrue(r.check({}, 0.9, now=0))
        self.assertFalse(r.check({}, 0.9, now=10_000))
        self.assertNotIn("reintentar", r.to_dict())
        r.retry = 25
        self.assertEqual(r.to_dict()["reintentar"], 25)

    def test_carga_desde_tarea(self):
        from core.rules import Rule
        r = Rule.from_dict({"nombre": "L", "condicion": {"zona": "Zona 1", "es": "baseline"},
                            "reintentar": 30})
        self.assertEqual(r.retry, 30.0)
        self.assertEqual(Rule.from_dict({"nombre": "L", "condicion": {"zona": "Zona 1", "es": "baseline"}}).retry, 0.0)

    def test_registro_indica_donde(self):
        m, logs = FakeMouse(), []
        FakeRunner(m)({"tipo": "mantener", "raton": True, "min": 0.01, "max": 0.01}, ctx(logs=logs))
        self.assertIn("donde está el ratón", logs[-1])


class TestContador(unittest.TestCase):
    def test_contadores_y_resumen(self):
        from core.engine import RunStats, fmt_elapsed
        s = RunStats(start=time.monotonic() - 125)
        self.assertEqual(s.fired("Lanzar"), 1)
        self.assertEqual(s.fired("Pescar"), 1)
        self.assertEqual(s.fired("Lanzar", retry=True), 2)
        s.errors = 1
        self.assertEqual(s.total, 3)
        txt = s.summary()
        self.assertIn("00:02:05", txt)
        self.assertIn("Lanzar ×2", txt)
        self.assertIn("1 reintentos", txt)
        self.assertIn("Pescar ×1", txt)
        self.assertIn("errores 1", txt)
        self.assertEqual(fmt_elapsed(3725), "01:02:05")

    def test_motor_cuenta_errores(self):
        from core.engine import Engine, EngineConfig, EventType
        e = Engine(EngineConfig())
        e._emit(EventType.ERROR, "x")
        e._emit(EventType.WINDOW_LOST, "Roblox")
        self.assertEqual((e.stats.errors, e.stats.window_lost), (1, 1))
