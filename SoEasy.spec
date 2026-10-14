# -*- mode: python ; coding: utf-8 -*-
import os
import json
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, copy_metadata

source = Path(SPECPATH)
import sys
sys.path.insert(0, str(source))
from packaging_support import public_binaries
datas = [(str(source / "models"), "models")]
datas += collect_data_files("rapidocr", excludes=["models/**"])
datas += collect_data_files("docx")
for package in ("rapidocr", "onnxruntime", "numpy", "opencv-python-headless", "Pillow", "python-docx"):
    datas += copy_metadata(package)
for filename in ("public_question.png", "public_answer.png"):
    datas.append((str(source / "public_test_assets" / filename), "_selftest"))

binaries = collect_dynamic_libs("onnxruntime")
# The public build uses Microsoft's runtime installed by the user. Do not
# copy System32/CPython/wheel runtime DLLs into this distribution.

a = Analysis(
    [str(source / "portable_app.py")],
    pathex=[str(source)],
    binaries=binaries,
    datas=datas,
    hiddenimports=["rapidocr.main", "rapidocr.inference_engine.onnxruntime",
                   "mss.windows", "pyautogui._pyautogui_win", "pygetwindow._pygetwindow_win",
                   "PIL.ImageTk"],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["torch", "tensorflow", "paddle", "openvino", "tensorrt", "pytest", "IPython",
              "PIL.AvifImagePlugin", "PIL._avif", "lxml.objectify",
              "mouseinfo", "portable_selftest"],
    noarchive=False,
)
# Screenshot OCR uses image processing, not OpenCV's optional video backend.
# Python ORT uses the pybind11 runtime plus providers_shared; the standalone
# C API onnxruntime.dll is not an import dependency of either module.
# Filter after Analysis because package hooks collect these DLLs themselves.
a.binaries = [entry for entry in a.binaries
              if not any(Path(name).name.lower().startswith("opencv_videoio_ffmpeg")
                         or Path(name).name.lower() == "onnxruntime.dll"
                         for name in entry[:2])]
a.binaries, import_modifications = public_binaries(a.binaries, Path(workpath) / "public_dlls")
import_record = Path(workpath) / "GEOS_IMPORT_MODIFICATIONS.json"
import_record.write_text(json.dumps(import_modifications, indent=2), encoding="utf-8")
a.datas.append(("licenses/GEOS_IMPORT_MODIFICATIONS.json", str(import_record), "DATA"))
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name="SoEasy题库采集器", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False, disable_windowed_traceback=False,
    icon=str(source / "app.ico"), version=str(source / "windows_version.txt"),
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="SoEasy题库采集器")
