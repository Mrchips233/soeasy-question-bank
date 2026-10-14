"""本地采集调试日志；日志失败不会替换采集异常，也不会额外截屏或点击。"""
from __future__ import annotations

import difflib
import hashlib
import importlib.metadata
import json
import platform
import re
import sys
import threading
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from uuid import uuid4


def text_difference(expected: str, actual: str) -> list[dict]:
    """记录每个不同位置和字符码点，区分形似标点及不可见字符。"""
    changes = []
    for operation, a1, a2, b1, b2 in difflib.SequenceMatcher(
            None, expected, actual, autojunk=False).get_opcodes():
        if operation != "equal":
            left, right = expected[a1:a2], actual[b1:b2]
            changes.append({"operation": operation, "expected_span": [a1, a2],
                            "actual_span": [b1, b2], "expected": left, "actual": right,
                            "expected_codepoints": [f"U+{ord(char):04X}" for char in left],
                            "actual_codepoints": [f"U+{ord(char):04X}" for char in right]})
    return changes


class DebugLog:
    def __init__(self, output_dir: Path, settings: dict | None = None, warning=None):
        self.directory = Path(output_dir) / "logs" / f"{datetime.now():%Y%m%d_%H%M%S_%f}_{uuid4().hex[:8]}"
        self.path = self.directory / "debug.log"
        self.events_path = self.directory / "events.jsonl"
        self.warning = warning or (lambda message: None)
        self._warned = False
        self._lock = threading.RLock()
        self.frame_count = 0
        try:
            self.directory.mkdir(parents=True, exist_ok=False)
            versions = {}
            for package in ("rapidocr", "onnxruntime", "numpy", "opencv-python-headless",
                            "Pillow", "python-docx"):
                try:
                    versions[package] = importlib.metadata.version(package)
                except importlib.metadata.PackageNotFoundError:
                    versions[package] = "unknown"
            sources = {}
            for name in ("question_bank.py", "app.py", "debug_log.py", "ocr_regions.py",
                         "ocr_cache.py", "ocr_backend.py", "feedback_capture.py", "models/manifest.json"):
                source = Path(__file__).resolve().parent / name
                if source.exists():
                    sources[name] = hashlib.sha256(source.read_bytes()).hexdigest()
            self.event("session_start", "开始本地调试日志", settings=settings or {},
                       python=sys.version, executable=sys.executable, platform=platform.platform(),
                       versions=versions, source_sha256=sources)
        except Exception as exc:
            self._failure(exc)

    def _failure(self, exc: Exception) -> None:
        if not self._warned:
            self._warned = True
            try:
                self.warning(f"调试日志写入失败：{exc}。采集状态校验仍然启用。")
            except Exception:
                pass

    def event(self, event: str, message: str = "", **details) -> None:
        try:
            record = {"time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                      "event": event, "message": str(message), **details}
            serialized = json.dumps(record, ensure_ascii=False)
            with self._lock:
                # 每次事件立即关闭文件，异常退出前的日志也已经落盘。
                with self.events_path.open("a", encoding="utf-8") as handle:
                    handle.write(serialized + "\n")
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(f"{record['time']} [{event}] {message}\n")
                    # OCR 所有行放在 events.jsonl，debug.log 保持便于阅读。
                    if details and event != "frame":
                        handle.write(json.dumps(details, ensure_ascii=False, indent=2) + "\n")
        except Exception as exc:
            self._failure(exc)

    def frame(self, frame, stage: str, number: int | None = None, attempt: int | None = None) -> int:
        try:
            with self._lock:
                self.frame_count += 1
                frame_id = self.frame_count
                frame.debug_id = frame_id
            self.event("frame", f"画面 {frame_id}: {stage}", frame_id=frame_id, stage=stage,
                       number=number, attempt=attempt, width=frame.width, height=frame.height,
                       ocr=[asdict(line) for line in frame.lines],
                       ocr_passes=getattr(frame, "ocr_passes", []),
                       refinement_error=getattr(frame, "refinement_error", ""),
                       progress_occlusion=getattr(frame, "progress_occlusion", {}),
                       ocr_stats=getattr(frame, "ocr_stats", {}))
            return frame_id
        except Exception as exc:
            self._failure(exc)
            return 0

    def capture(self, frame, stage: str) -> str | None:
        if frame is None:
            return None
        try:
            frame_id = getattr(frame, "debug_id", 0)
            safe_stage = re.sub(r"[^a-zA-Z0-9_-]", "_", stage)
            path = self.directory / "frames" / f"{frame_id:05d}_{safe_stage}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            frame.image.save(path)
            path.with_suffix(".ocr.json").write_text(
                json.dumps([asdict(line) for line in frame.lines], ensure_ascii=False, indent=2),
                encoding="utf-8")
            self.event("snapshot", f"保存调试截图：{path.name}", frame_id=frame_id,
                       stage=stage, path=str(path))
            return str(path)
        except Exception as exc:
            self._failure(exc)
            return None
