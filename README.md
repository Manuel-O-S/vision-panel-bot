# vision-panel-bot

Modular Computer Vision Automation Panel & Workflow Engine built with Python and OpenCV.

## Stack
| Capa | Tecnología |
|------|------------|
| Captura | `dxcam` (DXGI, alto FPS) con *fallback* a `mss` |
| Visión | OpenCV `matchTemplate` (`TM_CCOEFF_NORMED`) |
| Entrada | `pydirectinput` (SendInput) |
| Aleatoriedad | `secrets.SystemRandom` + Box-Muller (4 decimales) |
| GUI | CustomTkinter |
| Persistencia | JSON (`.json` / `.tarea`) |

## Estructura
```
core/        engine.py · vision.py · entropy.py · input_sim.py
             geometry.py (zonas relativas) · rules.py (Y/O/NOT, flanco, loops) · actions.py
tests/       python -m unittest discover -s tests -v
gui/         main_window.py · snipping_tool.py · block_editor.py
workflows/   task_manager.py
actions/     macro_recorder.py · custom_scripts/
assets/      templates/   (imágenes de referencia recortadas)
main.py      punto de entrada
```

## Arquitectura
- **Hilo principal**: GUI (Tk no es thread-safe; las actualizaciones desde otros hilos van por `after()`).
- **Hilo del motor** (`core.engine.Engine`): bucle `capturar → detectar → decidir → actuar`
  con estados `IDLE / RUNNING / PAUSED / STOPPING` controlados por `threading.Event`.
- **Listeners** de `pynput`: hilos propios para la grabación de macros.

## Instalación
```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

> Nota: `dxcam` solo funciona en Windows. Algunos juegos/aplicaciones requieren ejecutar
> como administrador para recibir entradas de `SendInput`.

## Plantillas
Guarda las imágenes recortadas en `assets/templates/` como `.png`. El nombre del archivo
(sin extensión) es el identificador de la plantilla, p. ej. `cr7.png` → `"cr7"`.
