"""Bundled PP-OCRv5 CPU backend, preserving the collector's legacy OCR API."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MODEL_DIRECTORY = Path(__file__).resolve().parent / "models"


class OCRBackendError(RuntimeError):
    """Installation or model-integrity error detected before collection begins."""


def _load_verified_models(directory: Path) -> tuple[dict, dict[str, Path], str]:
    manifest_path = directory / "manifest.json"
    try:
        content = manifest_path.read_bytes()
        manifest = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise OCRBackendError("缺少或损坏 OCR 模型清单，请重新解压完整程序包。") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise OCRBackendError("OCR 模型清单版本不支持，请使用完整的同一版本程序包。")
    entries = manifest.get("models")
    if not isinstance(entries, list) or len(entries) != 2:
        raise OCRBackendError("OCR 模型清单须包含检测和识别模型。")
    paths: dict[str, Path] = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("task") not in {"det", "rec"}:
            raise OCRBackendError("OCR 模型清单内容不正确。")
        task, filename = entry["task"], entry.get("filename")
        if (task in paths or not isinstance(filename, str)
                or Path(filename).name != filename or not filename.endswith(".onnx")):
            raise OCRBackendError("OCR 模型清单的文件名或模型类型不正确。")
        path = directory / filename
        try:
            if path.resolve().parent != directory.resolve():
                raise OSError("model is outside the bundled model directory")
            size = path.stat().st_size
            if size != entry.get("size_bytes"):
                raise OCRBackendError(f"OCR 模型 {filename} 不完整，请重新解压完整程序包。")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    digest.update(block)
        except OSError as exc:
            raise OCRBackendError(f"缺少 OCR 模型 {filename}，请重新解压完整程序包。") from exc
        if digest.hexdigest() != entry.get("sha256"):
            raise OCRBackendError(f"OCR 模型 {filename} 校验失败，请重新解压完整程序包。")
        paths[task] = path.resolve()
    if set(paths) != {"det", "rec"}:
        raise OCRBackendError("OCR 模型清单须包含检测和识别模型。")
    return manifest, paths, hashlib.sha256(content).hexdigest()


@dataclass
class OCRBackend:
    engine: Any
    metadata: dict

    def __call__(self, image):
        """Return (legacy [box, text, confidence] triples, timing), using BGR input."""
        result = self.engine(image)
        if result.boxes is None or result.txts is None or result.scores is None:
            return None, result.elapse_list
        if not (len(result.boxes) == len(result.txts) == len(result.scores)):
            raise OCRBackendError("OCR 返回的文字、坐标和置信度数量不一致。")
        triples = [[box.tolist(), str(text), float(score)]
                   for box, text, score in zip(result.boxes, result.txts, result.scores)]
        return triples, result.elapse_list


def build_ocr_backend() -> OCRBackend:
    """Load verified bundled models. No model downloads or API keys are used."""
    manifest, paths, manifest_hash = _load_verified_models(MODEL_DIRECTORY)
    try:
        from rapidocr import (RapidOCR, EngineType, LangDet, LangRec, ModelType,
                              OCRVersion)
    except ImportError as exc:
        raise OCRBackendError("OCR 依赖尚未安装，请运行 install.cmd、install.ps1 或 uv sync --locked。") from exc
    params = {
        "Global.use_cls": False,
        "Global.log_level": "error",
        "Global.model_root_dir": str(MODEL_DIRECTORY),
        "Det.engine_type": EngineType.ONNXRUNTIME,
        "Det.ocr_version": OCRVersion.PPOCRV5,
        "Det.model_type": ModelType.MOBILE,
        "Det.lang_type": LangDet.CH,
        "Det.model_path": str(paths["det"]),
        # Flat stem crops must not be enlarged to min-side 736 pixels.
        "Det.limit_type": "max",
        "Det.limit_side_len": 960,
        "Rec.engine_type": EngineType.ONNXRUNTIME,
        "Rec.ocr_version": OCRVersion.PPOCRV5,
        "Rec.model_type": ModelType.SERVER,
        "Rec.lang_type": LangRec.CH,
        "Rec.model_path": str(paths["rec"]),
        "EngineConfig.onnxruntime.intra_op_num_threads": 2,
        "EngineConfig.onnxruntime.inter_op_num_threads": 2,
        "EngineConfig.onnxruntime.use_cuda": False,
        "EngineConfig.onnxruntime.use_dml": False,
    }
    try:
        engine = RapidOCR(params=params)
        # rapidocr 3.10 loads lazily; eagerly initialize both verified local sessions
        # so an incompatible model stops before any selection or navigation.
        engine._load_det_model()
        engine._load_rec_model()
    except Exception as exc:
        raise OCRBackendError(f"本地 OCR 模型加载失败：{exc}") from exc
    try:
        version = importlib.metadata.version("rapidocr")
    except importlib.metadata.PackageNotFoundError:
        version = manifest["rapidocr_version"]
    metadata = {
        "backend": "RapidOCR / ONNX Runtime CPU",
        "version": version,
        "det": "PP-OCRv5 mobile",
        "rec": "PP-OCRv5 server Chinese",
        "config": dict(manifest["config"]),
        "manifest_sha256": manifest_hash,
        "models": [{"filename": item["filename"], "sha256": item["sha256"]}
                   for item in manifest["models"]],
    }
    return OCRBackend(engine, metadata)
