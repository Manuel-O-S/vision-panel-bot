"""
engine.py - Motor de ejecución concurrente (núcleo del panel).

Arquitectura de 2 hilos
    Hilo 1 (principal) - GUI. Nunca se bloquea: llama a métodos no bloqueantes
        del motor (start, pause, panic...) y lee ``engine.events`` con
        ``poll_events()`` desde un ``after(16, ...)`` (≈ 60 FPS).
    Hilo 2 (EngineWorker) - bucle continuo:
        localizar ventana -> capturar ROI -> OpenCV -> evaluar reglas -> acciones.

Comunicación
    GUI -> motor : métodos thread-safe (``threading.Event`` + ``Lock``).
    motor -> GUI : ``engine.events`` (``queue.Queue`` acotada de ``EngineEvent``).
                   Si la GUI no consume, se descartan los eventos más antiguos
                   para que la memoria no crezca sin límite.

Parada
    ``stop()``  -> parada ordenada (espera al hilo con timeout).
    ``panic()`` -> no bloqueante, pensada para hotkeys: todos los delays del
                   motor son ``Event.wait`` interrumpibles, así que el hilo se
                   detiene en cuanto termina la acción de entrada en curso.

Uso mínimo
    engine = Engine(EngineConfig(window_title="Mi App"), flow=Flow.from_dict(datos))
    engine.start()
    ...
    for ev in engine.poll_events(): print(ev)
    engine.stop()
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from functools import reduce
from pathlib import Path
from typing import Any

import numpy as np

from core.actions import ActionContext, ActionError, ActionRunner, DefaultActionRunner, expand_actions
from core.entropy import dormir_entropico, generar_delay_entropico
from core.geometry import Rect
from core.rules import BASELINE, Flow, Loop, Rule, ZoneObservation
from core.vision import (
    PreprocessConfig,
    ScreenCapture,
    TemplateLibrary,
    WindowLocator,
    baseline_delta,
    baseline_signature,
    crop,
    match_template,
    preprocess,
)

log = logging.getLogger(__name__)

__all__ = ["Engine", "EngineConfig", "EngineState", "EngineEvent", "EventType"]


class EngineState(Enum):
    IDLE = auto()
    RUNNING = auto()
    PAUSED = auto()
    STOPPING = auto()


class EventType(str, Enum):
    STATE = "estado"              # payload: EngineState
    LOG = "log"                   # payload: str
    ERROR = "error"               # payload: str
    OBSERVATION = "observacion"   # payload: {zona: ZoneObservation.to_dict()}
    RULE_FIRED = "regla"          # payload: {"loop", "proceso", "regla"}
    CALIBRATED = "calibrado"      # payload: {"zona"}
    WINDOW_LOST = "ventana"       # payload: título buscado
    FINISHED = "fin"              # payload: {"completado": bool}


@dataclass(frozen=True, slots=True)
class EngineEvent:
    type: EventType
    payload: Any = None
    timestamp: float = field(default_factory=time.time)


@dataclass(slots=True)
class EngineConfig:
    """Parámetros del motor. Sólo se deben cambiar con el motor detenido."""

    window_title: str | None = None       # subcadena del título; None = pantalla completa
    max_fps: float = 30.0                 # tope de ciclos de visión por segundo
    match_threshold: float = 0.85         # similitud mínima para "Zona == Plantilla"
    delta_threshold: float = 0.05         # tolerancia global contra el Baseline
    preprocess: PreprocessConfig = field(default_factory=PreprocessConfig)
    prefer_dxcam: bool = True
    base_dir: Path = field(default_factory=Path.cwd)  # raíz de rutas relativas de plantillas
    observation_interval: float = 0.1     # s entre eventos OBSERVATION (0 = no enviar)
    window_retry_interval: float = 0.5    # s de espera si la ventana no está disponible
    event_queue_size: int = 2000

    def __post_init__(self) -> None:
        if self.max_fps <= 0:
            raise ValueError("max_fps debe ser > 0")
        if not -1.0 <= self.match_threshold <= 1.0:
            raise ValueError("match_threshold debe estar entre -1 y 1")
        if not 0.0 <= self.delta_threshold <= 1.0:
            raise ValueError("delta_threshold debe estar entre 0 y 1")
        self.base_dir = Path(self.base_dir)


@dataclass(slots=True)
class _Snapshot:
    window: Rect
    zones: dict[str, Rect]
    observations: dict[str, ZoneObservation]


def fmt_elapsed(segundos: float) -> str:
    s = int(segundos)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


@dataclass
class RunStats:
    """Contadores de una ejecución: disparos por regla, reintentos, errores..."""

    start: float = field(default_factory=time.monotonic)
    fires: Counter[str] = field(default_factory=Counter)
    retries: Counter[str] = field(default_factory=Counter)
    errors: int = 0
    window_lost: int = 0

    @property
    def total(self) -> int:
        return sum(self.fires.values())

    def elapsed(self) -> float:
        return time.monotonic() - self.start

    def fired(self, rule: str, retry: bool = False) -> int:
        self.fires[rule] += 1
        if retry:
            self.retries[rule] += 1
        return self.fires[rule]

    def summary(self) -> str:
        t = self.elapsed()
        partes = []
        for regla, n in self.fires.items():
            txt = f"{regla} ×{n}"
            if t >= 60:
                txt += f" ({n / (t / 60):.1f}/min)"
            if self.retries[regla]:
                txt += f", {self.retries[regla]} reintentos"
            partes.append(txt)
        reglas = " · ".join(partes) or "ninguna regla se disparó"
        return (f"Σ Resumen {fmt_elapsed(t)}: {reglas} | errores {self.errors}"
                f" | ventana perdida {self.window_lost}")


class Engine:
    """Motor de automatización. Todos los métodos públicos son thread-safe."""

    def __init__(
        self,
        config: EngineConfig | None = None,
        flow: Flow | None = None,
        action_runner: ActionRunner | None = None,
        templates: TemplateLibrary | None = None,
    ) -> None:
        self.config = config or EngineConfig()
        self.templates = templates or TemplateLibrary()
        self.events: queue.Queue[EngineEvent] = queue.Queue(maxsize=self.config.event_queue_size)

        self._flow = flow
        self._runner: ActionRunner = action_runner or DefaultActionRunner(self.config.base_dir)
        self._state = EngineState.IDLE
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # set = NO pausado
        self._thread: threading.Thread | None = None
        self._listeners: list[Callable[[EngineState], None]] = []

        # Estado de visión compartido (protegido por _lock).
        self._baselines: dict[str, np.ndarray] = {}
        self._delta_global = self.config.delta_threshold
        self._delta_overrides: dict[str, float] = {}

        # Estado exclusivo del hilo de trabajo.
        self._capture: ScreenCapture | None = None
        self._locator: WindowLocator | None = None
        self._reqs: dict[str, set[str]] = {}
        self._window_lost = False
        self._last_obs_emit = 0.0
        self._best_scores: dict[tuple[str, str], float] = {}  # (zona, plantilla) -> máx. de la ejecución
        self.stats = RunStats()  # contadores de la ejecución actual (para el registro)

    # =============================== estado =============================== #
    @property
    def state(self) -> EngineState:
        with self._lock:
            return self._state

    @property
    def flow(self) -> Flow | None:
        return self._flow

    def is_alive(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def _set_state(self, new_state: EngineState) -> None:
        with self._lock:
            if self._state is new_state:
                return
            self._state = new_state
        self._emit(EventType.STATE, new_state)
        for cb in list(self._listeners):
            try:
                cb(new_state)
            except Exception:  # noqa: BLE001
                log.exception("Error en listener de estado")

    def add_state_listener(self, callback: Callable[[EngineState], None]) -> None:
        """Callback síncrono en el hilo que cambia el estado (¡puede ser el motor!).

        En la GUI es preferible leer los eventos STATE con ``poll_events()``.
        """
        self._listeners.append(callback)

    # ============================== eventos =============================== #
    def _emit(self, kind: EventType, payload: Any = None) -> None:
        if kind is EventType.ERROR:
            self.stats.errors += 1
        elif kind is EventType.WINDOW_LOST:
            self.stats.window_lost += 1
        ev = EngineEvent(kind, payload)
        try:
            self.events.put_nowait(ev)
        except queue.Full:
            try:
                self.events.get_nowait()  # descarta el más antiguo
            except queue.Empty:
                pass
            try:
                self.events.put_nowait(ev)
            except queue.Full:
                pass

    def poll_events(self, max_items: int = 200) -> list[EngineEvent]:
        """Saca hasta ``max_items`` eventos sin bloquear (para el ``after()`` de la GUI)."""
        out: list[EngineEvent] = []
        for _ in range(max_items):
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                break
        return out

    # ============================== control =============================== #
    def load_flow(self, flow: Flow) -> None:
        if self.is_alive():
            raise RuntimeError("Detén el motor antes de cargar otra tarea")
        self._flow = flow
        with self._lock:
            self._baselines.clear()
            self._delta_overrides.clear()

    def start(self) -> bool:
        """Arranca el hilo de trabajo. Devuelve False (y emite ERROR) si no puede."""
        with self._lock:
            if self.is_alive():
                if self._state is EngineState.PAUSED:
                    self.resume()
                    return True
                return self._state is EngineState.RUNNING
            flow = self._flow
            if flow is None:
                self._emit(EventType.ERROR, "No hay ninguna tarea cargada")
                return False
            errores = flow.validate() + self._load_templates(flow)
            if errores:
                for e in errores:
                    self._emit(EventType.ERROR, e)
                return False

            flow.reset()
            self._stop_event.clear()
            self._pause_event.set()
            self._set_state(EngineState.RUNNING)  # antes de start(): evita carreras con el finally
            self._thread = threading.Thread(
                target=self._run, args=(flow,), name="EngineWorker", daemon=True)
            self._thread.start()
            return True

    def pause(self) -> None:
        with self._lock:
            if self._state is EngineState.RUNNING:
                self._pause_event.clear()
                self._set_state(EngineState.PAUSED)

    def resume(self) -> None:
        with self._lock:
            if self._state is EngineState.PAUSED:
                self._pause_event.set()
                self._set_state(EngineState.RUNNING)

    def toggle_pause(self) -> None:
        """Para el hotkey de pausa/reproducción."""
        with self._lock:
            if self._state is EngineState.RUNNING:
                self.pause()
            elif self._state is EngineState.PAUSED:
                self.resume()

    def stop(self, timeout: float = 2.0) -> bool:
        """Parada ordenada. Devuelve True si el hilo terminó dentro del timeout."""
        t = self._thread
        if t is None or not t.is_alive():
            self._set_state(EngineState.IDLE)
            return True
        self._set_state(EngineState.STOPPING)
        self._stop_event.set()
        self._pause_event.set()  # despierta al hilo si estaba en pausa
        if threading.current_thread() is t:
            return False  # el hilo no puede esperarse a sí mismo
        t.join(timeout)
        if t.is_alive() and timeout:  # timeout=0 (GUI): no esperar es lo normal, no un fallo
            log.warning("El hilo del motor no terminó en %.1fs", timeout)
            return False
        return True

    def panic(self) -> None:
        """Botón de pánico: detiene todo al instante. No bloquea (seguro desde hotkeys)."""
        self._stop_event.set()
        self._pause_event.set()
        if self.is_alive():
            self._set_state(EngineState.STOPPING)
        self._emit(EventType.LOG, "PÁNICO: motor detenido")
        log.warning("Botón de pánico activado")

    # ===================== baseline y tolerancias (Delta) ================= #
    def recalibrate(self, zone: str | None = None) -> None:
        """Recalibración rápida: el Baseline se vuelve a tomar en el siguiente ciclo
        (o al pulsar Play si el motor está parado). ``zone=None`` = todas."""
        with self._lock:
            if zone is None:
                self._baselines.clear()
            else:
                self._baselines.pop(zone, None)
        self._emit(EventType.LOG, f"Recalibración pedida: {zone or 'todas las zonas'}")

    def set_baseline(self, zone: str, image_bgr: np.ndarray) -> None:
        """Fija el Baseline de una zona desde una imagen (p. ej. guardada en la tarea)."""
        sig = baseline_signature(image_bgr)
        with self._lock:
            self._baselines[zone] = sig

    def has_baseline(self, zone: str) -> bool:
        with self._lock:
            return zone in self._baselines

    def set_delta_threshold(self, value: float, zone: str | None = None) -> None:
        """Ajuste en caliente de la tolerancia. Precedencia: zona (aquí) >
        ``RelativeZone.tolerance`` > global."""
        if not 0.0 <= value <= 1.0:
            raise ValueError("La tolerancia debe estar entre 0 y 1")
        with self._lock:
            if zone is None:
                self._delta_global = value
            else:
                self._delta_overrides[zone] = value

    def _tolerance_for(self, zone: str, flow: Flow) -> float:
        with self._lock:
            if zone in self._delta_overrides:
                return self._delta_overrides[zone]
            z = flow.zone(zone)
            if z is not None and z.tolerance is not None:
                return z.tolerance
            return self._delta_global

    # ============================= plantillas ============================= #
    def _load_templates(self, flow: Flow) -> list[str]:
        errores = []
        for name, ref in flow.templates.items():
            if name in self.templates:
                continue
            path = Path(ref.path)
            if not path.is_absolute():
                path = self.config.base_dir / path
            try:
                self.templates.load_file(name, path, ref.ref_size)
            except (OSError, ValueError) as exc:
                errores.append(f"Plantilla {name!r}: {exc}")
        return errores

    # ========================= hilo de trabajo ============================ #
    def _run(self, flow: Flow) -> None:
        completado = False
        try:
            self._capture = ScreenCapture(self.config.prefer_dxcam)
            self._locator = WindowLocator(self.config.window_title)
            self._reqs = flow.requirements()
            self._window_lost = False
            self._best_scores = {}
            self.stats = RunStats()
            self._emit(EventType.LOG, f"Tarea {flow.name!r} iniciada ({self._capture.backend})")
            completado = all(self._run_loop(flow, loop) for loop in flow.loops)
        except Exception as exc:  # noqa: BLE001 - el hilo nunca debe morir en silencio
            log.exception("Error fatal en el motor")
            self._emit(EventType.ERROR, f"Error fatal en el motor: {exc}")
        finally:
            if self._capture is not None:
                self._capture.close()
                self._capture = None
            self._emit_best_scores()
            self._emit(EventType.LOG, self.stats.summary())
            self._emit(EventType.FINISHED, {"completado": completado})
            self._set_state(EngineState.IDLE)

    def _emit_best_scores(self) -> None:
        """Resumen al terminar: ayuda a elegir el umbral sin mirar el monitor en vivo."""
        umbral = self.config.match_threshold
        for (zona, t), score in sorted(self._best_scores.items()):
            nota = "✔ llegó al umbral" if score >= umbral else "✖ nunca llegó al umbral"
            if score == 0.0:
                nota += " — la zona sólo vio un color plano (¿tapada por otra ventana?)"
            self._emit(EventType.LOG, f"Mejor parecido {zona} › {t}: {score:.3f} "
                                      f"(umbral {umbral:.2f}) {nota}")

    def _checkpoint(self) -> bool:
        """Bloquea mientras esté en pausa. False => hay que salir."""
        while not self._pause_event.is_set():
            if self._stop_event.is_set():
                return False
            self._pause_event.wait(0.1)
        return not self._stop_event.is_set()

    def _run_loop(self, flow: Flow, loop: Loop) -> bool:
        """Ejecuta un Loop. Devuelve False si se pidió detener el motor."""
        min_period = 1.0 / self.config.max_fps
        hechas = 0
        while loop.iterations is None or hechas < loop.iterations:
            t0 = time.monotonic()
            if not self._checkpoint():
                return False

            snap = self._observe(flow)
            if snap is None:  # ventana no disponible: reintentar sin contar iteración
                if self._stop_event.wait(self.config.window_retry_interval):
                    return False
                continue

            for proc in loop.processes:
                if snap is None:
                    break
                disparadas = proc.evaluate(snap.observations, self.config.match_threshold)
                for regla in disparadas:
                    n = self.stats.fired(regla.name, regla.is_retry)
                    if regla.is_retry:
                        self._emit(EventType.LOG, f"↻ '{regla.name}': su condición sigue igual tras "
                                                  f"{regla.retry:g} s; se reintenta")
                    self._emit(EventType.RULE_FIRED,
                               {"loop": loop.name, "proceso": proc.name, "regla": regla.name,
                                "n": n, "total": self.stats.total, "t": self.stats.elapsed()})
                    ok = self._fire(flow, regla, snap)
                    regla.mark_done()
                    if not ok:
                        return False
                if disparadas:
                    # La pantalla cambió: los procesos siguientes ven un frame nuevo.
                    snap = self._observe(flow)

            hechas += 1
            if loop.iterations is not None and hechas >= loop.iterations:
                break
            espera = generar_delay_entropico(*loop.delay) if loop.delay[1] > 0 else 0.0
            espera = max(espera, min_period - (time.monotonic() - t0))
            if espera > 0 and self._stop_event.wait(espera):
                return False
        return True

    def _fire(self, flow: Flow, rule: Rule, snap: _Snapshot) -> bool:
        """Delay entrópico previo + acciones de la regla. False => detener motor."""
        d_min, d_max = rule.delay
        if d_max > 0 and not dormir_entropico(d_min, d_max, self._stop_event):
            return False
        if not self._checkpoint():  # respeta una pausa pulsada durante el delay
            return False

        try:
            pasos = list(expand_actions(rule.actions, flow.actions))
        except ActionError as exc:
            self._emit(EventType.ERROR, f"{rule.name}: {exc}")
            return not self._stop_event.is_set()

        ctx = ActionContext(snap.window, snap.zones, snap.observations, self._stop_event, rule.name,
                            log=lambda m, r=rule.name: self._emit(EventType.LOG, f"[{r}] {m}"))
        for i, accion in enumerate(pasos, start=1):
            if self._stop_event.is_set():
                return False
            try:
                self._runner(accion, ctx)
            except Exception as exc:  # noqa: BLE001 - una acción rota no tumba el motor
                log.exception("Acción %d de %r falló", i, rule.name)
                self._emit(EventType.ERROR, f"{rule.name}: la acción {i} falló: {exc}")
                break
        return not self._stop_event.is_set()

    # ------------------------------ visión ------------------------------ #
    def _observe(self, flow: Flow) -> _Snapshot | None:
        """Captura UNA vez el rectángulo que engloba las zonas usadas y mide cada una."""
        assert self._locator is not None and self._capture is not None
        win = self._locator.locate()
        if win is None:
            if not self._window_lost:
                self._window_lost = True
                self._emit(EventType.WINDOW_LOST, self.config.window_title)
            return None
        if self._window_lost:
            self._window_lost = False
            self._emit(EventType.LOG, "Ventana objetivo recuperada")

        zone_rects = {z.name: z.to_rect(win) for z in flow.zones}
        usadas = [n for n in self._reqs if n in zone_rects]
        observaciones: dict[str, ZoneObservation] = {}

        if usadas:
            bbox = reduce(Rect.union, (zone_rects[n] for n in usadas)).intersect(self._capture.bounds)
            if bbox is not None:
                frame = self._capture.grab(bbox)
                if frame is None:
                    return None
                for nombre in usadas:
                    o = self._measure_zone(flow, nombre, frame, bbox, zone_rects[nombre], win)
                    if o is not None:
                        observaciones[nombre] = o

        snap = _Snapshot(win, zone_rects, observaciones)
        self._maybe_emit_observation(snap)
        return snap

    def _measure_zone(self, flow: Flow, nombre: str, frame: np.ndarray, frame_rect: Rect,
                      zone_rect: Rect, win: Rect) -> ZoneObservation | None:
        region = crop(frame, frame_rect, zone_rect)
        if region is None or region.size == 0:
            return None  # zona fuera de pantalla
        origen = frame_rect.intersect(zone_rect)
        assert origen is not None
        o = ZoneObservation(nombre)
        objetivos = self._reqs[nombre]

        if BASELINE in objetivos:
            sig = baseline_signature(region)
            with self._lock:
                base = self._baselines.get(nombre)
                if base is None:
                    self._baselines[nombre] = base = sig
                    calibrada = True
                else:
                    calibrada = False
            if calibrada:
                self._emit(EventType.CALIBRATED, {"zona": nombre})
            o.baseline_delta = baseline_delta(sig, base)
            o.is_baseline = o.baseline_delta <= self._tolerance_for(nombre, flow)

        plantillas = sorted(objetivos - {BASELINE})
        if plantillas:
            cfg = self.config.preprocess
            procesada = preprocess(region, cfg)
            for t in plantillas:
                tpl = self.templates.prepared(t, win.size, cfg)
                if tpl is None:
                    continue
                m = match_template(procesada, tpl)
                o.scores[t] = m.score
                clave = (nombre, t)
                if m.score > self._best_scores.get(clave, -2.0):
                    self._best_scores[clave] = m.score
                o.locations[t] = (origen.left + m.x, origen.top + m.y, m.w, m.h)
        return o

    def _maybe_emit_observation(self, snap: _Snapshot) -> None:
        intervalo = self.config.observation_interval
        if intervalo <= 0:
            return
        ahora = time.monotonic()
        if ahora - self._last_obs_emit >= intervalo:
            self._last_obs_emit = ahora
            self._emit(EventType.OBSERVATION,
                       {n: o.to_dict() for n, o in snap.observations.items()})
