"""
Pruebas del núcleo (/core). Sólo stdlib + numpy + OpenCV:

    python -m unittest discover -s tests -v

El motor se prueba con una pantalla simulada (sin capturar ni mover el ratón).
"""
from __future__ import annotations

import statistics
import threading
import time
import unittest

import cv2
import numpy as np

from core import engine as engine_mod
from core.actions import DryRunActionRunner
from core.entropy import dormir_entropico, generar_delay_entropico
from core.geometry import Rect, RelativeZone
from core.rules import (
    And, Flow, Loop, Not, Or, Process, Rule, TemplateRef, Trigger, ZoneIs,
    ZoneObservation, parse_condition, parse_expression,
)
from core.vision import (
    PreprocessConfig, PreprocessMode, TemplateLibrary, baseline_delta,
    baseline_signature, match_template, preprocess,
)


# --------------------------------------------------------------------------- #
class TestEntropy(unittest.TestCase):
    def test_rango_y_4_decimales(self):
        for _ in range(5000):
            v = generar_delay_entropico(0.2, 0.5)
            self.assertGreaterEqual(v, 0.2)
            self.assertLessEqual(v, 0.5)
            self.assertEqual(v, round(v, 4))

    def test_distribucion_centrada_y_variada(self):
        muestras = [generar_delay_entropico(1.0, 2.0) for _ in range(20000)]
        self.assertAlmostEqual(statistics.fmean(muestras), 1.5, delta=0.01)
        # σ esperada = 1/6 (truncada a ±3σ -> ligeramente menor)
        self.assertAlmostEqual(statistics.pstdev(muestras), 1 / 6, delta=0.01)
        self.assertGreater(len(set(muestras)), 5000)  # sin repeticiones predecibles
        # Sin picos en los bordes (lo que delataría un clamp)
        bordes = sum(1 for m in muestras if m in (1.0, 2.0))
        self.assertLess(bordes, 20)

    def test_rango_degenerado_y_errores(self):
        self.assertEqual(generar_delay_entropico(0.3, 0.3), 0.3)
        self.assertEqual(generar_delay_entropico(0.0, 0.0), 0.0)
        with self.assertRaises(ValueError):
            generar_delay_entropico(0.5, 0.2)
        with self.assertRaises(ValueError):
            generar_delay_entropico(-1, 1)

    def test_dormir_interrumpible(self):
        ev = threading.Event()
        ev.set()
        t0 = time.monotonic()
        self.assertFalse(dormir_entropico(5, 6, ev))
        self.assertLess(time.monotonic() - t0, 0.1)


# --------------------------------------------------------------------------- #
class TestGeometry(unittest.TestCase):
    def test_independencia_de_resolucion(self):
        z = RelativeZone("Zona 1", 0.25, 0.5, 0.5, 0.25)
        self.assertEqual(z.to_rect(Rect(0, 0, 1920, 1080)), Rect(480, 540, 960, 270))
        self.assertEqual(z.to_rect(Rect(100, 50, 1280, 720)), Rect(420, 410, 640, 180))

    def test_ida_y_vuelta(self):
        win = Rect(10, 20, 1600, 900)
        z = RelativeZone.from_rect("Zona 2", Rect(410, 320, 160, 90), win)
        self.assertEqual(z.to_rect(win), Rect(410, 320, 160, 90))
        # El JSON guarda 6 decimales: precisión sub-píxel incluso en 8K
        self.assertEqual(RelativeZone.from_dict(z.to_dict()).to_rect(win), z.to_rect(win))

    def test_validacion(self):
        with self.assertRaises(ValueError):
            RelativeZone("Z", 0.8, 0.0, 0.5, 0.1)  # se sale por la derecha


# --------------------------------------------------------------------------- #
def _obs(**zonas):
    return {n.replace("_", " "): o for n, o in zonas.items()}


class TestRules(unittest.TestCase):
    def setUp(self):
        self.obs = _obs(
            Zona_1=ZoneObservation("Zona 1", 0.01, True),
            Zona_2=ZoneObservation("Zona 2", scores={"Plantilla B": 0.93}),
        )

    def test_operadores(self):
        z1 = ZoneIs("Zona 1", "Baseline")
        z2 = ZoneIs("Zona 2", "Plantilla B")
        self.assertTrue(And((z1, z2)).evaluate(self.obs, 0.85))
        self.assertFalse(And((z1, Not(z2))).evaluate(self.obs, 0.85))
        self.assertTrue(Or((Not(z1), z2)).evaluate(self.obs, 0.85))
        self.assertFalse(ZoneIs("Zona 2", "Plantilla B", 0.95).evaluate(self.obs, 0.85))
        self.assertFalse(ZoneIs("Zona 9", "Baseline").evaluate(self.obs, 0.85))

    def test_alias_zona_menos_1(self):
        self.assertEqual(ZoneIs("Zona 1", "Zona -1").target, "baseline")

    def test_parser_texto(self):
        c = parse_expression('SI (Zona 1 == Baseline) Y (Zona 2 == "Plantilla B") ==> Acción A3')
        self.assertEqual(c, And((ZoneIs("Zona 1", "baseline"), ZoneIs("Zona 2", "Plantilla B"))))
        self.assertTrue(c.evaluate(self.obs, 0.85))
        # Precedencia NOT > Y > O
        c2 = parse_expression("NOT Zona 1 == Baseline O Zona 2 == Plantilla B Y Zona 1 != Baseline")
        self.assertIsInstance(c2, Or)
        self.assertFalse(c2.evaluate(self.obs, 0.85))
        with self.assertRaises(ValueError):
            parse_expression("(Zona 1 == Baseline")

    def test_json_ida_y_vuelta(self):
        c = parse_expression('NOT (Zona 1 == Baseline O Zona 2 == "Plantilla B")')
        self.assertEqual(parse_condition(c.to_dict()), c)

    def test_flanco_evita_bucle(self):
        r = Rule("A3", ZoneIs("Zona 1", "baseline"))
        disparos = [r.check(self.obs, 0.85) for _ in range(5)]
        self.assertEqual(disparos, [True, False, False, False, False])
        apagada = _obs(Zona_1=ZoneObservation("Zona 1", 0.3, False))
        self.assertFalse(r.check(apagada, 0.85))
        self.assertTrue(r.check(self.obs, 0.85))  # nuevo flanco de subida

    def test_nivel(self):
        r = Rule("A", ZoneIs("Zona 1", "baseline"), trigger=Trigger.LEVEL)
        self.assertEqual([r.check(self.obs, 0.85) for _ in range(3)], [True] * 3)

    def test_flow_validacion_y_serializacion(self):
        flow = _flow_demo()
        self.assertEqual(flow.validate(), [])
        copia = Flow.from_dict(flow.to_dict())
        self.assertEqual(copia.to_dict(), flow.to_dict())
        flow.templates.clear()
        self.assertTrue(any("Plantilla B" in e for e in flow.validate()))


# --------------------------------------------------------------------------- #
def _textura(h, w, seed):
    rng = np.random.default_rng(seed)
    return cv2.GaussianBlur(rng.integers(0, 256, (h, w, 3), dtype=np.uint8), (5, 5), 0)


class TestVision(unittest.TestCase):
    def test_match_gris_y_canny(self):
        fondo = _textura(200, 300, 1)
        tpl = fondo[60:110, 120:200].copy()
        for modo in PreprocessMode:
            cfg = PreprocessConfig(mode=modo)
            m = match_template(preprocess(fondo, cfg), preprocess(tpl, cfg))
            # Canny puntúa más bajo (bordes del recorte), pero localiza igual de bien:
            # con Canny conviene bajar el umbral de similitud (~0.75-0.8).
            self.assertGreater(m.score, 0.95 if modo is PreprocessMode.GRAY else 0.8, modo)
            self.assertEqual((m.x, m.y), (120, 60), modo)

    def test_plantilla_plana_no_da_nan(self):
        fondo = np.full((50, 50), 128, np.uint8)
        m = match_template(fondo, np.full((10, 10), 128, np.uint8))
        self.assertAlmostEqual(m.score, 1.0)

    def test_reescalado_por_resolucion(self):
        lib = TemplateLibrary()
        lib.add("B", _textura(40, 80, 2), ref_size=(1920, 1080))
        tpl = lib.prepared("B", (960, 540), PreprocessConfig())
        self.assertEqual(tpl.shape, (20, 40))
        self.assertIs(lib.prepared("B", (960, 540), PreprocessConfig()), tpl)  # caché

    def test_baseline_tolera_iluminacion(self):
        img = _textura(60, 60, 3)
        base = baseline_signature(img)
        mas_brillo = cv2.add(img, np.full_like(img, 25))
        self.assertLess(baseline_delta(baseline_signature(mas_brillo), base), 0.01)
        con_boton = img.copy()
        cv2.rectangle(con_boton, (15, 20), (45, 40), (255, 255, 255), -1)  # aparece un botón
        self.assertGreater(baseline_delta(baseline_signature(con_boton), base), 0.05)


# --------------------------------------------------------------------------- #
WIN = Rect(0, 0, 400, 300)


class FakeScreen:
    """Pantalla simulada compartida entre el test y el motor."""

    def __init__(self):
        self.lock = threading.Lock()
        self.img = np.full((WIN.height, WIN.width, 3), 40, np.uint8)
        self.img[0:150, 0:200] = _textura(150, 200, 10)   # Zona 1 en estado neutro

    def show_b(self, visible: bool):
        with self.lock:
            self.img[150:300, 200:400] = TPL_B_SCENE if visible else 40


TPL_B_SCENE = _textura(150, 200, 11)
SCREEN = FakeScreen()


class FakeCapture:
    def __init__(self, *_a, **_k):
        self.bounds = WIN
        self.backend = "fake"

    def grab(self, rect: Rect):
        with SCREEN.lock:
            return SCREEN.img[rect.top:rect.bottom, rect.left:rect.right].copy()

    def close(self):
        pass


class FakeLocator:
    def __init__(self, *_a, **_k):
        pass

    def locate(self):
        return WIN


def _flow_demo() -> Flow:
    regla = Rule(
        "Acción A3",
        parse_expression('(Zona 1 == Baseline) Y (Zona 2 == "Plantilla B")'),
        actions=[{"tipo": "click", "zona": "Zona 2", "plantilla": "Plantilla B"}],
        delay=(0.01, 0.02),
    )
    return Flow(
        name="demo",
        zones=[RelativeZone("Zona 1", 0.0, 0.0, 0.5, 0.5),
               RelativeZone("Zona 2", 0.5, 0.5, 0.5, 0.5)],
        templates={"Plantilla B": TemplateRef("B.png", ref_size=(400, 300))},
        loops=[Loop("Loop 1", [Process("Proceso 1", [regla])], iterations=None, delay=(0.0, 0.0))],
    )


class TestEngine(unittest.TestCase):
    def setUp(self):
        self._orig = (engine_mod.ScreenCapture, engine_mod.WindowLocator)
        engine_mod.ScreenCapture = FakeCapture
        engine_mod.WindowLocator = FakeLocator
        SCREEN.show_b(False)

    def tearDown(self):
        engine_mod.ScreenCapture, engine_mod.WindowLocator = self._orig

    def _engine(self, runner):
        lib = TemplateLibrary()
        lib.add("Plantilla B", TPL_B_SCENE[40:90, 60:140].copy(), ref_size=(400, 300))
        cfg = engine_mod.EngineConfig(max_fps=200, observation_interval=0)
        return engine_mod.Engine(cfg, flow=_flow_demo(), action_runner=runner, templates=lib)

    def _wait(self, cond, timeout=3.0):
        fin = time.monotonic() + timeout
        while time.monotonic() < fin:
            if cond():
                return True
            time.sleep(0.01)
        return False

    def test_ciclo_completo_flanco_pausa_y_panico(self):
        runner = DryRunActionRunner()
        eng = self._engine(runner)
        self.assertTrue(eng.start())
        self.assertTrue(self._wait(lambda: eng.has_baseline("Zona 1")))
        time.sleep(0.1)
        self.assertEqual(runner.executed, [])  # Plantilla B aún no aparece

        SCREEN.show_b(True)
        self.assertTrue(self._wait(lambda: len(runner.executed) == 1))
        time.sleep(0.2)
        self.assertEqual(len(runner.executed), 1)  # flanco: no se repite

        SCREEN.show_b(False)
        time.sleep(0.1)
        SCREEN.show_b(True)
        self.assertTrue(self._wait(lambda: len(runner.executed) == 2))

        eng.pause()
        self.assertEqual(eng.state, engine_mod.EngineState.PAUSED)
        SCREEN.show_b(False)
        time.sleep(0.05)
        SCREEN.show_b(True)
        time.sleep(0.15)
        self.assertEqual(len(runner.executed), 2)  # en pausa no ejecuta
        eng.resume()

        t0 = time.monotonic()
        eng.panic()
        self.assertTrue(self._wait(lambda: not eng.is_alive(), 1.0))
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertEqual(eng.state, engine_mod.EngineState.IDLE)

        tipos = [e.type for e in eng.poll_events(1000)]
        self.assertIn(engine_mod.EventType.CALIBRATED, tipos)
        self.assertIn(engine_mod.EventType.RULE_FIRED, tipos)
        self.assertEqual(tipos[-1], engine_mod.EventType.STATE)

    def test_sin_tarea_no_arranca(self):
        eng = engine_mod.Engine()
        self.assertFalse(eng.start())
        self.assertEqual(eng.poll_events()[0].type, engine_mod.EventType.ERROR)

    def test_iteraciones_finitas(self):
        eng = self._engine(DryRunActionRunner())
        eng.flow.loops[0].iterations = 3
        self.assertTrue(eng.start())
        self.assertTrue(self._wait(lambda: not eng.is_alive()))
        fin = [e for e in eng.poll_events(1000) if e.type is engine_mod.EventType.FINISHED]
        self.assertEqual(fin[0].payload, {"completado": True})


if __name__ == "__main__":
    unittest.main()
