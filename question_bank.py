"""SoEasy 截图题库采集器。OCR、状态校验、断点保存及 Word 导出。"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
import unicodedata
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Callable
from uuid import uuid4

from debug_log import DebugLog, text_difference
from feedback_capture import FeedbackCandidate, find_feedback_candidate
from ocr_cache import ExactOCRCache
from ocr_regions import (find_answer_dialog, covered_stem_rows,
                         find_correct_toast, covered_toast_stem_rows)

# 冻结包的资源位于 _internal，用户配置和题库仍放在 EXE 旁边。
BASE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
REVEAL_CONFIRMATION_ROUNDS = 2
REVEAL_OBSERVATION_SECONDS = .45
REVEAL_SAMPLE_INTERVAL_SECONDS = .025
NEXT_MAX_CLICKS = 4
QUESTION_TYPE_NAMES = {"single": "单选题", "multiple": "多选题", "true_false": "判断题"}
WORD_FILENAMES = {"single": "单选题库.docx", "multiple": "多选题库.docx", "true_false": "判断题库.docx"}


def question_output_dir(output: Path, question_type: str = "single") -> Path:
    if question_type not in QUESTION_TYPE_NAMES:
        raise ValueError(f"不支持的题型：{question_type}")
    # 原单选 JSON 保留原路径；新增题型分别断点保存，不能覆盖相同题号。
    return Path(output) if question_type == "single" else Path(output) / QUESTION_TYPE_NAMES[question_type]


class RecognitionError(RuntimeError):
    """界面无法可靠识别，停止操作以免错配。"""


class Stopped(RuntimeError):
    pass


def compact(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


CHINESE_QUOTE_RE = re.compile(r"[\"'“”‘’「」『』]([\u4e00-\u9fff][\u4e00-\u9fff，,。！？!?；;：:、（）()…\s]*)[\"'“”‘’「」『』]")


def ocr_text_identity(text: str) -> str:
    """核对文字时忽略普通标点；原文用于保存，数字和数学符号仍逐字比较。"""
    source = compact(text)
    result = []
    operators = set("+-−*/÷×=<>≤≥≠±%‰^")
    for index, character in enumerate(source):
        numeric_separator = (character in ".:"
                             and index > 0 and index + 1 < len(source)
                             and source[index - 1].isdigit() and source[index + 1].isdigit())
        if (not unicodedata.category(character).startswith("P")
                or character in operators or numeric_separator):
            result.append(character)
    return "".join(result)


def question_content_key(stem: str, options: dict[str, str]) -> tuple:
    return (ocr_text_identity(stem),
            tuple((label, ocr_text_identity(text)) for label, text in options.items()))


def enable_dpi_awareness() -> None:
    if os.name == "nt":
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except (AttributeError, OSError):
                pass


@dataclass
class OCRLine:
    text: str
    confidence: float
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2)


@dataclass
class Frame:
    image: object
    lines: list[OCRLine]
    ocr_passes: list[dict] = field(default_factory=list)
    refinement_error: str = ""
    ocr_stats: dict = field(default_factory=dict)
    progress_occlusion: dict = field(default_factory=dict)

    @property
    def width(self) -> int:
        return self.image.width

    @property
    def height(self) -> int:
        return self.image.height


@dataclass
class Question:
    number: int
    total: int
    stem: str
    options: dict[str, str]
    bank_name: str = "练习题库"
    confidence: float = 1.0
    option_points: dict[str, tuple[float, float]] = field(default_factory=dict)
    question_type: str = "single"

    @property
    def signature(self) -> str:
        content = [self.number, compact(self.stem),
                   [(key, compact(value)) for key, value in self.options.items()]]
        if self.question_type != "single":
            content.append(self.question_type)
        return hashlib.sha256(json.dumps(content, ensure_ascii=False).encode()).hexdigest()

    @property
    def identity(self) -> tuple:
        # 原始 signature 与正文继续保存；界面一致性不受中文引号的 OCR 字形波动影响。
        return (self.number, self.total, compact(self.bank_name), self.question_type,
                question_content_key(self.stem, self.options))


class OCREngine:
    def __init__(self):
        from ocr_backend import build_ocr_backend
        self.engine = build_ocr_backend()
        self.cache = ExactOCRCache()
        self._model_calls = 0
        self._model_seconds = 0.0

    def read(self, image, scale: float | None = None) -> Frame:
        import numpy as np
        from PIL import Image
        # 小窗口放大后识别，坐标再换回截图像素，避免 DPI 引起点击偏移。
        whole_frame = scale is None
        scale = scale or max(1.0, min(2.0, 780 / image.width))
        started = time.perf_counter()
        if not hasattr(self, "cache"):
            self.cache = ExactOCRCache()
        calls_before = getattr(self, "_model_calls", 0)
        seconds_before = getattr(self, "_model_seconds", 0.0)
        stats_before = self.cache.stats()
        payload = self.cache.get(image, scale, whole_frame)
        cache_hit = payload is not None
        if cache_hit:
            # 文字结果可复用，image 始终是本次新截图；日志ID与可变列表也不复用。
            frame = Frame(image, **payload)
        elif whole_frame:
            # 原始整图与分区OCR分别缓存，像素不变时无需重复运行模型。
            frame = self.read(image, scale=scale)
            frame = self.refine_options_frame(self.refine_question_frame(frame))
            self.cache.put(image, scale, whole_frame, frame)
        else:
            resized = image.resize((round(image.width * scale), round(image.height * scale)),
                                   Image.Resampling.LANCZOS) if scale != 1 else image
            model_started = time.perf_counter()
            try:
                raw, _ = self.engine(np.asarray(resized.convert("RGB"))[:, :, ::-1].copy())
            finally:
                self._model_calls = getattr(self, "_model_calls", 0) + 1
                self._model_seconds = (getattr(self, "_model_seconds", 0.0)
                                       + time.perf_counter() - model_started)
            lines = []
            for box, text, confidence in raw or []:
                xs, ys = zip(*box)
                lines.append(OCRLine(str(text).strip(), float(confidence), min(xs) / scale,
                                     min(ys) / scale, max(xs) / scale, max(ys) / scale))
            frame = Frame(image, merge_rows(lines))
            self.cache.put(image, scale, whole_frame, frame)
        stats_after = self.cache.stats()
        frame.ocr_stats = {
            "seconds": round(time.perf_counter() - started, 6), "cached": cache_hit,
            "model_calls": getattr(self, "_model_calls", 0) - calls_before,
            "model_seconds": round(getattr(self, "_model_seconds", 0.0) - seconds_before, 6),
            "cache_hits": stats_after["hits"] - stats_before["hits"],
            "cache_misses": stats_after["misses"] - stats_before["misses"],
            "scale": scale, "whole_frame": whole_frame,
            "backend": getattr(self.engine, "metadata", {})}
        return frame

    def refine_options_frame(self, frame: Frame) -> Frame:
        """低置信度选项可选重读；只按题号数字绑定，增强失败保留原结果。"""
        try:
            original = parse_question(frame, expected_options=0)
        except RecognitionError:
            return frame
        _, _, stem_rows = stem_parts(frame)
        toolbar_top = min(line.y1 for line in frame.lines
                          if any(text in compact(line.text) for text in
                                 ("下一题", "查看答案", "完成练习")))
        rows = [line for line in frame.lines
                if stem_rows[-1].y2 < line.center[1] < toolbar_top
                and not is_counter_line(frame, line) and not is_progress_decoration(frame, line)
                and compact(line.text) not in UI_TEXT]
        if not rows or all(line.confidence >= .93 for line in rows):
            return frame
        heights = sorted(line.y2 - line.y1 for line in rows)
        height = heights[len(heights) // 2]
        body = stem_region_box(frame)
        box = (max(0, int(min(line.x1 for line in rows) - .6 * height)),
               max(int(stem_rows[-1].y2 + 2), int(min(line.y1 for line in rows) - .75 * height)),
               min(frame.width, max(int(max(line.x2 for line in rows) + height + 1),
                                    body[2] if body else frame.width)),
               min(int(toolbar_top - 2), int(max(line.y2 for line in rows) + .35 * height + 1)))
        if box[2] <= box[0] or box[3] <= box[1]:
            return frame
        from PIL import ImageEnhance, ImageOps
        crop = frame.image.crop(box)
        old_ids = {id(line) for line in rows}
        kept = [line for line in frame.lines if id(line) not in old_ids]
        passes = [*frame.ocr_passes]

        def candidate(method):
            image = (ImageEnhance.Contrast(ImageOps.grayscale(crop)).enhance(1.5).convert("RGB")
                     if method == "options_contrast" else crop)
            piece = self.read(image, scale=3)
            new_rows = [replace(line, x1=line.x1 + box[0], x2=line.x2 + box[0],
                                y1=line.y1 + box[1], y2=line.y2 + box[1]) for line in piece.lines]
            passes.append({"method": method, "scale": 3, "box": list(box),
                           "ocr": [asdict(line) for line in new_rows]})
            refined = Frame(frame.image, sorted([*kept, *new_rows],
                            key=lambda line: (line.center[1], line.x1)), ocr_passes=passes)
            fresh = parse_question(refined, expected_options=len(original.options))
            if (fresh.number, fresh.total) != (original.number, original.total):
                raise RecognitionError("选项分区识别改变了题号或总题数，保留原识别结果。")
            passes[-1].update(validation="question_number", number=fresh.number, total=fresh.total)
            return refined, fresh

        try:
            refined, fresh = candidate("options_plain")
            return refined
        except Exception as exc:
            passes.append({"result": "refinement_fallback", "region": "options",
                           "reason": str(exc), "validation": "question_number"})
            frame.ocr_passes = passes
            frame.refinement_error = ""
            return frame

    def refine_question_frame(self, frame: Frame) -> Frame:
        """可选题干增强只核对编号；文字差异或增强失败不能中断完整原题。"""
        if (has_correct_feedback(frame)
                or any("正确答案" in compact(line.text) or compact(line.text) == "知道了"
                       for line in frame.lines)):
            return frame
        try:
            original = parse_question(frame, expected_options=0)
        except RecognitionError:
            return frame
        box = stem_region_box(frame)
        if box is None:
            return frame
        from PIL import ImageEnhance, ImageOps
        original_crop = frame.image.crop(box)
        passes = [{"method": "full_frame", "ocr": [asdict(line) for line in frame.lines]}]

        def candidate(method: str, scale: float) -> tuple[Frame, Question]:
            crop = (ImageEnhance.Contrast(ImageOps.grayscale(original_crop)).enhance(1.5).convert("RGB")
                    if method == "contrast" else original_crop)
            piece = self.read(crop, scale=scale)
            refined = replace_stem_region(frame, piece, box)
            passes.append({"method": method, "scale": scale, "box": list(box),
                           "ocr": [asdict(line) for line in refined.lines]})
            fresh = parse_question(refined, expected_options=len(original.options))
            passes[-1].update(stem=fresh.stem, number=fresh.number,
                              total=fresh.total, validation="question_number")
            if (fresh.number, fresh.total) != (original.number, original.total):
                raise RecognitionError("题干分区识别改变了题号或总题数，保留原识别结果。")
            return refined, fresh

        try:
            refined, fresh = candidate("contrast", 2)
            if (refinement_drops_blank_punctuation(original.stem, fresh.stem)
                    or refinement_drops_terminal_quote(original.stem, fresh.stem)):
                # 内容核对忽略标点，但保存时保留原图已有的完整标点。
                passes.append({"result": "keep_original_structural_punctuation"})
                frame.ocr_passes = passes
                return frame
            refined.ocr_passes = passes
            return refined
        except Exception as exc:
            passes.append({"result": "refinement_fallback", "region": "stem",
                           "reason": str(exc), "validation": "question_number"})
            frame.ocr_passes = passes
            frame.refinement_error = ""
            return frame


def merge_rows(lines: list[OCRLine]) -> list[OCRLine]:
    """合并同一行被 OCR 分开的答案字母或选项标记，保留几何信息。"""
    rows: list[list[OCRLine]] = []
    for line in sorted(lines, key=lambda item: (item.center[1], item.x1)):
        if rows:
            row = rows[-1]
            center_y = sum(item.center[1] for item in row) / len(row)
            tolerance = min(line.y2 - line.y1, row[0].y2 - row[0].y1) * 0.40
            if abs(line.center[1] - center_y) <= tolerance:
                row.append(line)
                continue
        rows.append([line])
    result = []
    for row in rows:
        row.sort(key=lambda item: item.x1)
        groups: list[list[OCRLine]] = []
        for item in row:
            if groups and item.x1 - groups[-1][-1].x2 <= 0.75 * (item.y2 - item.y1):
                groups[-1].append(item)
            else:
                groups.append([item])
        for group in groups:
            result.append(OCRLine(" ".join(item.text for item in group),
                                  min(item.confidence for item in group),
                                  min(item.x1 for item in group), min(item.y1 for item in group),
                                  max(item.x2 for item in group), max(item.y2 for item in group)))
    return result


def find_button(frame: Frame, label: str) -> OCRLine:
    matches = [line for line in frame.lines if compact(line.text) == compact(label)]
    # 底部五个按钮可能被分行合并，必要时对该区域单独再 OCR，不猜坐标。
    if len(matches) != 1:
        raise RecognitionError(f"未能唯一定位“{label}”按钮，请完整显示小程序及底部按钮。")
    if matches[0].confidence < 0.60:
        raise RecognitionError(f"“{label}”按钮识别置信度过低。")
    return matches[0]


# 行首独立选项字母是边界；标点可有可无，不能要求 OCR 一定读到点号。
# 有空格时也支持英文正文；无空格时支持中文、数字及开引号/括号正文。
# 保留正文标点和算式，不把 C++、AB 等英文/运算前缀拆成选项。
OPTION_LABEL_CHARS = "A-Ha-hＡ-Ｈａ-ｈ"
OPTION_SEPARATOR_CHARS = ".．、,，:：;；。·•‧・･●◦!?！？)）"
OPTION_EXPLICIT_PREFIX_RE = re.compile(
    rf"^\s*[{OPTION_LABEL_CHARS}]\s*[{re.escape(OPTION_SEPARATOR_CHARS)}]")
OPTION_RE = re.compile(
    rf"^\s*(?:[✓✔√☑✅]\s*)?([{OPTION_LABEL_CHARS}])"
    rf"(?:\s*[{re.escape(OPTION_SEPARATOR_CHARS)}]\s*|\s+(?=[A-Za-zＡ-Ｚａ-ｚ\d\u4e00-\u9fff\"'“‘「『（(《〈【\[])"
    r"|\s*(?=[\d\u4e00-\u9fff\"'“‘「『（(《〈【\[]))(.*)$")
# 题号圆点可能读成不同项目符号，只在开头编号后识别，数学点运算符不参与。
QUESTION_NUMBER_EXTRA_DOTS = "•‧・･●◦"
# 英文/全角点及新增项目符号后接数字时，保留为正文以免误删数值。
NUMBER_RE = re.compile(
    r"^\s*(?:第\s*)?(\d+)\s*"
    rf"(?:题\s*[.．。·{QUESTION_NUMBER_EXTRA_DOTS}、:：)）\-]*|[.．](?!\s*\d)"
    rf"|[{QUESTION_NUMBER_EXTRA_DOTS}](?!\s*\d)|[。·、:：)）\-]"
    r"|[?？](?=\s*[\u4e00-\u9fff])|(?=[（(]))\s*(.*)$")
UI_TEXT = {"上一题", "下一题", "完成练习", "查看答案", "跳转", "知道了"}
PROGRESS_RE = re.compile(r"(\d+)\s*[/／]\s*(\d+)")


def correct_feedback_lines(frame: Frame) -> list[OCRLine]:
    matches = []
    for line in frame.lines:
        text = compact(line.text).lstrip("✓✔√☑✅/").rstrip("!。.")
        if (text in {"答题正确", "回答正确", "选项正确"}
                and frame.width * 0.2 < line.center[0] < frame.width * 0.8
                and frame.height * 0.25 < line.center[1] < frame.height * 0.75):
            matches.append(line)
    return matches


def has_correct_feedback(frame: Frame) -> bool:
    # 低置信度提示也需要等其消失，不能把它当作选项文字解析。
    return bool(correct_feedback_lines(frame))


def wrong_feedback_lines(frame: Frame) -> list[OCRLine]:
    """错误提示只在中央反馈位置识别，不匹配题干/选项里的同名字样。"""
    matches = []
    for line in frame.lines:
        text = compact(line.text).lstrip("✗✘×❌Xx/")
        text = text.rstrip("!！。.,，:：")
        if (re.fullmatch(r"(?:答题|答案|回答|选项)错误(?:[!！。.,，:：]*正确答案.*)?", text)
                and frame.width * .2 < line.center[0] < frame.width * .8
                and frame.height * .25 < line.center[1] < frame.height * .75):
            matches.append(line)
    return matches


def has_wrong_feedback(frame: Frame) -> bool:
    return bool(wrong_feedback_lines(frame)) or getattr(frame, "feedback_wrong_detected", False)


def has_answer_dialog(frame: Frame) -> bool:
    return any("正确答案" in compact(line.text) or compact(line.text) == "知道了"
               for line in frame.lines)


def is_counter_line(frame: Frame, line: OCRLine) -> bool:
    """排除题干里的分数；小程序的页码在题卡右侧。"""
    return (line.center[0] > frame.width * 0.55
            and re.fullmatch(r"[\d\s/／|IlOoGg]+", line.text.strip()) is not None)


def is_progress_decoration(frame: Frame, line: OCRLine) -> bool:
    """进度条左端孤立的小圆点不是题干，也不是待验证的文字行。"""
    if compact(line.text) not in {"●", "•", "○", "◦", "·", "|", "丨"} or line.x1 >= frame.width * .2:
        return False
    return any(is_counter_line(frame, counter) and PROGRESS_RE.fullmatch(counter.text.strip())
               and counter.y1 - 1.5 * (counter.y2 - counter.y1) <= line.center[1] <= counter.y2
               for counter in frame.lines)


def practice_header_lines(frame: Frame) -> list[OCRLine]:
    """标题须在题号上方；顶部装饰被读成选项字母时仍按标题处理。"""
    candidates = [line for line in frame.lines
                  if "练习" in line.text and line.center[1] < frame.height * .28
                  and not NUMBER_RE.match(line.text)
                  and not OPTION_EXPLICIT_PREFIX_RE.match(line.text)
                  and (re.search(r"共\s*\d+\s*题", line.text) is not None
                       or line.center[1] < frame.height * .18)]
    first_number_y = min((line.y1 for line in frame.lines
                          if NUMBER_RE.match(line.text)
                          or (line.x1 < frame.width * .3
                              and re.fullmatch(r"\d+", line.text.strip()))), default=frame.height)
    return [line for line in candidates if line.y2 < first_number_y]


def option_boundary_lines(frame: Frame) -> list[OCRLine]:
    """数字/英文正文的新标签按页码或对齐位置核对，不把题干变量当选项。"""
    header_ids = {id(line) for line in practice_header_lines(frame)}
    toolbar_top = min((line.y1 for line in frame.lines
                       if compact(line.text) in {"下一题", "查看答案", "完成练习"}),
                      default=frame.height)
    candidates = [(line, match, unicodedata.normalize("NFKC", match.group(1)).upper())
                  for line in sorted(frame.lines, key=lambda line: (line.center[1], line.x1))
                  if id(line) not in header_ids and line.y1 < toolbar_top
                  and (match := OPTION_RE.match(line.text))]
    counters = [line for line in frame.lines if is_counter_line(frame, line)
                and PROGRESS_RE.fullmatch(line.text.strip()) and line.confidence >= .60]
    result = []
    for line, match, label in candidates:
        tail = line.text[match.end(1):].lstrip()
        unmarked = bool(re.match(r"^[A-Za-zＡ-Ｚａ-ｚ\d]", tail))
        if unmarked:
            if len(counters) == 1:
                if line.y1 < counters[0].y2:
                    continue
            elif not any(other_label != label
                         and abs(other.x1 - line.x1) <= .6 * min(other.y2 - other.y1,
                                                                line.y2 - line.y1)
                         for other, _, other_label in candidates):
                continue
        # 已有中文/明确分隔符边界照常保留，重复 A 标签也不能被隐藏进题干。
        result.append(line)
    return sorted([*result, *true_false_option_lines(frame)],
                  key=lambda line: (line.center[1], line.x1))


def true_false_option_label(text: str) -> str | None:
    # 选中的圆圈/勾可能与“对”“错”一起进入 OCR，标签本身没有 A/B。
    value = compact(text).lstrip("✓✔√☑✅○●◯〇⊙◎").strip(".。:：、，,;；")
    return value if value in {"对", "错"} else None


def true_false_option_lines(frame: Frame) -> list[OCRLine]:
    toolbar_top = min((line.y1 for line in frame.lines
                       if any(text in compact(line.text) for text in ("下一题", "查看答案", "完成练习"))),
                      default=frame.height)
    headers = practice_header_lines(frame)
    counters = [line for line in frame.lines if is_counter_line(frame, line)
                and PROGRESS_RE.fullmatch(line.text.strip())]
    numbered = [line for line in frame.lines if NUMBER_RE.match(line.text)]
    top = max((line.y2 for line in counters), default=max((line.y2 for line in numbered), default=0))
    candidates = [line for line in frame.lines if top < line.y1 < toolbar_top
                  and true_false_option_label(line.text) is not None]
    if (not any("判断题" in compact(line.text) for line in headers)
            and {true_false_option_label(line.text) for line in candidates} != {"对", "错"}):
        return []
    return candidates


def detected_question_type(frame: Frame) -> str | None:
    headers = " ".join(compact(line.text) for line in practice_header_lines(frame))
    for value in ("true_false", "multiple", "single"):
        if QUESTION_TYPE_NAMES[value] in headers:
            return value
    if {true_false_option_label(line.text) for line in true_false_option_lines(frame)} == {"对", "错"}:
        return "true_false"
    return None


def stem_parts(frame: Frame) -> tuple[int | None, str, list[OCRLine]]:
    """从编号到第一项/弹窗之间提取题干，不依赖小字页码的位置。"""
    lines = sorted(frame.lines, key=lambda line: (line.center[1], line.x1))
    headers = practice_header_lines(frame)
    option_ids = {id(line) for line in option_boundary_lines(frame)}
    boundaries = [line.y1 for line in lines
                  if id(line) in option_ids
                  or is_partial_answer_option(frame, line)
                  or "正确答案" in compact(line.text)
                  or compact(line.text) in UI_TEXT | {"答题错误", "答案错误", "回答错误", "选项错误", "答题正确"}
                  or line in correct_feedback_lines(frame)]
    bottom = min(boundaries, default=frame.height * 0.91)
    headers = [line for line in headers if line.y1 < bottom]
    numbered = [line.y1 - 1 for line in lines if line.y1 < bottom and NUMBER_RE.match(line.text)]
    fallback_top = min([frame.height * 0.10, *numbered])
    top = max((line.y2 for line in headers), default=fallback_top)
    body = [line for line in lines if top < line.center[1] < bottom
            and not is_counter_line(frame, line) and not is_progress_decoration(frame, line)]
    first_index = next((i for i, line in enumerate(body)
                        if NUMBER_RE.match(line.text)
                        or (line.x1 < frame.width * 0.3 and re.fullmatch(r"\d+", line.text.strip()))), None)
    if first_index is not None:
        body = body[first_index:]
        prefix = NUMBER_RE.match(body[0].text)
        number = int(prefix.group(1)) if prefix else int(body[0].text.strip())
        stem = (prefix.group(2) if prefix else "") + "".join(line.text for line in body[1:])
    else:
        number = None
        stem = "".join(line.text for line in body)
    return number, stem.strip(), body


def is_partial_answer_option(frame: Frame, line: OCRLine) -> bool:
    """答案弹窗遮住选项正文时，页码下方露出的单个标签仍是选项边界。"""
    if (not re.fullmatch(r"[A-H]", compact(line.text).upper())
            or line.x1 >= frame.width * .30
            or not any("正确答案" in compact(row.text) or compact(row.text) == "知道了"
                       for row in frame.lines)):
        return False
    counters = [row for row in frame.lines if is_counter_line(frame, row)
                and PROGRESS_RE.fullmatch(row.text.strip()) and row.confidence >= .60]
    return len(counters) == 1 and counters[0].y2 < line.y1


def bind_progress_occlusion(frame: Frame, reference_frame: Frame | None) -> dict:
    """只屏蔽被新答案弹窗实际遮住的辅助页码，保留原始 OCR 证据。"""
    frame.progress_occlusion = {}
    if reference_frame is None or not has_answer_dialog(frame):
        return frame.progress_occlusion
    try:
        _, _, previous_progress = page_metadata(reference_frame)
    except RecognitionError:
        return frame.progress_occlusion
    if previous_progress is None:
        return frame.progress_occlusion
    dialog = find_answer_dialog(frame, reference_frame)
    if dialog is None or not covered_stem_rows(frame, reference_frame, dialog, [previous_progress]):
        return frame.progress_occlusion
    height = previous_progress.y2 - previous_progress.y1
    candidates = []
    for line in frame.lines:
        if not is_counter_line(frame, line) or not PROGRESS_RE.fullmatch(line.text.strip()):
            continue
        overlap = min(line.y2, previous_progress.y2) - max(line.y1, previous_progress.y1)
        if (overlap >= .5 * min(height, line.y2 - line.y1)
                and abs(line.x2 - previous_progress.x2) <= height
                and line.x1 < previous_progress.x2 and line.x2 > previous_progress.x1):
            candidates.append(line)
    # 只处理唯一对应的残缺页码；无遮挡的分母数字仍须与原页码总数一致。
    if len(candidates) == 1 and (int(PROGRESS_RE.fullmatch(candidates[0].text.strip()).group(2))
                                 == int(PROGRESS_RE.fullmatch(previous_progress.text.strip()).group(2))):
        frame.progress_occlusion = {
            "reason": "答案弹窗覆盖了原页面进度的数字，残缺进度不参与题号核对。",
            "dialog_box": list(dialog), "reference_progress": asdict(previous_progress),
            "ignored_progress": [asdict(candidates[0])],
        }
    return frame.progress_occlusion


def page_metadata(frame: Frame) -> tuple[int | None, int | None, OCRLine | None]:
    """优先使用题干前的编号；页码及标题总题数用于相互校验。"""
    number, _, stem_lines = stem_parts(frame)
    if number is not None:
        if number < 1:
            raise RecognitionError("题干开头的题号应从 1 开始。")
        if stem_lines[0].confidence < 0.60:
            raise RecognitionError("题干开头的题号识别置信度过低。")
    matches = []
    for line in frame.lines:
        if is_counter_line(frame, line):
            if asdict(line) in frame.progress_occlusion.get("ignored_progress", []):
                continue
            match = PROGRESS_RE.fullmatch(line.text.strip())
            if match:
                n, total = map(int, match.groups())
                if not 1 <= n <= total <= 100000:
                    raise RecognitionError("页面题号进度不合法。")
                matches.append((n, total, line))
    if len(matches) > 1:
        raise RecognitionError("识别到多个题号进度，无法确定当前题目。")
    progress = matches[0][2] if matches else None
    progress_total = None
    if matches:
        n, progress_total, progress = matches[0]
        if progress.confidence < 0.60:
            raise RecognitionError("题号进度识别置信度过低。")
        if number is not None and number != n:
            raise RecognitionError("题干编号与页面进度不一致，可能正在切换题目。")
        number = number if number is not None else n
    totals = []
    for line in practice_header_lines(frame):
        if "练习" in line.text:
            match = re.search(r"共\s*(\d+)\s*题", line.text)
            if match:
                if line.confidence < 0.60:
                    raise RecognitionError("顶部总题数识别置信度过低。")
                totals.append(int(match.group(1)))
    if len(set(totals)) > 1:
        raise RecognitionError("顶部出现不同的总题数。")
    header_total = totals[0] if totals else None
    if header_total is not None and not 1 <= header_total <= 100000:
        raise RecognitionError("顶部总题数不合法。")
    if header_total is not None and progress_total is not None and header_total != progress_total:
        raise RecognitionError("顶部总题数与当前进度的总题数不一致。")
    total = header_total if header_total is not None else progress_total
    if total is not None and number is not None and number > total:
        raise RecognitionError("题干编号超过了总题数。")
    return number, total, progress


def read_progress(frame: Frame) -> tuple[int, int, OCRLine | None]:
    number, total, progress = page_metadata(frame)
    if number is None:
        raise RecognitionError("未识别到题目开头的编号，也未识别到可用的页面进度。")
    if total is None:
        raise RecognitionError("未识别到总题数，请框选标题中的“共379题”或完整页面进度。")
    return number, total, progress


def stem_identity(text: str) -> str:
    return ocr_text_identity(text)


STEM_MATCH_THRESHOLD = 0.90
STEM_NUMBER_RE = re.compile(r"\d+(?:[.:]\d+)*")
STEM_EXTRA_OPERATORS = frozenset("+-−*/÷×∙⋅=<>≤≥≠±%‰^")


def _stem_critical_matches(text: str) -> list[tuple[int, int, str]]:
    """保留整段数值及所有Unicode数学符号，返回其顺序和字符范围。"""
    matches, index = [], 0
    while index < len(text):
        numeric = STEM_NUMBER_RE.match(text, index)
        if numeric is not None:
            matches.append((index, numeric.end(), numeric.group()))
            index = numeric.end()
            continue
        character = text[index]
        if character in STEM_EXTRA_OPERATORS or unicodedata.category(character) == "Sm":
            matches.append((index, index + 1, character))
        index += 1
    return matches


def stem_critical_tokens(text: str) -> list[str]:
    return [token for _, _, token in _stem_critical_matches(stem_identity(text))]


def _edit_distances_to_prefixes(expected: str, actual: str) -> list[int]:
    """末行同时给出 actual 与 expected 各前缀的 Levenshtein 编辑距离。"""
    previous = list(range(len(expected) + 1))
    for row, actual_char in enumerate(actual, start=1):
        current = [row]
        for column, expected_char in enumerate(expected, start=1):
            current.append(min(current[-1] + 1, previous[column] + 1,
                               previous[column - 1] + (actual_char != expected_char)))
        previous = current
    return previous


def stem_similarity(expected: str, actual: str) -> float:
    """普通标点不计入；相似度为 1 - 编辑距离 / 较长正文的字数。"""
    left, right = stem_identity(expected), stem_identity(actual)
    if left == right:
        return 1.0
    if not left or not right:
        return 0.0
    return 1.0 - _edit_distances_to_prefixes(left, right)[-1] / max(len(left), len(right))


def stem_matches(expected: str, actual: str, threshold: float = STEM_MATCH_THRESHOLD) -> bool:
    """题干允许少量字词波动，数字及数学符号的内容和顺序仍须一致。"""
    left, right = stem_identity(expected), stem_identity(actual)
    return (stem_critical_tokens(left) == stem_critical_tokens(right)
            and stem_similarity(left, right) + 1e-12 >= threshold)


def stem_prefix_similarity(expected: str, actual: str) -> float:
    """比较从题干开头开始的可见前缀，不能截断数字来规避数值核对。"""
    left, right = stem_identity(expected), stem_identity(actual)
    if not left or not right:
        return 0.0
    critical = _stem_critical_matches(left)
    right_tokens = stem_critical_tokens(right)
    distances = _edit_distances_to_prefixes(left, right)
    best = 0.0
    for length in range(1, len(left) + 1):
        if any(start < length < end for start, end, _ in critical):
            continue
        if [token for _, end, token in critical if end <= length] != right_tokens:
            continue
        best = max(best, 1.0 - distances[length] / max(length, len(right)))
    return best


def stem_prefix_matches(expected: str, actual: str) -> bool:
    return stem_prefix_similarity(expected, actual) + 1e-12 >= STEM_MATCH_THRESHOLD


def questions_match(expected: Question, actual: Question) -> bool:
    return ((expected.number, expected.total, compact(expected.bank_name))
            == (actual.number, actual.total, compact(actual.bank_name))
            and question_content_key("", expected.options) == question_content_key("", actual.options)
            and stem_matches(expected.stem, actual.stem))


def stem_rows_covered(reference_rows: list[OCRLine], actual_rows: list[OCRLine]) -> bool:
    """允许OCR拆行/合行，但每行原题干的位置必须有本次读到的文字覆盖。"""
    return all(any(line.y1 - (line.y2 - line.y1) * .35 <= old.center[1]
                   <= line.y2 + (line.y2 - line.y1) * .35 for line in actual_rows)
               for old in reference_rows)


def complete_recovered_stem(expected: str, actual: str) -> bool:
    """完整行已恢复后，题干文字达到90%相似且数字/数学符号一致。"""
    return stem_matches(expected, actual)


def refinement_drops_terminal_quote(expected: str, actual: str) -> bool:
    """分区OCR只漏末引号时，用原图保留的完整标点保存正文。"""
    raw_expected, raw_actual = compact(expected), compact(actual)
    if any(match.end() == len(raw_expected) for match in CHINESE_QUOTE_RE.finditer(raw_expected)):
        normalize = lambda text: CHINESE_QUOTE_RE.sub(lambda match: '"' + match.group(1) + '"', text)
        return normalize(raw_actual + raw_expected[-1]) == normalize(raw_expected)
    return False


def refinement_drops_blank_punctuation(expected: str, actual: str) -> bool:
    """完整原图有空白括号时，分区重读不能只删其括号及引语末闭引号。"""
    original, candidate = compact(expected), compact(actual)
    permitted = {index for match in re.finditer(r"\(\)", original)
                 for index in range(match.start(), match.end())}
    if not permitted:
        return False
    if any(match.end() == len(original) for match in CHINESE_QUOTE_RE.finditer(original)):
        permitted.add(len(original) - 1)
    import difflib
    changes = [op for op in difflib.SequenceMatcher(None, original, candidate,
                                                   autojunk=False).get_opcodes()
               if op[0] != "equal"]
    return bool(changes) and all(
        operation == "delete" and all(index in permitted for index in range(a1, a2))
        for operation, a1, a2, _, _ in changes)


def verify_answer_question(frame: Frame, question: Question) -> None:
    number, total, _ = page_metadata(frame)
    if number is None:
        raise RecognitionError("答案画面未识别到题号数字，等待题号可读后再匹配答案。")
    if number != question.number:
        raise RecognitionError("答案弹窗的题号与刚才截图的题号不一致。")
    if total is not None and total != question.total:
        raise RecognitionError("答案弹窗的总题数与刚才截图不一致。")


def stem_region_box(frame: Frame) -> tuple[int, int, int, int] | None:
    _, _, rows = stem_parts(frame)
    if not rows:
        return None
    heights = sorted(line.y2 - line.y1 for line in rows)
    height = heights[len(heights) // 2]
    headers = practice_header_lines(frame)
    header_bottom = max((line.y2 for line in headers if line.y2 < rows[0].y1), default=0)
    option_top = min((line.y1 - 2 for line in option_boundary_lines(frame)),
                     default=frame.height)
    box = (max(0, int(min(line.x1 for line in rows) - height)),
           max(int(header_bottom + 2), 0, int(min(line.y1 for line in rows) - 1.4 * height)),
           min(frame.width, int(max(line.x2 for line in rows) + height + 1)),
           min(frame.height, int(option_top), int(max(line.y2 for line in rows) + 0.55 * height + 1)))
    return box if box[2] > box[0] and box[3] > box[1] else None


def frame_metadata_line(frame: Frame, line: OCRLine) -> bool:
    return is_counter_line(frame, line) or any(line == header for header in practice_header_lines(frame))


def replace_stem_region(frame: Frame, piece: Frame, box: tuple[int, int, int, int]) -> Frame:
    translated = [replace(line, x1=line.x1 + box[0], x2=line.x2 + box[0],
                          y1=line.y1 + box[1], y2=line.y2 + box[1]) for line in piece.lines]
    context = Frame(frame.image, [*frame.lines, *translated])
    translated = [line for line in translated if not frame_metadata_line(context, line)]
    kept = [line for line in frame.lines if frame_metadata_line(frame, line) or not (
        box[0] <= line.center[0] <= box[2] and box[1] <= line.center[1] <= box[3])]
    return Frame(frame.image, sorted([*kept, *translated], key=lambda line: (line.center[1], line.x1)))


def practice_bank_name(header: str) -> str:
    name = re.sub(r"^[\d\s|丨!！]+", "", header)
    # 只剥离紧接常见完整标题的单个装饰字母，保留 C语言、B组、AI 等真实名称。
    name = re.sub(r"^[A-Ha-hＡ-Ｈａ-ｈ]\s*(?=(?:单选题|多选题|判断题|填空题|选择题|问答题)练习\s*(?:[（(]|$))",
                  "", name)
    return re.sub(r"[（(].*", "", name).strip() + "题库"


def parse_question(frame: Frame, min_confidence: float = 0.60, expected_options: int = 4,
                   question_type: str | None = None) -> Question:
    detected = detected_question_type(frame)
    if question_type is not None and question_type not in QUESTION_TYPE_NAMES:
        raise ValueError(f"不支持的题型：{question_type}")
    if question_type is not None and detected is not None and question_type != detected:
        raise RecognitionError(f"当前界面是{QUESTION_TYPE_NAMES[detected]}，请将采集题型切换为{QUESTION_TYPE_NAMES[detected]}。")
    question_type = question_type or detected or "single"
    # 分区增强告警仅用于诊断；初次正文、选项仍须可完整提取，页面只核对数字。
    if has_correct_feedback(frame):
        raise RecognitionError("答题正确提示仍在显示，等待提示消失后读取完整选项。")
    if any("正确答案" in compact(line.text) or compact(line.text) == "知道了"
           for line in frame.lines):
        raise RecognitionError("答案弹窗仍打开，请先关闭弹窗再读取完整选项。")
    number, total, _ = read_progress(frame)
    _, stem, stem_lines = stem_parts(frame)
    if not stem or not stem_lines:
        raise RecognitionError("题干识别为空。")

    # 只保留题干下方到工具栏上方的题目选项，页码不是必要边界。
    toolbar_lines = [line for line in frame.lines
                     if any(text in compact(line.text) for text in ("下一题", "查看答案", "完成练习"))]
    if not toolbar_lines:
        raise RecognitionError("底部工具栏未识别到，请完整显示小程序。")
    bottom = min(line.y1 for line in toolbar_lines)
    content = [line for line in frame.lines
               if stem_lines[-1].y2 < line.center[1] < bottom
               and compact(line.text) not in UI_TEXT and not is_counter_line(frame, line)
               and not is_progress_decoration(frame, line)]
    options, points, used = {}, {}, list(stem_lines)
    active = None
    for line in content:
        if question_type == "true_false":
            label = true_false_option_label(line.text)
            if label is None:
                raise RecognitionError(f"判断题选项无法识别：{line.text}")
            if label in options:
                raise RecognitionError(f"判断题选项 {label} 出现重复标签。")
            options[label] = label
            points[label] = line.center
            used.append(line)
            continue
        m = OPTION_RE.match(line.text)
        if m:
            label, text = m.groups()
            label = unicodedata.normalize("NFKC", label).upper()
            if label in options:
                raise RecognitionError(f"选项 {label} 出现重复标签。")
            options[label] = text.strip()
            points[label] = line.center
            active = label
        elif active is not None:
            options[active] += line.text.strip()
        else:
            # 未标记的第一行可能意味着 A 标签丢失，不能继续猜。
            raise RecognitionError(f"选项标签无法识别：{line.text}")
        used.append(line)
    if question_type == "true_false":
        if set(options) != {"对", "错"}:
            raise RecognitionError("判断题选项不完整，请完整显示“对”和“错”两个选项。")
    else:
        expected = list("ABCDEFGH"[:len(options)])
        if len(options) < 2 or list(options) != expected or any(not v for v in options.values()):
            raise RecognitionError("选项不完整或标签不连续，要求从 A 开始且至少两个选项。")
    if question_type != "true_false" and expected_options and len(options) != expected_options:
        raise RecognitionError(f"应有 {expected_options} 个选项，实际仅识别到 {len(options)} 个。请完整显示所有选项。")
    confidence = min(line.confidence for line in used)
    if confidence < min_confidence:
        raise RecognitionError(f"题目识别置信度 {confidence:.2f} 低于 {min_confidence:.2f}。")
    header = next((line.text for line in practice_header_lines(frame) if line.y2 < stem_lines[0].y1), "练习")
    # 标题左侧蓝色竖线有时被 OCR 识别为 1 或 !，不属于题库名称。
    bank_name = practice_bank_name(header)
    return Question(number, total, stem.strip(), options, bank_name, confidence, points, question_type)


def parse_answer(frame: Frame, question: Question, selected_label: str | None = None,
                 reference_frame: Frame | None = None) -> list[str]:
    if reference_frame is not None:
        bind_progress_occlusion(frame, reference_frame)
    verify_answer_question(frame, question)
    return _parse_answer_labels(frame, question, selected_label)


def selected_option_labels(frame: Frame, question: Question) -> set[str]:
    """读取多选框的勾/蓝色选中样式；只用于选择状态，不用它推断答案。"""
    import numpy as np
    result = set()
    for line in frame.lines:
        match = OPTION_RE.match(line.text)
        if not match:
            continue
        label = unicodedata.normalize("NFKC", match.group(1)).upper()
        if label not in question.options:
            continue
        if re.match(r"^\s*[✓✔√☑✅]", line.text):
            result.add(label)
            continue
        height = max(1, line.y2 - line.y1)
        box = (max(0, int(min(line.x1 - height * 2, frame.width * .05))),
               max(0, int(line.y1 - height * .35)),
               min(frame.width, int(line.x2 + height)),
               min(frame.height, int(line.y2 + height * .35)))
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        pixels = np.asarray(frame.image.crop(box).convert("RGB"), dtype=np.int16)
        red, green, blue = pixels[:, :, 0], pixels[:, :, 1], pixels[:, :, 2]
        selected_blue = ((red < 110) & (green >= 55) & (green < 180)
                         & (blue >= 160) & (blue - red > 80) & (blue - green > 45))
        if np.count_nonzero(selected_blue) >= max(8, height * height * .08):
            result.add(label)
    return result


def _parse_answer_labels(frame: Frame, question: Question,
                         selected_label: str | None = None) -> list[str]:
    """将当前答案字母或文字唯一匹配到已识别的原题选项。"""
    # 先限定“正确答案”所在行，防止把题干中的英文字母当成答案。
    candidates = [line for line in frame.lines if "正确答案" in compact(line.text)]
    feedback = correct_feedback_lines(frame)
    if not candidates and feedback:
        if len(feedback) != 1 or feedback[0].confidence < 0.60:
            raise RecognitionError("答题正确提示识别置信度过低或出现多次。")
        if selected_label is None or selected_label not in question.options:
            raise RecognitionError("识别到答题正确，但没有本次点击的选项记录，不能推测正确答案。")
        return [selected_label]
    if len(candidates) != 1:
        raise RecognitionError("未识别到唯一的“正确答案”内容。")
    line = candidates[0]
    if line.confidence < 0.60:
        raise RecognitionError("答案识别置信度过低。")
    text = unicodedata.normalize("NFKC", line.text)
    value = re.split(r"正确答案(?:是|为)?\s*[:：]?\s*", text, maxsplit=1)[-1].strip()
    if re.fullmatch(r"[A-Ha-h](?:[\s,、，;；/&和]*[A-Ha-h])*[。.]?", value):
        labels = sorted(set(re.findall(r"[A-H]", value.upper())))
    else:
        labels = [label for label, option in question.options.items()
                  if ocr_text_identity(option).rstrip("。.") == ocr_text_identity(value).rstrip("。.")]
        if len(labels) != 1:
            raise RecognitionError(f"答案无法唯一匹配原选项：{value!r}")
    if not labels or any(label not in question.options for label in labels):
        raise RecognitionError(f"答案 {value!r} 在原选项中不存在。")
    if feedback and selected_label is not None and labels != [selected_label]:
        raise RecognitionError("答题正确提示与弹窗显示的正确答案不一致。")
    return labels


def atomic_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


class BankStore:
    def __init__(self, output_dir: Path, question_type: str = "single"):
        self.question_type = question_type
        self.output_dir = question_output_dir(output_dir, question_type)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.output_dir / "题库.json"
        self.data = {"version": 1, "bank_name": "练习题库", "question_type": question_type,
                     "total": None, "questions": []}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if self.data.get("version") != 1 or not isinstance(self.data.get("questions"), list):
                raise RecognitionError("题库 JSON 格式不正确。请选择新的输出目录。")
            saved_type = self.data.get("question_type")
            if saved_type is None:
                saved_type = next((mode for mode, name in QUESTION_TYPE_NAMES.items()
                                   if name in self.data.get("bank_name", "")), "single")
            if saved_type != question_type:
                raise RecognitionError("现有 JSON 属于另一题型，请选择对应题型或新的输出目录。")
        self.records = {item["number"]: item for item in self.data["questions"]}
        if len(self.records) != len(self.data["questions"]):
            raise RecognitionError("题库中存在重复题号，请检查 JSON。")
        self.backup_path: Path | None = None

    def validate(self, question: Question, overwrite: bool = False) -> bool:
        if question.question_type != self.question_type:
            raise RecognitionError("题目类型与当前保存目录不一致，不能覆盖其他题型的记录。")
        if self.data["total"] is not None and self.data["total"] != question.total:
            raise RecognitionError("现有题库总题数与当前小程序不同。请为另一题库选择新目录。")
        record = self.records.get(question.number)
        return record is not None

    def evidence_paths(self, number: int, overwrite: bool = False) -> tuple[Path, Path]:
        # 覆盖时使用新文件名，识别失败或恢复 JSON 备份时仍能使用原截图。
        prefix = f"{number:04d}"
        if overwrite and number in self.records:
            prefix += f"_{datetime.now():%Y%m%d_%H%M%S_%f}_{uuid4().hex[:8]}"
        folder = self.output_dir / "screenshots"
        return folder / f"{prefix}_question.png", folder / f"{prefix}_answer.png"

    def add(self, question: Question, answer: list[str], question_path: Path, answer_path: Path,
            answer_source: str = "answer_dialog", overwrite: bool = False) -> bool:
        exists = self.validate(question, overwrite=overwrite)
        if exists and not overwrite:
            return False
        if not answer or any(label not in question.options for label in answer):
            raise RecognitionError("正确答案为空或无法匹配当前选项，保留原记录。")
        if question.question_type == "true_false" and len(answer) != 1:
            raise RecognitionError("判断题正确答案只能是“对”或“错”其中一个，保留原记录。")
        record = {
            "number": question.number, "question": question.stem, "question_type": question.question_type,
            "options": dict(question.options),
            "correct_labels": list(answer),
            "correct_answers": [question.options[label] for label in answer],
            "answer_source": answer_source,
            "signature": question.signature, "ocr_confidence": question.confidence,
            "question_screenshot": str(question_path.relative_to(self.output_dir)),
            "answer_screenshot": str(answer_path.relative_to(self.output_dir)),
            "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        records = {**self.records, question.number: record}
        data = {**self.data, "bank_name": question.bank_name, "question_type": question.question_type,
                "total": question.total,
                "questions": sorted(records.values(), key=lambda item: item["number"])}
        if exists and self.backup_path is None:
            backup = self.output_dir / "backups" / f"题库_{datetime.now():%Y%m%d_%H%M%S_%f}_{uuid4().hex[:8]}.json"
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.path, backup)
            self.backup_path = backup
        # 磁盘保存成功后才更新内存，写入失败时 Word 导出仍使用原题库。
        atomic_json(self.path, data)
        self.records, self.data = records, data
        return True

    def export(self, title: str | None = None) -> Path | None:
        if not self.records:
            return None
        target = self.output_dir / WORD_FILENAMES[self.question_type]
        try:
            export_docx(self.data, target, title)
        except PermissionError:
            # Word 打开时保留采集成果，另存一个时间戳文件。
            target = target.with_name(f"{target.stem}_{datetime.now():%Y%m%d_%H%M%S}.docx")
            export_docx(self.data, target, title)
        return target


def export_docx(data: dict, target: Path, title: str | None = None) -> None:
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor
    doc = Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.top_margin = section.bottom_margin = Cm(2.0)
    section.left_margin = section.right_margin = Cm(2.2)
    for name in ("Normal", "Title", "Heading 1"):
        style = doc.styles[name]
        style.font.name = "Calibri"
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "宋体")
    normal = doc.styles["Normal"]
    normal.font.size = Pt(11)
    normal.paragraph_format.line_spacing = 1.22
    normal.paragraph_format.space_after = Pt(4)
    doc.styles["Title"].font.size = Pt(20)
    doc.styles["Title"].paragraph_format.space_after = Pt(10)
    doc.add_paragraph(title or data.get("bank_name", "练习题库"), style="Title")
    records = sorted(data["questions"], key=lambda item: item["number"])
    doc.add_paragraph(f"收录 {len(records)} 题。每题列出题号、题目、选项及正确答案。")
    for record in records:
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(10)
        p.paragraph_format.keep_with_next = True
        p.paragraph_format.keep_together = True
        p.add_run(f"{record['number']}. {record['question']}").bold = True
        for label, text in record["options"].items():
            judgment = record.get("question_type", data.get("question_type")) == "true_false"
            p = doc.add_paragraph(text if judgment else f"{label}. {text}")
            p.paragraph_format.left_indent = Cm(0.45)
            p.paragraph_format.first_line_indent = Cm(-0.45)
            p.paragraph_format.keep_with_next = True
            p.paragraph_format.keep_together = True
        p = doc.add_paragraph()
        p.paragraph_format.keep_together = True
        labels = record["correct_labels"]
        judgment = record.get("question_type", data.get("question_type")) == "true_false"
        p.add_run("正确答案：" + "；".join(
            record["options"][label] if judgment else f"{label}. {record['options'][label]}"
            for label in labels)).bold = True
    # 长题库需要页码，使用域保证 Word 打开后可更新。
    footer = section.footer.paragraphs[0]
    footer.alignment = 1
    run = footer.add_run()
    field_el = OxmlElement("w:fldSimple")
    field_el.set(qn("w:instr"), "PAGE")
    run._r.addnext(field_el)
    doc.core_properties.title = title or data.get("bank_name", "练习题库")
    doc.core_properties.author = ""
    temp = target.with_name(target.stem + ".tmp.docx")
    doc.save(temp)
    os.replace(temp, target)


class StopControl:
    def __init__(self, event: threading.Event | None = None):
        self.event = event or threading.Event()
        if os.name == "nt":
            ctypes.windll.user32.GetAsyncKeyState(0x1B)  # 清除开始前旧的 Esc 按键记录。

    def check(self) -> None:
        escape = os.name == "nt" and ctypes.windll.user32.GetAsyncKeyState(0x1B) & 0x8001
        if self.event.is_set() or escape:
            raise Stopped("已停止，已完成的题目已保存。")

    def pause(self, seconds: float) -> None:
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            self.check()
            self.event.wait(min(0.05, max(0.0, until - time.monotonic())))
        self.check()


class Desktop:
    def __init__(self, region: dict, stop: StopControl):
        import mss
        import pyautogui
        self.region, self.stop = region, stop
        self.capture = mss.mss()  # 必须在采集线程中创建。
        self.mouse = pyautogui
        self.mouse.FAILSAFE = True
        self.mouse.PAUSE = 0.05

    def screenshot(self):
        from PIL import Image
        self.stop.check()
        try:
            self.mouse.failSafeCheck()
        except self.mouse.FailSafeException as exc:
            raise Stopped("鼠标移到屏幕角落，已停止采集。") from exc
        shot = self.capture.grab(self.region)
        return Image.frombytes("RGB", shot.size, shot.rgb)

    def fast_screenshot(self):
        # mss 在采集线程内复用；每次 frombytes 都产生独立原图，可直接冻结。
        return self.screenshot()

    def click(self, point: tuple[float, float]):
        self.stop.check()
        x, y = point
        if not (0 <= x < self.region["width"] and 0 <= y < self.region["height"]):
            raise RecognitionError("点击坐标超出已校准区域。")
        try:
            self.mouse.click(self.region["left"] + round(x), self.region["top"] + round(y))
        except self.mouse.FailSafeException as exc:
            raise Stopped("鼠标移到屏幕角落，已停止采集。") from exc
        self.stop.check()

    def close(self):
        self.capture.close()


def save_frame(frame: Frame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.image.save(path)
    atomic_json(path.with_suffix(".ocr.json"), [asdict(line) for line in frame.lines])


def recognition_details(frame: Frame, question: Question | None = None,
                        fresh: Question | None = None) -> dict:
    """仅用于日志，任何解析错误也记录原文，不参与放宽采集校验。"""
    details = {"frame_id": getattr(frame, "debug_id", None), "width": frame.width,
               "height": frame.height, "ocr": [asdict(line) for line in frame.lines],
               "ocr_passes": frame.ocr_passes, "refinement_error": frame.refinement_error,
               "progress_occlusion": frame.progress_occlusion}
    try:
        number, total, _ = page_metadata(frame)
        details["actual_metadata"] = {"number": number, "total": total}
    except Exception as exc:
        details["metadata_error"] = str(exc)
    _, stem, _ = stem_parts(frame)
    details["actual_stem"] = stem
    details["actual_stem_normalized"] = stem_identity(stem)
    if fresh is not None:
        details["actual_question"] = {**asdict(fresh), "signature": fresh.signature}
    if question is not None:
        details["expected_question"] = {**asdict(question), "signature": question.signature}
        details["expected_stem_normalized"] = stem_identity(question.stem)
        details["stem_difference"] = text_difference(question.stem, stem)
        details["normalized_stem_difference"] = text_difference(stem_identity(question.stem), stem_identity(stem))
        details["normalized_prefix_match"] = stem_identity(question.stem).startswith(stem_identity(stem))
        details["raw_prefix_match"] = compact(question.stem).startswith(compact(stem))
        details["stem_similarity"] = stem_similarity(question.stem, stem)
        details["stem_match_threshold"] = STEM_MATCH_THRESHOLD
        details["stem_similarity_match"] = stem_matches(question.stem, stem)
        details["stem_prefix_similarity"] = stem_prefix_similarity(question.stem, stem)
        details["stem_critical_tokens_match"] = (stem_critical_tokens(question.stem)
                                                 == stem_critical_tokens(stem))
        if fresh is not None:
            details["option_difference"] = {
                label: {"expected": question.options.get(label), "actual": fresh.options.get(label),
                        "expected_normalized": ocr_text_identity(question.options.get(label, "")),
                        "actual_normalized": ocr_text_identity(fresh.options.get(label, "")),
                        "changes": text_difference(question.options.get(label, ""), fresh.options.get(label, ""))}
                for label in dict.fromkeys([*question.options, *fresh.options])
                if question.options.get(label) != fresh.options.get(label)}
    return details


class Collector:
    """一次读取完整题目；后续只检查题号并使用当前按钮坐标。"""
    def __init__(self, desktop, engine, store: BankStore, stop: StopControl,
                 log: Callable[[str], None] = print, delay: float = 0.6,
                 attempts: int = 8, on_progress: Callable[[int, int], None] | None = None,
                 expected_options: int = 4, overwrite: bool = False,
                 debug: DebugLog | None = None,
                 on_ocr: Callable[[dict], None] | None = None, question_type: str = "single"):
        if question_type not in QUESTION_TYPE_NAMES:
            raise ValueError(f"不支持的题型：{question_type}")
        self.desktop, self.engine, self.store, self.stop = desktop, engine, store, stop
        self.delay, self.attempts = delay, attempts
        self.debug = debug or DebugLog(store.output_dir, settings={
            "region": getattr(desktop, "region", None), "delay": delay, "attempts": attempts,
            "expected_options": expected_options, "overwrite": overwrite,
            "question_type": question_type}, warning=log)

        def logged_message(message):
            self.debug.event("message", str(message))
            log(str(message))
        self.log = logged_message
        self.on_progress = on_progress or (lambda done, total: None)
        self.on_ocr = on_ocr
        self.expected_options = expected_options
        self.question_type = question_type
        self.overwrite = overwrite
        self.last_frame: Frame | None = None
        self.question_frame: Frame | None = None
        self.reveal_point: tuple[float, float] | None = None

    def publish_ocr(self, frame: Frame, stage: str, number: int | None = None,
                    attempt: int | None = None) -> None:
        if self.on_ocr is None:
            return
        try:
            # 只传独立文字快照；工作线程不访问 Tk 或可变 Frame。
            self.on_ocr({"stage": stage, "number": number, "attempt": attempt,
                         "text": "\n".join(line.text for line in frame.lines),
                         "frame_id": getattr(frame, "debug_id", None)})
        except Exception as exc:
            self.debug.event("ocr_viewer_error", str(exc), stage=stage)

    def record_check(self, event: str, frame: Frame, question: Question | None = None,
                     fresh: Question | None = None, reason: str = "", attempt: int | None = None,
                     snapshot: bool = False) -> None:
        try:
            details = recognition_details(frame, question, fresh)
            if snapshot:
                details["screenshot"] = self.debug.capture(frame, event)
                details["before_screenshot"] = self.debug.capture(self.question_frame, "before_selection")
            self.debug.event(event, reason, attempt=attempt, **details)
        except Exception as exc:
            self.debug.event("diagnostic_error", str(exc), stage=event)

    def click(self, label: str, point: tuple[float, float]) -> None:
        frame_id = getattr(self.last_frame, "debug_id", None)
        region = getattr(self.desktop, "region", None)
        screen_point = ([region["left"] + round(point[0]), region["top"] + round(point[1])]
                        if isinstance(region, dict) else None)
        self.debug.event("click_requested", label, point=list(point), frame_id=frame_id,
                         screen_point=screen_point)
        self.desktop.click(point)
        self.debug.event("click_completed", label, point=list(point), frame_id=frame_id)

    def frame(self, stage: str = "frame", number: int | None = None,
              attempt: int | None = None, image=None,
              feedback_candidate: FeedbackCandidate | None = None,
              require_correct: bool = True) -> Frame:
        self.stop.check()
        self.debug.event("frame_requested", stage, number=number, attempt=attempt)
        image = self.desktop.screenshot() if image is None else image
        if feedback_candidate is None:
            frame = self.engine.read(image)
        else:
            frame = self.read_feedback_frame(image, feedback_candidate, require_correct=require_correct)
        # 文字行合并有利于正文；底部按钮必须分别定位，使用原始 OCR 区域重读。
        # 对每个按钮使用固定横向五等分区域 OCR，文字坐标仍来自识别结果。
        # 一般窗口无需此分支，只有相邻按钮被 merge_rows 合并时才触发。
        # 已确认的瞬时提示没有下一题点击；等画面恢复后再定位底部按钮。
        if (not getattr(frame, "feedback_confirmed", False)
                and not getattr(frame, "feedback_wrong_detected", False)
                and not any(compact(line.text) == "下一题" for line in frame.lines)):
            import dataclasses
            start_y = round(frame.height * 0.91)
            frame.lines = [line for line in frame.lines if line.center[1] < start_y]
            for i in range(5):
                x1, x2 = round(frame.width * i / 5), round(frame.width * (i + 1) / 5)
                crop = frame.image.crop((x1, start_y, x2, frame.height))
                piece = self.engine.read(crop, scale=2)
                for line in piece.lines:
                    frame.lines.append(dataclasses.replace(line, x1=line.x1 + x1,
                                                           x2=line.x2 + x1, y1=line.y1 + start_y,
                                                           y2=line.y2 + start_y))
            frame.lines.sort(key=lambda line: (line.center[1], line.x1))
        bind_progress_occlusion(frame, self.question_frame)
        self.last_frame = frame
        self.debug.frame(frame, stage, number, attempt)
        if frame.progress_occlusion:
            self.debug.event("progress_occluded", "忽略被答案弹窗遮挡的残缺页码",
                             frame_id=getattr(frame, "debug_id", None), **frame.progress_occlusion)
        self.publish_ocr(frame, stage, number, attempt)
        self.stop.check()
        return frame

    def read_feedback_frame(self, image, candidate: FeedbackCandidate,
                            require_correct: bool = True) -> Frame:
        """独立读取已冻结提示，不让被遮选项的重读丢弃正确提示行。"""
        x1, y1, x2, y2 = candidate.bbox
        box = (max(0, x1 - 8), max(0, y1 - 8),
               min(image.width, x2 + 8), min(image.height, y2 + 8))
        self.stop.check()
        piece = self.engine.read(image.crop(box), scale=2)
        self.stop.check()
        # 遮挡画面只作原始全图识别；选项原文来自提示前的完整题目。
        scale = max(1.0, min(2.0, 780 / image.width))
        frame = self.engine.read(image, scale=scale)
        translated = [replace(line, x1=line.x1 + box[0], x2=line.x2 + box[0],
                              y1=line.y1 + box[1], y2=line.y2 + box[1])
                      for line in piece.lines]
        probe = Frame(image, translated)
        anchors = correct_feedback_lines(probe)
        wrong = wrong_feedback_lines(probe)
        raw_lines = list(frame.lines)
        frame.ocr_passes = [*frame.ocr_passes, {
            "region": "feedback_frozen", "box": list(box), "scale": 2,
            "candidate": asdict(candidate), "lines": [asdict(line) for line in translated],
            "full_raw_lines": [asdict(line) for line in raw_lines],
            "ocr_stats": piece.ocr_stats}]
        if wrong:
            # 冻结图里的错误一旦识别到就锁存，后续消失也不能走无错误推断。
            frame.lines = [line for line in raw_lines
                           if not (line.x1 < x2 and line.x2 > x1 and line.y1 < y2 and line.y2 > y1)] + translated
            frame.lines.sort(key=lambda line: (line.center[1], line.x1))
            frame.feedback_wrong_detected = True
            frame.feedback_confirmed = False
            self.debug.event("feedback_wrong_detected", "冻结截图识别到错误提示",
                             candidate=asdict(candidate), ocr=[asdict(line) for line in wrong])
            return frame
        if not anchors:
            # 像素候选也可能是其他提示，绝不能仅凭灰块或蓝色选项确定答案。
            frame.lines = [line for line in frame.lines if line not in correct_feedback_lines(frame)]
            frame.feedback_confirmed = False
            self.debug.event("feedback_candidate_rejected", "提示区域没有独立的正确文字",
                             candidate=asdict(candidate), box=list(box),
                             ocr=[asdict(line) for line in translated])
            return frame
        reason = ""
        if len(anchors) != 1 or anchors[0].confidence < .90:
            reason = "短时正确提示识别置信度不足，无法确认本次选项。"
        elif self.question_frame is None or find_correct_toast(probe, self.question_frame) is None:
            reason = "短时正确提示缺少新灰色面板及白色勾的图像证据。"
        if reason:
            if not require_correct:
                frame.lines = [line for line in raw_lines if line not in correct_feedback_lines(frame)]
                frame.feedback_confirmed = False
                self.debug.event("feedback_candidate_rejected", reason,
                                 candidate=asdict(candidate), box=list(box),
                                 ocr=[asdict(line) for line in translated])
                return frame
            self.last_frame = frame
            self.debug.frame(frame, "feedback_unverified")
            self.publish_ocr(frame, "feedback_unverified")
            self.record_check("feedback_failed", frame, reason=reason, snapshot=True)
            raise RecognitionError(reason)
        # 只移除与真实面板相交的行；合并了 B 选项的提示行仍在原始日志中。
        frame.lines = [line for line in raw_lines
                       if not (line.x1 < x2 and line.x2 > x1 and line.y1 < y2 and line.y2 > y1)]
        frame.lines.append(anchors[0])
        frame.lines.sort(key=lambda line: (line.center[1], line.x1))
        frame.feedback_confirmed = True
        frame.refinement_error = ""
        self.debug.event("feedback_text_confirmed", "冻结截图的提示区域已独立确认",
                         candidate=asdict(candidate), box=list(box),
                         anchor=asdict(anchors[0]), removed_lines=len(raw_lines) - len(frame.lines) + 1)
        return frame

    def feedback_frame(self, stage: str, question: Question) -> Frame:
        """点击后先短时采样，采样期间不做 OCR、不再次点击。"""
        capture = getattr(self.desktop, "fast_screenshot", None)
        if capture is None or self.question_frame is None:
            self.stop.pause(min(self.delay, .15))
            return self.frame(stage, question.number)
        started = time.monotonic()
        duration, interval, max_frames = .85, .04, 30
        self.debug.event("feedback_capture_start", stage, number=question.number,
                         duration=duration, interval=interval, max_frames=max_frames)
        candidate = None
        sampled, image = 0, None
        while sampled < max_frames:
            self.stop.check()
            image = capture()
            sampled += 1
            candidate = find_feedback_candidate(image, self.question_frame.image)
            if candidate is not None:
                break
            remaining = duration - (time.monotonic() - started)
            if remaining <= 0:
                break
            self.stop.pause(min(interval, remaining))
        self.debug.event("feedback_capture_completed", stage, number=question.number,
                         samples=sampled, seconds=round(time.monotonic() - started, 6),
                         candidate=asdict(candidate) if candidate else None)
        if candidate is not None:
            # OCR 报错或用户停止时，已经抓到的短暂提示原图也必须留下。
            frozen = Frame(image, [])
            self.last_frame = frozen
            self.debug.frame(frozen, "feedback_captured", question.number)
            self.debug.capture(frozen, "feedback_captured")
        frame = self.frame(stage, question.number, image=image, feedback_candidate=candidate)
        if candidate is not None:
            self.debug.capture(frame, "feedback_frozen")
            if not getattr(frame, "feedback_confirmed", False) and not has_wrong_feedback(frame):
                # 不正确的提示消失后重新读取当前页，回到原有题目/答案校验流程。
                return self.frame(stage, question.number)
        return frame

    def observe_reveal_feedback(self, question: Question, round_number: int) -> tuple[list[Frame], Frame]:
        """每次查看答案完整观测一段时间；先留图，再 OCR，错误证据不丢弃。"""
        capture = getattr(self.desktop, "fast_screenshot", None)
        if capture is None or self.question_frame is None:
            self.stop.pause(REVEAL_OBSERVATION_SECONDS)
            frame = self.frame("wait_answer", question.number, round_number)
            return [frame], frame
        started = time.monotonic()
        samples, candidates, keys = 0, [], set()
        image, last_key, overflow = None, None, False
        self.debug.event("reveal_observation_start", "查看答案后观测错误提示",
                         number=question.number, round=round_number,
                         duration=REVEAL_OBSERVATION_SECONDS,
                         interval=REVEAL_SAMPLE_INTERVAL_SECONDS, max_frames=30)
        while samples < 30:
            self.stop.check()
            image = capture()
            samples += 1
            candidate = find_feedback_candidate(image, self.question_frame.image)
            last_key = None
            if candidate is not None:
                last_key = (candidate.bbox, hashlib.sha256(image.crop(candidate.bbox).tobytes()).digest())
                if last_key not in keys:
                    frozen = Frame(image, [])
                    self.last_frame = frozen
                    self.debug.frame(frozen, "reveal_feedback_captured", question.number, round_number)
                    self.debug.capture(frozen, "reveal_feedback_captured")
                    if len(candidates) < 8:
                        candidates.append((last_key, image, candidate))
                        keys.add(last_key)
                    else:
                        overflow = True
            remaining = REVEAL_OBSERVATION_SECONDS - (time.monotonic() - started)
            if remaining <= 0:
                break
            self.stop.pause(min(REVEAL_SAMPLE_INTERVAL_SECONDS, remaining))
        self.debug.event("reveal_observation_completed", "查看答案观测完成",
                         number=question.number, round=round_number, samples=samples,
                         seconds=round(time.monotonic() - started, 6),
                         candidates=len(candidates), overflow=overflow)
        frames, by_key = [], {}
        for key, frozen_image, candidate in candidates:
            frame = self.frame("reveal_feedback_frozen", question.number, round_number,
                               image=frozen_image, feedback_candidate=candidate, require_correct=False)
            self.debug.capture(frame, "reveal_feedback_frozen")
            frames.append(frame)
            by_key[key] = frame
        if (last_key in by_key and image.size == by_key[last_key].image.size
                and image.tobytes() == by_key[last_key].image.tobytes()):
            current = by_key[last_key]
        else:
            current_candidate = next((candidate for key, _, candidate in candidates if key == last_key), None)
            current = self.frame("wait_answer", question.number, round_number, image=image,
                                 feedback_candidate=current_candidate, require_correct=False)
            frames.append(current)
        current.feedback_capture_incomplete = overflow
        current.feedback_overlay_active = last_key is not None
        return frames, current

    def restore_reveal_question(self, question: Question, return_feedback: bool = True) -> Frame:
        """等提示消失时，新出现的错误/答案也必须交回答案流程处理。"""
        reason = ""
        for attempt in range(self.attempts):
            self.stop.pause(self.delay)
            frame = self.frame("reveal_restore", question.number, attempt + 1)
            if has_wrong_feedback(frame) or has_answer_dialog(frame):
                if return_feedback:
                    self.debug.event("reveal_restore_feedback", "恢复题面时检测到错误或答案，转回答案读取",
                                     number=question.number, frame_id=getattr(frame, "debug_id", None))
                    return frame
                # 关闭弹窗后的动画可能尚未结束；下一题必须等实际遮挡消失。
                self.debug.event("overlay_wait", "等待弹窗或提示消失", number=question.number,
                                 attempt=attempt + 1, frame_id=getattr(frame, "debug_id", None))
                reason = "弹窗或提示尚未消失。"
                continue
            try:
                self.check_current_page(question, frame, require_number=True)
                if has_correct_feedback(frame) or getattr(frame, "feedback_overlay_active", False):
                    raise RecognitionError("等待正确提示消失。")
                find_button(frame, "查看答案")
                return frame
            except RecognitionError as exc:
                reason = str(exc)
                self.record_check("reveal_restore_retry", frame, question, reason=reason,
                                  attempt=attempt + 1, snapshot=attempt == self.attempts - 1)
        raise RecognitionError(reason or "恢复题面超时。")

    def stable_question(self, expected: int | None = None, total: int | None = None,
                        initial_frame: Frame | None = None) -> tuple[Question, Frame]:
        """读取一次完整题目；仅未读完整或尚未切到目标题号时重试。"""
        reason = ""
        for attempt in range(self.attempts):
            if attempt == 0 and initial_frame is not None:
                frame = initial_frame
            else:
                self.stop.pause(self.delay)
                frame = self.frame("stable_question", expected, attempt + 1)
            try:
                question = parse_question(frame, expected_options=self.expected_options,
                                          question_type=self.question_type)
                if total is not None and question.total != total:
                    raise RecognitionError("切换后的题目总数改变了。")
                if expected is not None and question.number != expected:
                    raise RecognitionError(f"等待第 {expected} 题，当前仍是第 {question.number} 题。")
                self.debug.event("question_read", f"第 {question.number} 题读取完成", attempt=attempt + 1,
                                 question=asdict(question), frame_id=getattr(frame, "debug_id", None))
                return question, frame
            except RecognitionError as exc:
                reason = str(exc)
                self.record_check("question_retry", frame, reason=reason, attempt=attempt + 1,
                                  snapshot=attempt == self.attempts - 1)
        raise RecognitionError(reason or "读取题目超时。")

    def check_current_page(self, question: Question, frame: Frame,
                           require_number: bool = False) -> None:
        """只检查题号/总数，不比较题干、选项、行位置或截图尺寸。"""
        if has_answer_dialog(frame) and not frame.progress_occlusion:
            bind_progress_occlusion(frame, self.question_frame)
        try:
            number, total, _ = page_metadata(frame)
            if require_number and number is None:
                raise RecognitionError("未识别到当前题号，等待题面恢复。")
            if number is not None and number != question.number:
                raise RecognitionError("当前题号变化了，停止点击以免题目与答案错配。")
            if total is not None and total != question.total:
                raise RecognitionError("当前总题数变化了。")
        except RecognitionError as exc:
            self.record_check("page_number_failed", frame, question, reason=str(exc), snapshot=True)
            raise

    def same_question(self, question: Question, frame: Frame | None = None) -> tuple[Question, Frame]:
        # 兼容已有调用；正文始终使用最初完整截图，不再按后续 OCR 改写。
        frame = frame if frame is not None else self.frame("current_page", question.number)
        self.check_current_page(question, frame, require_number=True)
        return question, frame

    def reveal_answer(self, question: Question, frame: Frame) -> None:
        self.check_current_page(question, frame, require_number=True)
        self.reveal_point = find_button(frame, "查看答案").center
        self.click("查看答案", self.reveal_point)

    def next_question(self, question: Question, frame: Frame) -> tuple[Question, Frame]:
        """下一题点击未生效时有限补点；只对明确仍在原题的当前画面补点。"""
        if has_answer_dialog(frame) or has_correct_feedback(frame) or getattr(frame, "feedback_overlay_active", False):
            frame = self.restore_reveal_question(question, return_feedback=False)
        self.check_current_page(question, frame, require_number=True)
        self.last_frame = frame
        self.debug.capture(frame, "before_next")
        self.click("下一题", find_button(frame, "下一题").center)
        clicks, unchanged_reads, pending_image, target_seen = 1, 0, None, False
        expected, reason = question.number + 1, ""
        for attempt in range(self.attempts):
            self.stop.pause(max(.3, self.delay))
            frame = self.frame("next_question", expected, attempt + 1, image=pending_image)
            pending_image = None
            try:
                number, total, _ = page_metadata(frame)
            except RecognitionError as exc:
                # 过渡画面或未知题号只能等待，不能再次点击。
                unchanged_reads = 0
                reason = str(exc)
                self.record_check("next_wait", frame, reason=reason, attempt=attempt + 1)
                continue
            if total is not None and total != question.total:
                raise RecognitionError("切换后的题目总数改变了。")
            if number == expected:
                target_seen = True
                unchanged_reads = 0
                try:
                    fresh = parse_question(frame, expected_options=self.expected_options,
                                           question_type=self.question_type)
                    self.debug.event("next_completed", f"已进入第 {expected} 题", clicks=clicks,
                                     number=expected, question=asdict(fresh),
                                     frame_id=getattr(frame, "debug_id", None))
                    return fresh, frame
                except RecognitionError as exc:
                    # 新题已经到了；选项尚未读完整也不能再点而跳过这一题。
                    reason = str(exc)
                    self.record_check("question_retry", frame, reason=reason, attempt=attempt + 1)
                    continue
            if number is not None and number != question.number:
                self.record_check("next_number_conflict", frame, question,
                                  reason="切换后出现非预期题号。", snapshot=True)
                raise RecognitionError(f"等待第 {expected} 题，却进入第 {number} 题，已停止以免跳题。")
            if (number is None or has_answer_dialog(frame) or has_correct_feedback(frame)
                    or has_wrong_feedback(frame) or getattr(frame, "feedback_overlay_active", False)):
                unchanged_reads = 0
                reason = "等待题号可读且弹窗、提示消失后切换下一题。"
                self.debug.event("next_wait", reason, number=expected, attempt=attempt + 1,
                                 frame_id=getattr(frame, "debug_id", None))
                continue
            if target_seen:
                unchanged_reads = 0
                reason = f"第 {expected} 题已出现，等待新题完整识别；不再补点。"
                self.debug.event("next_wait", reason, number=expected, attempt=attempt + 1)
                continue
            unchanged_reads += 1
            reason = f"等待第 {expected} 题，当前仍是第 {question.number} 题。"
            self.debug.event("next_wait", reason, number=expected, attempt=attempt + 1,
                             clicks=clicks, frame_id=getattr(frame, "debug_id", None))
            if unchanged_reads < 2 or clicks >= NEXT_MAX_CLICKS or attempt + 1 >= self.attempts:
                continue
            try:
                button = find_button(frame, "下一题")
            except RecognitionError as exc:
                reason = str(exc)
                unchanged_reads = 0
                self.record_check("next_wait", frame, reason=reason, attempt=attempt + 1)
                continue
            # OCR 可能耗时数秒；补点前再取得最新像素。若界面已改变，先读新图，
            # 不拿过期题号补点。尺寸改变也只触发重新读取，不作报错确认条件。
            current_image = self.desktop.screenshot()
            if current_image.size != frame.image.size or current_image.tobytes() != frame.image.tobytes():
                pending_image = current_image
                unchanged_reads = 0
                self.debug.event("next_refresh", "补点前界面已变化，先读取新画面", number=expected)
                continue
            self.last_frame = frame
            self.debug.capture(frame, "next_retry")
            self.debug.event("next_retry", "仍在原题，重新点击下一题", number=question.number,
                             target=expected, click_number=clicks + 1,
                             point=list(button.center), frame_id=getattr(frame, "debug_id", None))
            self.log(f"仍在第 {question.number} 题，补点下一题（第 {clicks + 1}/{NEXT_MAX_CLICKS} 次）。")
            self.click("下一题", button.center)
            clicks += 1
            unchanged_reads = 0
        self.record_check("next_failed", frame, question, reason=reason, snapshot=True)
        raise RecognitionError(reason or f"切换到第 {expected} 题超时。")

    def recover_answer_frame(self, question: Question, frame: Frame) -> Frame | None:
        """对同一张截图的原题干区域重 OCR；恢复文字来自当前画面，不补入旧题干。"""
        if self.question_frame is None:
            return None
        if (frame.width, frame.height) != (self.question_frame.width, self.question_frame.height):
            self.debug.event("answer_recovery_skipped", "截图尺寸变化，无法使用之前的题干区域。")
            return None
        try:
            number, total, _ = page_metadata(frame)
        except RecognitionError as exc:
            self.debug.event("answer_recovery_skipped", str(exc))
            return None
        if ((number is not None and number != question.number)
                or (total is not None and total != question.total)):
            self.debug.event("answer_recovery_skipped", "当前题号或总数冲突，保留原来的停止检查。")
            return None
        _, _, reference_lines = stem_parts(self.question_frame)
        if not reference_lines:
            return None
        heights = sorted(line.y2 - line.y1 for line in reference_lines)
        height = heights[len(heights) // 2]
        header_bottom = max((line.y2 for line in practice_header_lines(self.question_frame)
                             if line.y2 < reference_lines[0].y1), default=0)
        box = (max(0, int(min(line.x1 for line in reference_lines) - height)),
               max(int(header_bottom + 2), 0, int(min(line.y1 for line in reference_lines) - 1.4 * height)),
               min(frame.width, int(max(line.x2 for line in reference_lines) + height + 1)),
               min(frame.height, int(max(line.y2 for line in reference_lines) + 0.55 * height + 1)))
        if box[2] <= box[0] or box[3] <= box[1]:
            return None
        from PIL import ImageEnhance, ImageOps
        original_crop = frame.image.crop(box)
        source_id = getattr(frame, "debug_id", None)
        for attempt, method in enumerate(("plain", "contrast"), start=1):
            self.stop.check()
            self.debug.event("answer_recovery_start", "单独重读当前截图的题干区域",
                             number=question.number, source_frame_id=source_id, box=list(box),
                             method=method, attempt=attempt, scale=2)
            try:
                crop = original_crop if method == "plain" else ImageEnhance.Contrast(
                    ImageOps.grayscale(original_crop)).enhance(1.5).convert("RGB")
                piece = self.engine.read(crop, scale=2)
                translated = [replace(line, x1=line.x1 + box[0], x2=line.x2 + box[0],
                                      y1=line.y1 + box[1], y2=line.y2 + box[1]) for line in piece.lines]
                context = Frame(frame.image, [*frame.lines, *translated])

                def metadata_line(line: OCRLine) -> bool:
                    return frame_metadata_line(frame, line)

                # 页码可能落在题干矩形内；保留当前原图的元数据，不以裁切 OCR 覆盖。
                translated = [line for line in translated if not frame_metadata_line(context, line)]
                kept = [line for line in frame.lines if metadata_line(line) or not (
                    box[0] <= line.center[0] <= box[2] and box[1] <= line.center[1] <= box[3])]
                recovered = Frame(frame.image, sorted([*kept, *translated],
                                  key=lambda line: (line.center[1], line.x1)))
                self.debug.frame(recovered, "answer_recovery", question.number, attempt)
                recovered_number, recovered_total, _ = page_metadata(recovered)
                if (recovered_number is not None and recovered_number != question.number):
                    raise RecognitionError("区域重识别的题号与原题不一致。")
                if (recovered_total is not None and recovered_total != question.total):
                    raise RecognitionError("区域重识别的总题数与原题不一致。")
                _, recovered_stem, used = stem_parts(recovered)
                if any(line.confidence < 0.60 for line in used):
                    raise RecognitionError("题干区域重识别置信度过低，不能确认完整题目。")
                if (recovered_number is None
                        and len(re.findall(r"\w", stem_identity(recovered_stem), flags=re.UNICODE)) < 12):
                    raise RecognitionError("区域重识别未显示题号，题干不足以确认同一道题。")
                if not stem_rows_covered(reference_lines, used):
                    raise RecognitionError("区域重识别漏掉了原图中的完整题干行。")
                if not complete_recovered_stem(question.stem, recovered_stem):
                    raise RecognitionError("区域重识别仍未恢复完整题干，不能仅凭剩余片段匹配答案。")
                self.record_check("answer_recovered", recovered, question,
                                  reason="从当前截图读回完整题干，同题校验通过。", snapshot=True)
                self.debug.event("answer_recovery_result", "题干重读成功", source_frame_id=source_id,
                                 frame_id=getattr(recovered, "debug_id", None), box=list(box), method=method)
                self.last_frame = recovered
                return recovered
            except Stopped:
                raise
            except Exception as exc:
                self.debug.event("answer_recovery_rejected", str(exc), method=method, attempt=attempt,
                                 box=list(box), source_frame_id=source_id, traceback=traceback.format_exc())
        return None

    def recover_visible_answer_frame(self, question: Question, frame: Frame) -> Frame | None:
        """答案弹窗或正确提示真遮挡时，仅重读上下方完整可见内容。"""
        reference = self.question_frame
        if reference is None:
            return None
        dialog = find_answer_dialog(frame, reference)
        overlay_kind = "answer_dialog"
        covered_rows = covered_stem_rows
        if dialog is None:
            dialog = find_correct_toast(frame, reference)
            overlay_kind = "correct_feedback"
            covered_rows = covered_toast_stem_rows
            if dialog is None:
                return None
        _, _, reference_rows = stem_parts(reference)
        covered = covered_rows(frame, reference, dialog, reference_rows)
        if not covered:
            return None
        number, total, _ = page_metadata(frame)
        if (number, total) != (question.number, question.total):
            raise RecognitionError("弹窗遮挡时必须识别到与原题一致的题号及总题数。")
        header = next((line for line in practice_header_lines(frame)
                       if line.y2 < reference_rows[0].y1), None)
        if header is not None and (header.confidence < 0.60
                                   or practice_bank_name(header.text) != question.bank_name):
            raise RecognitionError("弹窗画面的题库名称与原题不一致或置信度过低。")
        body_box = stem_region_box(reference)
        if body_box is None:
            raise RecognitionError("无法定位弹窗前的题干区域。")
        cutoff = min(dialog[1] - 2, int(min(reference_rows[index].y1 for index in covered) - 4))
        safe_rows = [line for line in reference_rows if line.y2 <= cutoff]
        if not safe_rows or safe_rows[0] is not reference_rows[0]:
            raise RecognitionError("弹窗上方没有足够的完整题干行。")
        upper_box = (body_box[0], body_box[1], body_box[2], cutoff)
        if cutoff <= body_box[1]:
            raise RecognitionError("弹窗上方的题干识别区域为空。")
        self.stop.check()
        self.debug.event("visible_answer_recovery_start", "仅重读弹窗上方题干和下方选项",
                         number=question.number, source_frame_id=getattr(frame, "debug_id", None),
                         dialog_box=list(dialog), upper_box=list(upper_box), covered_rows=list(covered),
                         overlay_kind=overlay_kind)
        passes = [*frame.ocr_passes, {"method": "full_frame", "ocr": [asdict(line) for line in frame.lines]}]
        frame.ocr_passes = passes

        def read_region(box, method, scale=2):
            self.stop.check()
            try:
                return self.engine.read(frame.image.crop(box), scale=scale)
            except Stopped:
                raise
            except Exception as exc:
                passes.append({"method": method, "box": list(box), "scale": scale,
                               "error": str(exc), "traceback": traceback.format_exc()})
                raise RecognitionError(f"弹窗可见区域识别失败：{exc}") from exc

        upper_piece = read_region(upper_box, "visible_stem")
        upper_lines = [replace(line, x1=line.x1 + upper_box[0], x2=line.x2 + upper_box[0],
                               y1=line.y1 + upper_box[1], y2=line.y2 + upper_box[1])
                       for line in upper_piece.lines]
        upper_lines = [line for line in upper_lines if not frame_metadata_line(frame, line)]
        passes.append({"method": "visible_stem", "box": list(upper_box), "scale": 2,
                       "ocr": [asdict(line) for line in upper_lines]})
        _, _, old_stem_rows = stem_parts(frame)
        old_row_ids = {id(line) for line in old_stem_rows}
        kept = [line for line in frame.lines if id(line) not in old_row_ids]
        recovered = Frame(frame.image, sorted([*kept, *upper_lines],
                          key=lambda line: (line.center[1], line.x1)), ocr_passes=passes)
        self.debug.frame(recovered, "visible_answer_stem", question.number)
        _, visible_stem, used = stem_parts(recovered)
        if (len(re.findall(r"\w", stem_identity(visible_stem), flags=re.UNICODE)) < 12
                or any(line.confidence < 0.60 for line in used)):
            raise RecognitionError("弹窗上方的完整题干过短或识别置信度过低。")
        # 使用确实完整可见的参考行确定正文范围，不能按本次OCR长度裁短原题。
        _, expected_visible_stem, _ = stem_parts(Frame(reference.image, safe_rows))
        visible_similarity = stem_similarity(expected_visible_stem, visible_stem)
        passes[-1].update(expected_visible_stem=expected_visible_stem,
                          stem_similarity=visible_similarity,
                          stem_match_threshold=STEM_MATCH_THRESHOLD)
        if not stem_matches(expected_visible_stem, visible_stem):
            raise RecognitionError("弹窗上方的题干与原题不一致。")
        if any(not any(line.y1 - (line.y2 - line.y1) * 0.35 <= old.center[1]
                       <= line.y2 + (line.y2 - line.y1) * 0.35 for line in used)
               for old in safe_rows):
            raise RecognitionError("弹窗上方重读漏掉了完整可见的题干行。")

        visible_option_rows = [line for line in reference.lines
                               if OPTION_RE.match(line.text) and line.y1 >= dialog[3] + 2]
        expected_labels = [unicodedata.normalize("NFKC", OPTION_RE.match(line.text).group(1)).upper()
                           for line in visible_option_rows]
        lower_box = None
        checked_options = {}
        if visible_option_rows:
            first = min(visible_option_rows, key=lambda line: line.y1)
            start_y = max(dialog[3] + 2, int(first.y1 - (first.y2 - first.y1) * 0.4))
            bottom = min((int(line.y1 - 2) for line in reference.lines
                          if any(text in compact(line.text) for text in
                                 ("下一题", "查看答案", "完成练习"))), default=frame.height)
            lower_box = (0, start_y, frame.width, bottom)
            lower_scale = 2
            if overlay_kind == "correct_feedback":
                # 复用选题前已由两种设置确认的完整选项区域，避免换裁剪上下文再读错字。
                confirmed = next((item for item in reversed(reference.ocr_passes)
                                  if item.get("method") == "options_plain"), None)
                enhanced = next((item for item in reversed(reference.ocr_passes)
                                 if item.get("method") == "options_contrast"), None)
                if confirmed is not None and enhanced is not None:
                    box = tuple(confirmed["box"])
                    if (box == tuple(enhanced["box"]) and box[1] >= dialog[3] + 2
                            and all(box[0] <= line.x1 < line.x2 <= box[2]
                                    and box[1] <= line.y1 < line.y2 <= box[3]
                                    for line in visible_option_rows)):
                        lower_box, lower_scale = box, confirmed["scale"]
                        start_y, bottom = box[1], box[3]
            self.stop.check()
            lower_piece = read_region(lower_box, "visible_options", lower_scale)
            lower_lines = [replace(line, x1=line.x1 + lower_box[0], x2=line.x2 + lower_box[0],
                                   y1=line.y1 + start_y, y2=line.y2 + start_y)
                           for line in lower_piece.lines]
            passes.append({"method": "visible_options", "box": list(lower_box), "scale": lower_scale,
                           "ocr": [asdict(line) for line in lower_lines]})
            lower_frame = Frame(frame.image, lower_lines)
            self.debug.frame(lower_frame, "visible_answer_options", question.number)
            active = None
            for line in sorted(lower_lines, key=lambda line: (line.center[1], line.x1)):
                if line.confidence < 0.60:
                    raise RecognitionError("弹窗下方选项识别置信度过低。")
                match = OPTION_RE.match(line.text)
                if match:
                    active, text = match.groups()
                    active = unicodedata.normalize("NFKC", active).upper()
                    if active not in expected_labels or active in checked_options:
                        raise RecognitionError("弹窗下方选项标签与原题不一致或出现重复。")
                    checked_options[active] = text.strip()
                elif active is not None:
                    checked_options[active] += line.text.strip()
                else:
                    raise RecognitionError("弹窗下方可见选项未识别到标签。")
            if set(checked_options) != set(expected_labels):
                raise RecognitionError("弹窗下方可见选项没有识别完整。")
            if any(ocr_text_identity(text) != ocr_text_identity(question.options[label])
                   for label, text in checked_options.items()):
                raise RecognitionError("弹窗下方可见选项文字与原题不一致。")
            recovered.lines = sorted([
                *(line for line in recovered.lines if not start_y <= line.center[1] <= bottom),
                *lower_lines], key=lambda line: (line.center[1], line.x1))
        self.stop.check()
        verify_answer_question(recovered, question)
        self.debug.frame(recovered, "visible_answer_verified", question.number)
        self.debug.event("visible_answer_verified", "可见题干前缀及下方选项均与原题一致",
                         number=question.number, dialog_box=list(dialog), upper_box=list(upper_box),
                         lower_box=list(lower_box) if lower_box else None,
                         covered_rows=list(covered), checked_options=checked_options,
                         visible_stem=visible_stem, expected_visible_stem=expected_visible_stem,
                         stem_similarity=visible_similarity, stem_match_threshold=STEM_MATCH_THRESHOLD,
                         frame_id=getattr(recovered, "debug_id", None),
                         overlay_kind=overlay_kind)
        self.record_check("visible_answer_recovered", recovered, question,
                          reason="只使用确实未遮挡的题干行核对；已遮行没有填入旧文字。", snapshot=True)
        self.last_frame = recovered
        return recovered

    def read_verified_answer(self, question: Question, frame: Frame,
                             selected_label: str) -> tuple[list[str], Frame]:
        """直接匹配一次答案；不重读被弹窗遮挡的正文来确认原题。"""
        self.check_current_page(question, frame, require_number=True)
        if (question.question_type == "multiple" and has_correct_feedback(frame)
                and not has_answer_dialog(frame)
                and not getattr(frame, "multiple_selection_verified", False)):
            # 提示只表示当前整个勾选集合正确；被遮住的旧勾不能被省略为单个 A。
            restored = self.restore_reveal_question(question)
            if (has_wrong_feedback(restored) or has_answer_dialog(restored)
                    or selected_option_labels(restored, question) != {selected_label}):
                raise RecognitionError("多选正确提示对应的勾选与本次选择不一致，不能只保存一个选项。")
        return _parse_answer_labels(frame, question, selected_label), frame

    def select_for_collection(self, question: Question, frame: Frame) -> tuple[str, Frame]:
        selected_label = next(iter(question.options))
        if question.question_type != "multiple":
            self.click(f"选择 {selected_label}", question.option_points[selected_label])
            return selected_label, self.feedback_frame("after_selection", question)
        # 多选框是切换状态。重新采集已答过的题时，不能把旧勾选一起算作本次答案。
        for attempt in range(3):
            self.check_current_page(question, frame, require_number=True)
            selected = selected_option_labels(frame, question)
            self.debug.event("multiple_selection", "将多选勾选调整为一个已知选项",
                             number=question.number, selected=sorted(selected),
                             desired=[selected_label], attempt=attempt + 1)
            for label in question.options:
                if label in selected and label != selected_label:
                    self.click(f"取消选择 {label}", question.option_points[label])
                    self.stop.pause(min(.15, self.delay))
            if selected_label not in selected:
                self.click(f"选择 {selected_label}", question.option_points[selected_label])
            frame = self.feedback_frame("after_selection", question)
            if has_answer_dialog(frame) or has_wrong_feedback(frame):
                return selected_label, frame
            if has_correct_feedback(frame):
                correct_frame = frame
                frame = self.restore_reveal_question(question)
                if has_answer_dialog(frame) or has_wrong_feedback(frame):
                    return selected_label, frame
                if selected_option_labels(frame, question) == {selected_label}:
                    correct_frame.multiple_selection_verified = True
                    return selected_label, correct_frame
                continue
            if selected_option_labels(frame, question) == {selected_label}:
                return selected_label, frame
        raise RecognitionError("多选框未成功切换为本次选择，已停止以免将旧勾选误记为正确答案。")

    def wait_answer(self, question: Question, selected_label: str,
                    wrong_seen: bool = False, initial_frame: Frame | None = None) -> tuple[list[str], Frame, bool]:
        if selected_label not in question.options:
            raise RecognitionError("没有本次实际点击的选项记录，无法判断正确答案。")
        reason = ""
        trusted_feedback = None
        dialog_seen = False
        round_number, attempt, relocations = 1, 0, 0
        if initial_frame is None:
            frames, frame = self.observe_reveal_feedback(question, round_number)
        else:
            # 某些页面选中后就显示答案弹窗，直接读取，避免在弹窗下点按钮。
            frames, frame = [initial_frame], initial_frame
        while attempt < self.attempts:
            wrong_now = any(has_wrong_feedback(item) for item in frames)
            wrong_seen = wrong_seen or wrong_now
            dialogs = [item for item in frames if has_answer_dialog(item)]
            dialog_seen = dialog_seen or bool(dialogs)
            if wrong_now:
                self.debug.event("answer_wrong_latched", "检测到错误，取消无错误推断",
                                 number=question.number, round=round_number,
                                 frame_ids=[getattr(item, "debug_id", None)
                                            for item in frames if has_wrong_feedback(item)])
            # 明确弹窗优先；无错误推断不会替代已经显示的正确答案内容。
            candidate = dialogs[-1] if dialogs else next(
                (item for item in reversed(frames) if has_correct_feedback(item)), None)
            if not dialogs and not wrong_seen and not dialog_seen:
                if getattr(frame, "feedback_capture_incomplete", False):
                    raise RecognitionError("查看答案期间提示变化过多，无法完成错误提示核对。")
                if candidate is not None:
                    answer, verified = self.read_verified_answer(question, candidate, selected_label)
                    if answer != [selected_label]:
                        raise RecognitionError("正确提示与本次选择不一致。")
                    trusted_feedback = verified
                    self.debug.event("reveal_correct_observed", "本轮识别到正确提示，继续完成多次查看",
                                     number=question.number, round=round_number,
                                     frame_id=getattr(verified, "debug_id", None))
                if has_correct_feedback(frame) or getattr(frame, "feedback_overlay_active", False):
                    frame = self.restore_reveal_question(question)
                    if has_wrong_feedback(frame) or has_answer_dialog(frame):
                        frames = [frame]
                        continue
                self.check_current_page(question, frame, require_number=True)
                self.last_frame = frame
                button = find_button(frame, "查看答案")
                if (self.reveal_point is not None
                        and (abs(button.center[0] - self.reveal_point[0]) > max(8, (button.x2 - button.x1) / 2)
                             or abs(button.center[1] - self.reveal_point[1]) > max(8, (button.y2 - button.y1) / 2))):
                    # 点击后布局变化说明旧坐标可能落空；只重定位按钮，不核对正文或尺寸。
                    relocations += 1
                    if relocations > self.attempts:
                        raise RecognitionError("查看答案按钮持续移动，无法完成点击。")
                    self.debug.event("reveal_button_relocated", "查看答案按钮移动，使用新位置补点",
                                     number=question.number, round=round_number,
                                     previous_point=list(self.reveal_point), point=list(button.center))
                    self.reveal_answer(question, frame)
                    frames, frame = self.observe_reveal_feedback(question, round_number)
                    continue
                self.debug.event("answer_no_wrong_round", "本轮未检测到错误提示",
                                 number=question.number, round=round_number,
                                 required_rounds=REVEAL_CONFIRMATION_ROUNDS,
                                 selected_label=selected_label, frame_id=getattr(frame, "debug_id", None))
                if round_number >= REVEAL_CONFIRMATION_ROUNDS:
                    if (question.question_type == "multiple"
                            and selected_option_labels(frame, question) != {selected_label}):
                        raise RecognitionError("多选勾选与本次选择不一致，不能默认所选选项正确。")
                    evidence = trusted_feedback if trusted_feedback is not None else frame
                    source = "correct_feedback" if trusted_feedback is not None else "no_wrong_feedback"
                    evidence.answer_source = source
                    evidence.reveal_confirmation_rounds = round_number
                    self.debug.event("answer_verified", "多次查看答案未出现错误，按本次选项正确保存",
                                     number=question.number, answer=[selected_label],
                                     selected_label=selected_label, rounds=round_number,
                                     answer_source=source, frame_id=getattr(evidence, "debug_id", None))
                    return [selected_label], evidence, False
                self.reveal_answer(question, frame)
                round_number += 1
                frames, frame = self.observe_reveal_feedback(question, round_number)
                continue
            attempt += 1
            try:
                if candidate is None or (not dialogs and wrong_seen):
                    raise RecognitionError("已检测到错误提示，等待弹窗中的正确答案，不能默认所选选项正确。")
                frame = candidate
                answer, frame = self.read_verified_answer(question, frame, selected_label)
                dismiss = any(compact(line.text) == "知道了" for line in frame.lines)
                if has_correct_feedback(frame):
                    if dialog_seen and not dialogs:
                        raise RecognitionError("已经出现明确答案弹窗，不能改用正确提示。")
                    if dismiss:
                        find_button(frame, "知道了")
                    # 正确提示持续时间短，一次可靠识别后立即保存，不要求连续两帧。
                    self.debug.event("answer_verified", "正确提示已确认", number=question.number,
                                     answer=answer, attempt=attempt, frame_id=getattr(frame, "debug_id", None))
                    return answer, frame, dismiss
                find_button(frame, "知道了")
                self.debug.event("answer_verified", "答案弹窗已读取", number=question.number,
                                 answer=answer, attempt=attempt, frame_id=getattr(frame, "debug_id", None))
                return answer, frame, True
            except RecognitionError as exc:
                reason = str(exc)
                self.record_check("answer_retry", frame, question, reason=reason, attempt=attempt,
                                  snapshot=True)
                self.log(f"答案识别第 {attempt}/{self.attempts} 轮失败：{reason}")
            if attempt < self.attempts:
                self.stop.pause(self.delay)
                frame = self.frame("wait_answer", question.number, attempt + 1)
                frames = [frame]
        raise RecognitionError(reason or "未读取到答案弹窗。")

    def run(self, max_new: int = 0) -> Path | None:
        # 覆盖模式下，更新已有题也计入本次采集数量。
        collected_count = 0
        run_error = None
        path = None
        self.log(f"调试日志目录：{self.debug.directory}")
        self.debug.event("run_start", "开始采集", max_new=max_new, overwrite=self.overwrite)
        try:
            initial = self.frame("initial")
            if any(compact(line.text) == "知道了" for line in initial.lines):
                self.log("关闭当前答案弹窗，重新读取题目。")
                self.click("知道了", find_button(initial, "知道了").center)
                initial = None
            question, frame = self.stable_question(initial_frame=initial)
            self.on_progress(len(self.store.records), question.total)
            while True:
                self.stop.check()
                self.question_frame = frame
                saved = self.store.validate(question, overwrite=self.overwrite)
                if saved and not self.overwrite:
                    self.log(f"第 {question.number}/{question.total} 题已保存，继续下一题。")
                else:
                    action = "重新采集" if saved else "采集"
                    self.log(f"{action}第 {question.number}/{question.total} 题，已识别 {len(question.options)} 个选项。")
                    q_path, a_path = self.store.evidence_paths(question.number, overwrite=self.overwrite)
                    save_frame(frame, q_path)
                    self.debug.event("question_captured", f"第 {question.number} 题截图已保存",
                                     question=asdict(question), screenshot=str(q_path),
                                     frame_id=getattr(frame, "debug_id", None))
                    # 记录本次实际点击的标签，用于正确提示和多轮无错误反馈判定。
                    self.reveal_point = None
                    selected_label, selected_frame = self.select_for_collection(question, frame)
                    wrong_before_reveal = has_wrong_feedback(selected_frame)
                    if has_correct_feedback(selected_frame) and not wrong_before_reveal:
                        try:
                            answer, selected_frame = self.read_verified_answer(question, selected_frame, selected_label)
                        except RecognitionError as exc:
                            self.record_check("feedback_failed", selected_frame, question,
                                              reason=str(exc), snapshot=True)
                            raise
                        answer_frame = selected_frame
                        dismiss = any(compact(line.text) == "知道了" for line in selected_frame.lines)
                        self.debug.event("answer_verified", "选中后答题正确提示已确认",
                                         number=question.number, answer=answer, selected_label=selected_label,
                                         frame_id=getattr(selected_frame, "debug_id", None))
                    else:
                        if wrong_before_reveal:
                            self.debug.event("answer_wrong_latched", "选中后检测到错误，查看答案时保留该结果",
                                             number=question.number,
                                             frame_id=getattr(selected_frame, "debug_id", None))
                        if has_answer_dialog(selected_frame):
                            answer, answer_frame, dismiss = self.wait_answer(
                                question, selected_label, wrong_seen=wrong_before_reveal,
                                initial_frame=selected_frame)
                        else:
                            if wrong_before_reveal:
                                selected_frame = self.restore_reveal_question(question, return_feedback=False)
                            self.reveal_answer(question, selected_frame)
                            answer, answer_frame, dismiss = self.wait_answer(question, selected_label,
                                                                            wrong_seen=wrong_before_reveal)
                    direct = has_correct_feedback(answer_frame)
                    answer_source = getattr(answer_frame, "answer_source",
                                            "correct_feedback" if direct else "answer_dialog")
                    save_frame(answer_frame, a_path)
                    previous_backup = self.store.backup_path
                    self.store.add(question, answer, q_path, a_path,
                                   answer_source=answer_source,
                                   overwrite=self.overwrite)
                    self.debug.event("question_saved", f"第 {question.number} 题 JSON 已提交",
                                     number=question.number, answer=answer, selected_label=selected_label,
                                     overwritten=saved, question_screenshot=str(q_path), answer_screenshot=str(a_path),
                                     answer_source=answer_source)
                    if self.store.backup_path != previous_backup:
                        self.log(f"覆盖前的 JSON 已备份：{self.store.backup_path}")
                    collected_count += 1
                    if direct:
                        rounds = getattr(answer_frame, "reveal_confirmation_rounds", 0)
                        if rounds:
                            self.log(f"第 {question.number} 题已查看答案 {rounds} 次且未出现错误，"
                                     f"保留正确提示并保存已选选项 {selected_label}。")
                        else:
                            self.log(f"第 {question.number} 题答题正确，直接保存已选选项 {selected_label}。")
                    elif answer_source == "no_wrong_feedback":
                        self.log(f"第 {question.number} 题已查看答案 {REVEAL_CONFIRMATION_ROUNDS} 次，"
                                 f"均未检测到错误，按已选选项 {selected_label} 正确保存。")
                    result = "已覆盖原记录" if saved else "已保存"
                    self.log(f"第 {question.number} 题{result}，正确答案：{'、'.join(answer)}。")
                    self.on_progress(len(self.store.records), question.total)
                    if collected_count % 10 == 0:
                        exported = self.store.export()
                        self.debug.event("word_exported", "定期导出 Word", path=str(exported))
                    # 瞬时正确提示会自行消失；仅答案弹窗需要点击“知道了”。
                    self.stop.check()
                    if dismiss:
                        self.last_frame = answer_frame
                        self.click("知道了", find_button(answer_frame, "知道了").center)
                    frame = answer_frame
                if question.number == question.total or (max_new > 0 and collected_count >= max_new):
                    break
                question, frame = self.next_question(question, frame)
            total = question.total
            missing = [number for number in range(1, total + 1) if number not in self.store.records]
            if missing:
                self.log(f"本次采集结束，共保存 {len(self.store.records)}/{total} 题。缺少题号："
                         + "、".join(map(str, missing[:20])) + ("……" if len(missing) > 20 else ""))
            else:
                self.log(f"采集完成，题库共 {total} 题。")
        except Exception as exc:
            run_error = exc
            self.debug.event("run_failed", str(exc), exception_type=type(exc).__name__,
                             traceback=traceback.format_exc(), collected_count=collected_count)
            self.debug.capture(self.last_frame, "stopped")
            self.debug.capture(self.question_frame, "before_selection")
            if self.last_frame is not None:
                try:
                    save_frame(self.last_frame, self.store.output_dir / "diagnostics" /
                               f"{datetime.now():%Y%m%d_%H%M%S_%f}_stopped.png")
                except Exception as diagnostic_error:
                    self.debug.event("diagnostic_error", str(diagnostic_error), stage="legacy_stopped_snapshot")
            self.log(str(exc))
            raise
        finally:
            try:
                path = self.store.export()
                if path:
                    self.log(f"Word 已导出：{path}")
                    self.debug.event("word_exported", "结束时导出 Word", path=str(path))
            except Exception as export_error:
                self.debug.event("export_failed", str(export_error), traceback=traceback.format_exc())
                self.log(f"Word 导出失败：{export_error}")
                if run_error is None:
                    run_error = export_error
                    raise
            finally:
                try:
                    self.desktop.close()
                except Exception as close_error:
                    self.debug.event("desktop_close_failed", str(close_error))
                self.debug.event("run_end", "本次采集结束", status="failed" if run_error else "completed",
                                 collected_count=collected_count, saved_count=len(self.store.records))
                self.log(f"本次调试日志：{self.debug.path}")
        return path


def from_images(question_path: Path, answer_path: Path, output: Path, overwrite: bool = False,
                question_type: str | None = None) -> Path:
    from PIL import Image
    debug = DebugLog(question_output_dir(output, question_type) if question_type else output,
                     settings={"mode": "images", "question_type": question_type, "question_image": str(question_path),
                                      "answer_image": str(answer_path), "overwrite": overwrite}, warning=print)
    q_frame = a_frame = None
    try:
        debug.event("ocr_loading", "加载本地 OCR 模型")
        engine = OCREngine()
        q_frame = engine.read(Image.open(question_path).convert("RGB"))
        debug.frame(q_frame, "offline_question")
        a_frame = engine.read(Image.open(answer_path).convert("RGB"))
        bind_progress_occlusion(a_frame, q_frame)
        debug.frame(a_frame, "offline_answer")
        if a_frame.progress_occlusion:
            debug.event("progress_occluded", "忽略被答案弹窗遮挡的残缺页码",
                        frame_id=getattr(a_frame, "debug_id", None), **a_frame.progress_occlusion)
        question = parse_question(q_frame, question_type=question_type)
        debug.event("offline_comparison", "离线题干比对", **recognition_details(a_frame, question))
        answer = parse_answer(a_frame, question)
        store = BankStore(output, question_type=question.question_type)
        if not store.validate(question, overwrite=overwrite) or overwrite:
            q_dest, a_dest = store.evidence_paths(question.number, overwrite=overwrite)
            save_frame(q_frame, q_dest)
            save_frame(a_frame, a_dest)
            store.add(question, answer, q_dest, a_dest, overwrite=overwrite)
            debug.event("question_saved", "离线识别已提交 JSON", number=question.number, answer=answer)
        path = store.export()
        debug.event("word_exported", "离线导出 Word", path=str(path))
        return path
    except Exception as exc:
        debug.event("offline_failed", str(exc), traceback=traceback.format_exc())
        debug.capture(q_frame, "offline_question")
        debug.capture(a_frame, "offline_answer")
        raise
    finally:
        print(f"调试日志：{debug.path}")


def main():
    parser = argparse.ArgumentParser(description="截图 OCR 采集 SoEasy 练习题并导出 Word")
    parser.add_argument("--config", type=Path, default=BASE / "config.json")
    parser.add_argument("--output", type=Path, default=BASE / "output")
    parser.add_argument("--images", nargs=2, type=Path, metavar=("QUESTION", "ANSWER"), help="离线识别题目图和答案图")
    parser.add_argument("--export", type=Path, metavar="JSON", help="从已经采集的题库 JSON 重新导出 Word")
    parser.add_argument("--max-new", type=int, default=0, help="本次最多采集几题，覆盖已有题也计数；0 表示不限")
    parser.add_argument("--overwrite", action="store_true", help="重新采集并覆盖同题号记录；其他题保留，覆盖前备份 JSON")
    parser.add_argument("--question-type", choices=tuple(QUESTION_TYPE_NAMES), default=None,
                        help="题型：single 单选、multiple 多选、true_false 判断；离线识别默认按标题判断")
    args = parser.parse_args()
    if args.images:
        print(from_images(*args.images, args.output, overwrite=args.overwrite, question_type=args.question_type))
    elif args.export:
        data = json.loads(args.export.read_text(encoding="utf-8"))
        question_type = data.get("question_type") or next(
            (mode for mode, name in QUESTION_TYPE_NAMES.items() if name in data.get("bank_name", "")), "single")
        if args.question_type is not None and args.question_type != question_type:
            parser.error("指定题型与 JSON 题型不一致，已取消导出以免覆盖其他题型的 Word。")
        target = question_output_dir(args.output, question_type) / WORD_FILENAMES[question_type]
        target.parent.mkdir(parents=True, exist_ok=True)
        export_docx(data, target)
        print(target)
    else:
        if os.name != "nt":
            parser.error("自动点击模式需要 Windows。")
        if not args.config.exists():
            parser.error("请先运行 app.py，框选小程序区域并保存配置。")
        enable_dpi_awareness()
        config = json.loads(args.config.read_text(encoding="utf-8"))
        question_type = args.question_type or config.get("question_type", "single")
        expected_options = (2 if question_type == "true_false" else
                            4 if question_type != config.get("question_type", "single") else
                            config.get("expected_options", 4))
        debug = DebugLog(question_output_dir(args.output, question_type), settings={**config,
                                               "question_type": question_type, "expected_options": expected_options,
                                               "output": str(args.output),
                                               "overwrite": args.overwrite, "max_new": args.max_new}, warning=print)
        print(f"调试日志目录：{debug.directory}")
        try:
            stop = StopControl()
            debug.event("ocr_loading", "加载本地 OCR 模型")
            engine = OCREngine()
            debug.event("ocr_ready", "OCR 模型已加载",
                        backend=getattr(getattr(engine, "engine", None), "metadata", {}))
            print("5 秒后开始，请将小程序置于前台，Esc 或鼠标移到屏幕左上角停止。")
            stop.pause(5)
            collector = Collector(Desktop(config["region"], stop), engine, BankStore(args.output, question_type=question_type),
                                  stop, delay=config.get("delay", 0.6),
                                  expected_options=expected_options,
                                  overwrite=args.overwrite, debug=debug, question_type=question_type)
            collector.run(args.max_new)
        except Exception as exc:
            debug.event("cli_failed", str(exc), traceback=traceback.format_exc())
            raise


if __name__ == "__main__":
    try:
        main()
    except Stopped as exc:
        print(exc)
    except Exception as exc:
        print(f"采集停止：{exc}")
        raise SystemExit(1)
