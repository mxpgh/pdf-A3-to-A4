# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A macOS desktop tool (PyQt5) that splits scanned/printed A3 exam PDFs (two A4 pages side-by-side) into single-page A4 PDFs for home printing. Distributed as a `.app` bundle built with PyInstaller.

## Commands

All commands assume miniconda's Python (the repo's working interpreter — `/Library/Frameworks/Python.framework/...` Python has stale/broken `fitz`):

```bash
# Run from source (GUI)
/opt/miniconda3/bin/python main.py

# Headless smoke (Qt offscreen, no display needed)
QT_QPA_PLATFORM=offscreen /opt/miniconda3/bin/python test_preview.py

# Build .app — always go through build.sh (it preserves the .spec)
./build.sh
# Output: dist/A3转A4试卷转换.app (~217 MB)

# Launch built .app
open "dist/A3转A4试卷转换.app"
```

There is no pytest suite. `test_preview.py` is a smoke script, not asserts. Quick algorithm regression is done inline:

```bash
/opt/miniconda3/bin/python -c "
import sys; sys.path.insert(0,'.')
from splitter_core import process_pdf
print(process_pdf('test_a3_exam.pdf', 'test_output'))
"
```

## Architecture

Two-layer split — keep this boundary intact:

- **`splitter_core.py`** — pure algorithm, no Qt. End-to-end entry is `process_pdf(pdf_path, out_dir, divider_override=None) → (out_path, preview_pairs)`. Pipeline: `load_pdf_pages` (pdf2image with PyMuPDF fallback) → `detect_divider` (per page) → `split_page` → `auto_crop` → `build_a4_pdf` (JPEG-embedded, downsampled to `target_print_dpi=200`).
- **`main.py`** — PyQt5 GUI. All long work runs in `QThread` subclasses (`PreviewWorker`, `ConvertWorker`); the main thread only consumes structured-output signals.

`divider_override` is **a list aligned to pages**, one int per page (or `None` for auto). The historic single-int form still works (auto-wrapped to a 1-element list). The UI maintains `self._auto_dividers` per page and pushes the whole list into `ConvertWorker`, so user adjustments on page N apply to page N only.

### Qt threading rules (broken once, do not break again)

Custom signals on `QThread` subclasses **must not be named `finished`** — that name shadows the built-in `QThread.finished`. Use `loaded` / `done` instead. `deleteLater` always connects to the built-in `finished` (no-arg, fires when `run()` truly returns), never to a custom signal.

Active workers are held in `MainWindow._live_workers: List[QThread]`. The list is `append`-ed at start and `remove`-d via the built-in `finished` signal. Without this, Python GC reclaims the old worker when `self.preview_worker` is reassigned and triggers `QThread: Destroyed while thread is still running` → SIGABRT. Do not just store the worker in a single `self.preview_worker` attribute — keep it in `_live_workers` until the thread is truly finished.

PIL `Image` objects **must not cross the worker/main thread boundary**. `PreviewWorker.loaded` emits `List[(rgb_bytes, w, h)]`; the main thread re-builds `Image.frombytes(...)`. Crossing PIL Image objects directly caused fatal aborts (libjpeg/libpng + PIL thread-locals are not reentrant in this environment).

`QImage(bytes, ...)` does not copy — always call `.copy()` before `QPixmap.fromImage(...)`. Otherwise the buffer is GC'd and the pixmap shows garbage or crashes.

### Crash logging

`main.py` enables `faulthandler` and a `sys.excepthook` that write to `~/Library/Logs/A3转A4试卷转换/crash.log`. PyInstaller `--windowed` swallows stderr, so this is the only way to see what killed a packaged build. Read this file first when debugging user-reported crashes.

### Algorithm notes that matter

- `detect_divider` has two paths: dark fold (scanned book center) vs blank gap (printed two-column A3). For the blank-gap case, it finds the **midpoint of the longest near-white run**, not a single weighted argmin — earlier weighted-argmin gave equal scores to every column inside a wide white band and consistently shifted the split 30+ px off-center. If you "simplify" this, you reintroduce the bug where page-N right-column content leaks into page-(N+1).
- `auto_crop` has a `min_dark_ratio=0.005` guard. Pages that are nearly blank (only a page-footer like "第 8页 共 8页" plus a stray scanner speck) bypass cropping. Without the guard, `build_a4_pdf`'s isotropic scaling blew the page footer up to fill the whole A4 page.
- `split_page` clamps `divider_x ± gap` to `[1, w-1]` and falls back to the geometric center when the clamped left/right collide. Required because `detect_divider` can legitimately return values near 0 or near `w` on degenerate input.
- `load_pdf_pages` catches **only** `PDFInfoNotInstalledError / PDFPageCountError / FileNotFoundError` from `pdf2image`, then falls back to PyMuPDF with `alpha=False`. Wrapping the fallback in a bare `except` (the original code) hid corrupted-PDF / permission errors and gave users misleading "poppler not installed" symptoms.

## Packaging

- `A3转A4试卷转换.spec` is the single source of truth for PyInstaller config. `build.sh` runs `pyinstaller --noconfirm <spec>`. **Do not** add `rm *.spec` back to `build.sh` (the previous version did, silently wiping any spec tuning).
- `strip=False` everywhere — macOS strip corrupts PIL `_imaging` and numpy extensions' code signature.
- `splitter_core.py` is **not** in `datas` — PyInstaller picks it up automatically through the `import` graph; adding it as data duplicated it on disk and risked two-copy drift.
- The `excludes` list (scipy/pandas/matplotlib/torch/...) is what dropped the bundle from 487 MB to 217 MB. Don't blanket `--collect-all numpy` — it drags scipy/pyarrow/pandas back in.

## Backups

`bug/backup_<timestamp>/` holds known-good snapshots of `main.py / splitter_core.py / build.sh / .spec` from each major refactor. Use these to bisect regressions instead of git (there is no git history).
