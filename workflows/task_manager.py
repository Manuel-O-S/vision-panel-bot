"""
task_manager.py - Guardado y carga de tareas (.json / .tarea).

Una tarea empaqueta todo lo necesario para reproducir una automatización:

    {
      "schema_version": 2,
      "creado": "2026-11-17T10:00:00", "modificado": "...",
      "ajustes": {
        "ventana": "Mi App", "umbral_similitud": 0.85, "tolerancia_delta": 0.05,
        "preproceso": "gris", "max_fps": 30, "esc_panico": true
      },
      "flujo": {
        "nombre": "...",
        "zonas":      [{"nombre": "Zona 1", "x": 0.1, "y": 0.2, "ancho": 0.3, "alto": 0.1}],
        "plantillas": {"Plantilla B": {"archivo": "assets/templates/Plantilla_B.png", "ref": [1920, 1080]}},
        "loops":      [{"nombre": "Loop 1", "iteraciones": null, "delay": [0.5, 1.2],
                        "procesos": [{"nombre": "Proceso 1", "reglas": [...]}]}]
      }
    }

Las imágenes de las plantillas viven en ``assets/templates`` y la tarea guarda
su ruta relativa a la raíz del proyecto.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import cv2
import numpy as np

from core.engine import EngineConfig
from core.rules import Flow, Loop, Process
from core.vision import PreprocessConfig, PreprocessMode

__all__ = ["SCHEMA_VERSION", "VALID_EXTENSIONS", "TaskError", "TaskSettings", "Task",
           "TaskManager", "save_template_image", "safe_filename"]

SCHEMA_VERSION = 2
VALID_EXTENSIONS = {".json", ".tarea"}


class TaskError(ValueError):
    """Error con mensaje apto para mostrar al usuario."""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class TaskSettings:
    window_title: str | None = None
    match_threshold: float = 0.85
    delta_threshold: float = 0.05
    preprocess: str = PreprocessMode.GRAY.value
    max_fps: float = 30.0
    esc_panic: bool = True

    def apply_to(self, config: EngineConfig) -> EngineConfig:
        """Nueva configuración del motor con estos ajustes."""
        return replace(
            config,
            window_title=self.window_title or None,
            match_threshold=self.match_threshold,
            delta_threshold=self.delta_threshold,
            preprocess=replace(config.preprocess, mode=PreprocessMode(self.preprocess)),
            max_fps=self.max_fps,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ventana": self.window_title,
            "umbral_similitud": self.match_threshold,
            "tolerancia_delta": self.delta_threshold,
            "preproceso": self.preprocess,
            "max_fps": self.max_fps,
            "esc_panico": self.esc_panic,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TaskSettings:
        s = cls(
            window_title=data.get("ventana") or None,
            match_threshold=float(data.get("umbral_similitud", 0.85)),
            delta_threshold=float(data.get("tolerancia_delta", 0.05)),
            preprocess=str(data.get("preproceso", PreprocessMode.GRAY.value)),
            max_fps=float(data.get("max_fps", 30.0)),
            esc_panic=bool(data.get("esc_panico", True)),
        )
        # Valida con las mismas reglas que el motor.
        s.apply_to(EngineConfig(preprocess=PreprocessConfig()))
        return s


@dataclass
class Task:
    flow: Flow
    settings: TaskSettings = field(default_factory=TaskSettings)
    created: str = field(default_factory=_now)
    modified: str = field(default_factory=_now)

    @property
    def name(self) -> str:
        return self.flow.name

    @classmethod
    def new(cls, name: str = "Nueva tarea") -> Task:
        """Tarea vacía con la estructura mínima: Loop 1 > Proceso 1."""
        flow = Flow(name=name, loops=[Loop("Loop 1", [Process("Proceso 1")], delay=(0.3, 0.8))])
        return cls(flow=flow)

    def copy_flow(self) -> Flow:
        """Copia profunda del flujo (el motor ejecuta la copia; la GUI edita el original)."""
        return Flow.from_dict(self.flow.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "creado": self.created,
            "modificado": self.modified,
            "ajustes": self.settings.to_dict(),
            "flujo": self.flow.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Task:
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise TaskError(f"Versión de tarea no soportada: {version} (se esperaba {SCHEMA_VERSION})")
        return cls(
            flow=Flow.from_dict(data["flujo"]),
            settings=TaskSettings.from_dict(data.get("ajustes", {})),
            created=str(data.get("creado", _now())),
            modified=str(data.get("modificado", _now())),
        )


class TaskManager:
    def __init__(self, tasks_dir: str | Path = "tasks") -> None:
        self.tasks_dir = Path(tasks_dir)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

    def save(self, task: Task, path: str | Path | None = None) -> Path:
        """Guarda de forma atómica (temporal + replace): un corte de luz no deja
        el archivo a medias."""
        path = Path(path) if path else self.tasks_dir / f"{safe_filename(task.name)}.tarea"
        if path.suffix.lower() not in VALID_EXTENSIONS:
            raise TaskError(f"Extensión no válida: {path.suffix or '(ninguna)'} (usa .tarea o .json)")
        path.parent.mkdir(parents=True, exist_ok=True)
        task.modified = _now()
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(task.to_dict(), f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return path

    def load(self, path: str | Path) -> Task:
        path = Path(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise TaskError(f"No se pudo leer {path.name}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise TaskError(f"{path.name} no es un JSON válido (línea {exc.lineno})") from exc
        if not isinstance(data, dict):
            raise TaskError(f"{path.name} no contiene una tarea")
        try:
            return Task.from_dict(data)
        except TaskError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise TaskError(f"{path.name} está dañada o incompleta: {exc}") from exc

    def list_tasks(self) -> list[Path]:
        return sorted(p for p in self.tasks_dir.iterdir() if p.suffix.lower() in VALID_EXTENSIONS)


_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def safe_filename(name: str) -> str:
    """Nombre válido en Windows (sin caracteres prohibidos ni espacios)."""
    limpio = re.sub(r"_+", "_", _UNSAFE.sub("_", name.strip(" .")).replace(" ", "_"))
    return limpio.strip("_") or "sin_nombre"


def save_template_image(image: np.ndarray, name: str, templates_dir: Path, root: Path) -> str:
    """Guarda la plantilla como PNG y devuelve su ruta relativa a ``root``.

    Usa ``imencode`` + ``tofile`` porque ``cv2.imwrite`` falla con tildes/ñ en Windows.
    """
    templates_dir.mkdir(parents=True, exist_ok=True)
    path = templates_dir / f"{safe_filename(name)}.png"
    ok, buf = cv2.imencode(".png", image)
    if not ok:
        raise TaskError(f"No se pudo codificar la plantilla {name!r}")
    buf.tofile(str(path))
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)
