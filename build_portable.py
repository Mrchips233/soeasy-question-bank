"""构建并验证 Windows 10/11 x64 免安装版，不打包个人配置与题库。"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile


SOURCE = Path(__file__).resolve().parent
RELEASE_VERSION = "1.2.2-beta.2"


def source_hashes() -> dict[str, str]:
    files = [*(path for path in SOURCE.glob("*.py") if path.name != "portable_selftest.py"),
             SOURCE / "SoEasy.spec", SOURCE / "windows_version.txt",
             SOURCE / "models" / "manifest.json", SOURCE / "README.md", SOURCE / "LICENSE",
             SOURCE / "THIRD_PARTY_NOTICES.md", SOURCE / "THIRD_PARTY_SOURCES.md", SOURCE / "app.ico",
             *SOURCE.glob("public_test_assets/*.png"), *SOURCE.glob("third_party_licenses/**/*")]
    files = [path for path in files if path.is_file()]
    return {path.relative_to(SOURCE).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(files)}


def build() -> None:
    if os.name != "nt" or sys.maxsize <= 2**32:
        raise RuntimeError("请在 Windows 64 位 Python 中构建。")
    root = SOURCE / ".work" / "build" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    root.mkdir(parents=True, exist_ok=False)
    artifacts = SOURCE / ".work" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    hashes = source_hashes()
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--workpath", str(root / "work"),
                    "--distpath", str(root / "dist"), str(SOURCE / "SoEasy.spec")], cwd=SOURCE, check=True)
    folder = root / "dist" / "SoEasy题库采集器"
    executable = folder / "SoEasy题库采集器.exe"
    from packaging_support import is_windows_runtime_copy
    assert not [path for path in folder.rglob("*") if path.is_file() and is_windows_runtime_copy(path.name)]
    excluded_files = [path for path in folder.rglob("*") if path.is_file()
                      and (path.name.lower().startswith("opencv_videoio_ffmpeg")
                           or path.name.lower() == "onnxruntime.dll"
                           or path.name.lower().startswith(("_avif.", "objectify.")))]
    assert not excluded_files, excluded_files
    instructions = (
        "SoEasy 题库采集器 · Windows 10/11 64位免安装版\n\n"
        "1. 将整个压缩包解压到一个可以写入的文件夹，例如桌面或D盘。\n"
        "2. 首次运行先从微软官网安装 Microsoft Visual C++ v14 x64 运行库。\n"
        "   官方页面：https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist\n"
        "   然后双击“ SoEasy题库采集器.exe ”启动，无需安装 Python 或 uv。\n"
        "3. 打开练习小程序，完整显示题干、选项和底部按钮，再框选小程序区域。\n"
        "4. 选择与小程序相同的题型：单选题、判断题或多选题。判断题自动使用2个选项。\n"
        "5. 先仅识别当前题并试采集3题，检查Word后再采集到最后。\n\n"
        "OCR模型已包含，运行库安装完成后的题目识别无需联网。配置保存在EXE旁的 config.json，题库默认保存在 output。\n"
        f"当前版本：{RELEASE_VERSION}（精简Beta版）。建议解压到独立文件夹试用。\n"
        "采集时可通过实时OCR文字窗口查看识别结果。\n"
        "单选保存到output，Word名为单选题库.docx；判断保存到output/判断题，Word名为判断题库.docx；\n"
        "多选保存到output/多选题，Word名为多选题库.docx。三类JSON和截图分别保存，不会按同题号互相覆盖。\n"
        "JSON用于继续采集和重新导出Word，请保留；修正错误可用“重采当前题”。\n"
        "换电脑或移动/缩放小程序窗口后需要重新框选。\n"
        "请保留整个文件夹，_internal 内含运行环境及模型，不能只复制EXE。\n"
        "按Esc或把鼠标移到屏幕左上角可停止。\n"
    )
    (folder / "使用说明.txt").write_text(instructions, encoding="utf-8-sig")
    shutil.copy2(SOURCE / "README.md", folder / "详细说明.md")
    shutil.copy2(SOURCE / "LICENSE", folder / "LICENSE")
    shutil.copy2(SOURCE / "THIRD_PARTY_NOTICES.md", folder / "THIRD_PARTY_NOTICES.md")
    shutil.copy2(SOURCE / "THIRD_PARTY_SOURCES.md", folder / "THIRD_PARTY_SOURCES.md")
    licenses = folder / "许可证"
    licenses.mkdir()
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        shutil.copy2(python_license, licenses / "Python-LICENSE.txt")
    shutil.copytree(SOURCE / "third_party_licenses", licenses / "third_party")
    environment = os.environ.copy()
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"):
        environment.pop(name, None)
    environment["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
    smoke = root / "verification"
    # 从与源码/EXE无关的工作目录启动，模拟不依赖 Python/uv 的用户机器。
    subprocess.run([str(executable), "--self-test", str(smoke)], cwd=root,
                   env=environment, check=True, timeout=180)
    report = json.loads((smoke / "result.json").read_text(encoding="utf-8"))
    assert report["success"], report
    assert report["checks"]["current_collector_features"]["next_dropped_click_recovered"], report
    assert report["checks"]["question_type_modes"]["three_types_verified"], report
    assert report["checks"]["question_type_modes"]["separate_json_word_files"], report
    assert report["checks"]["hidden_gui"]["question_type_controls_verified"], report
    assert report["checks"]["hidden_gui"]["type_switching_verified"], report
    assert report["fixtures_are_original"], report
    assert report["checks"]["real_synthetic_ocr"]["correct_labels"] == ["B"], report
    assert report["checks"]["real_synthetic_ocr"]["real_local_ocr_verified"], report
    assert source_hashes() == hashes, "构建过程中源码变化，请重新打包。"
    assert not (folder / "config.json").exists()
    assert not (folder / "output").exists()
    manifest = {"version": RELEASE_VERSION, "built_at": datetime.now().astimezone().isoformat(),
                "target": "Windows 10/11 x64", "source_sha256": hashes,
                "prerelease": True,
                "public_release": True,
                "microsoft_runtime_prerequisite": "Microsoft Visual C++ v14 x64, installed from Microsoft",
                "public_fixtures_only": True,
                "package_optimization": {"removed": ["OpenCV optional FFmpeg video DLL",
                                                       "Pillow AVIF plugin",
                                                       "standalone ONNX Runtime C API DLL",
                                                       "lxml.objectify"],
                                         "ocr_models_changed": False},
                "features": {"live_ocr_window": True, "repeated_body_confirmation": False,
                             "reveal_rounds": 2, "reveal_observation_seconds": .45,
                             "reveal_sample_interval_seconds": .025, "next_max_clicks": 4,
                             "question_types": ["single", "true_false", "multiple"],
                             "output_layout": {"single": ".", "true_false": "判断题", "multiple": "多选题"},
                             "word_filenames": {"single": "单选题库.docx", "true_false": "判断题库.docx",
                                                "multiple": "多选题库.docx"},
                             "multiple_preselection_normalization": True,
                             "numeric_page_validation": True,
                             "occluded_progress_detection": True},
                "frozen_selftest_passed": True}
    (folder / "版本信息.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    package_files = [{"path": path.relative_to(folder).as_posix(),
                      "size": path.stat().st_size,
                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                     for path in sorted(folder.rglob("*")) if path.is_file()]
    (folder / "package_files.json").write_text(
        json.dumps(package_files, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    files = sorted(path for path in folder.rglob("*") if path.is_file())
    # A beta must not replace the stable archive or its build metadata.
    archive = artifacts / f"01-SoEasy-v{RELEASE_VERSION}-Windows-x64.zip"
    temporary_archive = root / archive.name
    with zipfile.ZipFile(temporary_archive, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=9) as packed:
        for path in files:
            packed.write(path, folder.name + "/" + path.relative_to(folder).as_posix())
    with zipfile.ZipFile(temporary_archive) as packed:
        assert packed.testzip() is None
        assert len(packed.namelist()) == len(files)
        for path in files:
            assert packed.read(folder.name + "/" + path.relative_to(folder).as_posix()) == path.read_bytes()
    os.replace(temporary_archive, archive)
    result = {"folder": str(folder), "exe": str(executable), "zip": str(archive),
              "zip_bytes": archive.stat().st_size, "zip_files": len(files),
              "unpacked_bytes": sum(path.stat().st_size for path in files),
              "zip_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
              "selftest": str(smoke / "result.json"), "target": "Windows 10/11 x64",
              "version": RELEASE_VERSION, "prerelease": True,
              "source": str(SOURCE), "source_sha256": hashes,
              "no_user_config_or_output_in_package": True}
    (artifacts / f"soeasy_portable_build_result_{RELEASE_VERSION}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    build()
