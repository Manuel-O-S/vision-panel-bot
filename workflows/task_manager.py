"""
task_manager.py - Guardado y carga de tareas (.json / .tarea).
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
VALID_EXTENSIONS = {".json", ".tarea"}


@dataclass
class Task:
    name: str
    blocks: list[dict[str, Any]] = field(default_factory=list)
    regions: list[list[int]] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    created: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    schema_version: int = SCHEMA_VERSION


class TaskManager:
    def __init__(self, tasks_dir: str | Path = "tasks") -> None:
        self.tasks_dir = Path(tasks_dir)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

    def save(self, task: Task, filename: str | None = None) -> Path:
        path = self.tasks_dir / (filename or f"{task.name}.tarea")
        if path.suffix not in VALID_EXTENSIONS:
            raise ValueError(f"Extensión no válida: {path.suffix}")
        # Escritura atómica: archivo temporal + replace
        fd, tmp = tempfile.mkstemp(dir=self.tasks_dir, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(task), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return path

    def load(self, path: str | Path) -> Task:
        path = Path(path)
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        if data.get("schema_version", 0) > SCHEMA_VERSION:
            raise ValueError("Tarea creada con una versión más reciente")
        return Task(**data)

    def list_tasks(self) -> list[Path]:
        return sorted(p for p in self.tasks_dir.iterdir() if p.suffix in VALID_EXTENSIONS)
