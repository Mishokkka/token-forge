# TokenForge 1.8.0

Local Windows 10/11 utility for creating transparent character tokens and equipment artwork for Foundry VTT and other virtual tabletops. Background removal uses local rembg/ONNX Runtime models; the initial model download needs internet access.

**[Русская инструкция / Complete Russian guide](README_RU.md)** · [Architecture and guarantees](ARCHITECTURE.md) · [Changelog](CHANGELOG.md)

## Installation (Windows)

1. [Download the main branch ZIP](https://github.com/Mishokkka/token-forge/archive/refs/heads/main.zip) and extract the complete directory.
2. Install regular 64-bit CPython 3.11–3.13 with Tcl/Tk (Python 3.12 recommended; Windows 3.13.0 is unsupported). Install the Microsoft Visual C++ 2015–2022 x64 Redistributable if ONNX Runtime cannot load.
3. Run `install.bat`, then `run.bat`. The installer uses the tested dependency constraints for your Python minor version.
4. Optionally run `install_context_menu.bat` to add the owned Windows Explorer **Send to** shortcuts. To remove them, run `remove_context_menu.bat`.

When updating, copy the complete source distribution, not individual modules. Preserve a custom ring separately; settings and cache live under `%LOCALAPPDATA%\TokenForge`.

## What it produces

- Character tokens: 256, 512 or 1024 square PNG/WebP, clipped under a configurable transparent ring.
- Equipment: 500 × 500 lossless WebP, 3 px black outline, complete fit, rotation, mirrors and placement controls.
- Optional high-quality WebP source copy beside the untouched original.

Select a folder, choose the desired mode, adjust the preview and export one image or the entire folder. Use **Сохранить все изменения** (Save all changes) after adjusting multiple arts: only arts edited in the current folder and mode during this app session are exported, even with Skip existing enabled. Successfully saved edits are cleared; failed, cancelled or newer edits remain pending. The button never authorizes replacement of independent images; use Save selected for explicit approval. Existing independent images require explicit overwrite approval; legacy or stale generated files are not silently overwritten by skip mode.

Limits: PNG/JPEG/WebP/BMP/TIFF, 256 MiB encoded size, 64 Mi pixels, side at most 16384 px, rings at most 4096 square. Valid embedded color profiles are converted to sRGB. Uncached background removal has separate work-size limits: ordinary 16 Mi pixels, edge cleanup 4 Mi pixels, alpha matting 1 Mi pixel. Larger images can still be viewed/copied subject to decoder limits; for extraction, resize or choose a less expensive mode. Transparent bypass and valid cache hits do not require inference. Signed/float/32-bit grayscale data needs conversion to a defined 8/16-bit display range first.

## Checks

`check_install.bat` runs diagnostics and offline regression tests. `gui_smoke.py` runs genuine Tk event-loop integration with synthetic transparent art. Actions covers Python 3.11–3.13 on Windows. Model quality, first download, large real-world folders and actual Explorer interaction are manual release checks.

### Model readiness and cancellation
The desktop shows whether the selected model has a local weights file, displays downloaded bytes, percent/speed/ETA when available, and distinguishes model initialization from background removal. Native processing shows elapsed time rather than a fictional percent. Stop or close cancels a model download/inference process; completed files remain intact. Local presence does not guarantee ONNX initialization. Diagnostic stage/error log: `%LOCALAPPDATA%\TokenForge\inference.log`. Automatic Explorer actions still use the existing silent CLI.
