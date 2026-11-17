"""
main.py - Punto de entrada: configura logging, inicializa el motor y la GUI.
"""
from __future__ import annotations

import ctypes
import logging
import sys


def enable_dpi_awareness() -> None:
    """Evita que Windows escale la ventana: coordenadas de pantalla = píxeles reales."""
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:  # noqa: BLE001
            ctypes.windll.user32.SetProcessDPIAware()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(threadName)s %(name)s: %(message)s",
    )
    enable_dpi_awareness()

    from core.engine import Engine
    from gui.main_window import MainWindow

    engine = Engine()
    app = MainWindow(engine)  # la GUI corre en el hilo principal; el motor en su propio hilo
    app.mainloop()


if __name__ == "__main__":
    main()
