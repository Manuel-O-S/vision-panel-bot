"""
main.py - Punto de entrada: logging, DPI, motor, tareas y GUI.
"""
from __future__ import annotations

import ctypes
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def enable_dpi_awareness() -> None:
    """Evita que Windows escale la ventana: coordenadas de pantalla = píxeles reales.

    Debe llamarse ANTES de crear cualquier ventana Tk.
    """
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor
        except Exception:  # noqa: BLE001
            ctypes.windll.user32.SetProcessDPIAware()


def setup_logging() -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    fmt = "%(asctime)s [%(levelname)s] %(threadName)s %(name)s: %(message)s"
    archivo = RotatingFileHandler(ROOT / "logs" / "vision_bot.log", maxBytes=2_000_000,
                                  backupCount=3, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format=fmt,
                        handlers=[logging.StreamHandler(), archivo])


def main() -> None:
    setup_logging()
    enable_dpi_awareness()

    from core.engine import Engine, EngineConfig
    from gui.main_window import MainWindow
    from workflows.task_manager import TaskManager

    engine = Engine(EngineConfig(base_dir=ROOT))  # hilo 2: se crea al pulsar Play
    tasks = TaskManager(ROOT / "tasks")
    app = MainWindow(engine, tasks, ROOT)         # hilo 1: GUI
    app.mainloop()


if __name__ == "__main__":
    main()
