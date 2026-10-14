"""只用当前和选题前的截图，识别答案弹窗的真实像素边界。"""
from __future__ import annotations

import math
import re
import unicodedata


def _compact(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def _dialog_component(frame, reference_frame):
    """返回 (bbox, 连通域像素)，不截屏、点击或补写题干。"""
    import cv2
    import numpy as np

    if frame.image.size != reference_frame.image.size:
        return None
    answers = [line for line in frame.lines
               if "正确答案" in _compact(line.text) and line.confidence >= .90]
    acknowledgements = [line for line in frame.lines
                        if _compact(line.text) == "知道了" and line.confidence >= .90]
    if len(answers) != 1 or len(acknowledgements) != 1:
        return None
    answer, acknowledgement = answers[0], acknowledgements[0]
    if answer.y2 >= acknowledgement.y1:
        return None
    pixels = np.asarray(frame.image.convert("RGB"))
    previous = np.asarray(reference_frame.image.convert("RGB"))
    white = (pixels.min(axis=2) >= 250).astype(np.uint8)
    _, labels, stats, _ = cv2.connectedComponentsWithStats(white, 8)
    difference = np.abs(pixels.astype(np.int16) - previous.astype(np.int16)).max(axis=2)
    candidates = []
    for label, (x, y, width, height, area) in enumerate(stats[1:], 1):
        # 留出四边的采样像素；延伸到图像边缘的白背景不作为弹窗。
        if x <= 0 or y <= 0 or x + width >= frame.width or y + height >= frame.height:
            continue
        if not all(x <= anchor.x1 < anchor.x2 <= x + width and
                   y <= anchor.y1 < anchor.y2 <= y + height
                   for anchor in (answer, acknowledgement)):
            continue
        if area / (width * height) < .70:
            continue
        sides = [difference[y - 1, x:x + width],
                 difference[y + height, x:x + width],
                 difference[y:y + height, x - 1],
                 difference[y:y + height, x + width]]
        # 原题卡也是白色：其边界没有新出现，不能误当遮挡弹窗。
        if any((side >= 5).mean() < .40 for side in sides):
            continue
        if (np.concatenate(sides) >= 5).mean() < .60:
            continue
        box = (int(x), int(y), int(x + width), int(y + height))
        candidates.append((box, label, int(width * height)))
    if not candidates:
        return None
    box, label, _ = min(candidates, key=lambda candidate: candidate[2])
    return box, labels == label


def find_answer_dialog(frame, reference_frame):
    """返回绝对像素 exclusive bbox；无可靠新弹窗证据时返回 None。"""
    component = _dialog_component(frame, reference_frame)
    return component[0] if component is not None else None


def covered_stem_rows(frame, reference_frame, dialog_box, rows):
    """返回确有旧字墨被弹窗白色连通域覆盖的 rows 下标。

    rows 是调用方提供的选题前题干行，不从题干语义猜测遮挡位置。
    仅矩形相交或文本识别不同不足以认定有遮挡。
    """
    import numpy as np

    component = _dialog_component(frame, reference_frame)
    if component is None or component[0] != dialog_box:
        return ()
    _, panel_pixels = component
    previous = np.asarray(reference_frame.image.convert("RGB"))
    ink = previous.max(axis=2) < 160
    result = []
    for index, line in enumerate(rows):
        x1, y1 = max(0, math.floor(line.x1)), max(0, math.floor(line.y1))
        x2 = min(reference_frame.width, math.ceil(line.x2))
        y2 = min(reference_frame.height, math.ceil(line.y2))
        if x1 >= x2 or y1 >= y2:
            continue
        patch = ink[y1:y2, x1:x2]
        covered = patch & panel_pixels[y1:y2, x1:x2]
        if covered.sum() >= max(4, math.ceil(.02 * patch.sum())):
            result.append(index)
    return tuple(result)


def _white_check_above_anchor(pixels, box, anchor):
    """确认上方有两条斜臂组成的白色勾，避免只凭提示文字认面板。"""
    import cv2
    import numpy as np

    x1, y1, x2, y2 = box
    region = pixels[y1:y2, x1:x2]
    _, labels, stats, _ = cv2.connectedComponentsWithStats(
        (region.min(axis=2) >= 240).astype(np.uint8), connectivity=8)
    font_height = anchor.y2 - anchor.y1
    for label, (x, y, width, height, area) in enumerate(stats[1:], 1):
        if not (.8 * font_height <= width <= 2 * font_height
                and .5 * font_height <= height <= 1.5 * font_height
                and .08 <= area / (width * height) <= .50):
            continue
        if (y1 + y + height >= anchor.y1
                or abs(x1 + x + width / 2 - (x1 + x2) / 2) > .3 * font_height):
            continue
        component = labels[y:y + height, x:x + width] == label
        profile = np.array([np.median(np.nonzero(component[:, column])[0])
                            if component[:, column].any() else np.nan
                            for column in range(width)])
        if np.isnan(profile).any():
            continue
        vertex = int(np.argmax(profile))
        if not (.20 * width <= vertex <= .45 * width
                and profile[vertex] >= .65 * height
                and profile[vertex] - profile[0] >= .25 * height
                and profile[vertex] - profile[-1] >= .50 * height
                and profile[vertex] - profile[-1] >=
                    1.5 * (profile[vertex] - profile[0])):
            continue
        # 两臂必须近似直线；圆点、方框或大块白图案不作为正确图标。
        arms = (profile[:vertex + 1], profile[vertex:])
        if all(np.sqrt(np.mean((np.polyval(np.polyfit(np.arange(len(arm)), arm, 1),
                                            np.arange(len(arm))) - arm) ** 2))
               <= max(1.5, .10 * height) for arm in arms):
            return True
    return False


def _correct_toast_component(frame, reference_frame):
    """高可信正确文字、新灰色面板及其白勾须同时存在。"""
    import cv2
    import numpy as np

    if frame.image.size != reference_frame.image.size:
        return None
    anchors = [line for line in frame.lines if line.confidence >= .90 and
               re.fullmatch(r"[✓✔√☑✅/／]*答题正确[!！。]?", _compact(line.text))]
    if len(anchors) != 1:
        return None
    anchor = anchors[0]
    font_height = anchor.y2 - anchor.y1
    if font_height <= 0:
        return None
    pixels = np.asarray(frame.image.convert("RGB"))
    previous = np.asarray(reference_frame.image.convert("RGB"))
    low, high = pixels.min(axis=2), pixels.max(axis=2)
    gray = ((high - low <= 6) & (low >= 40) & (high <= 110)).astype(np.uint8)
    _, labels, stats, _ = cv2.connectedComponentsWithStats(gray, connectivity=8)
    difference = np.abs(pixels.astype(np.int16) - previous.astype(np.int16)).max(axis=2)
    candidates = []
    for label, (x, y, width, height, area) in enumerate(stats[1:], 1):
        if x <= 0 or y <= 0 or x + width >= frame.width or y + height >= frame.height:
            continue
        if not (.75 <= width / height <= 1.3
                and 4 * font_height <= width <= 9 * font_height
                and area / (width * height) >= .85):
            continue
        cx, cy = x + width / 2, y + height / 2
        if (abs(cx - frame.width / 2) > 1.5 * font_height
                or abs(cy - frame.height / 2) > 1.5 * font_height
                or abs(anchor.center[0] - cx) > .5 * font_height):
            continue
        if not (x + .5 * font_height <= anchor.x1 < anchor.x2 <= x + width - .5 * font_height
                and cy < anchor.y1 < anchor.y2 <= y + height - .4 * font_height):
            continue
        mask = labels == label
        mask_sides = (mask[y, x:x + width], mask[y + height - 1, x:x + width],
                      mask[y:y + height, x], mask[y:y + height, x + width - 1])
        difference_sides = (difference[y, x:x + width], difference[y + height - 1, x:x + width],
                            difference[y:y + height, x], difference[y:y + height, x + width - 1])
        if any(side.mean() < .60 or (values[side] >= 20).mean() < .80
               for side, values in zip(mask_sides, difference_sides)):
            continue
        if (difference[mask] >= 20).mean() < .85:
            continue
        box = (int(x), int(y), int(x + width), int(y + height))
        if not _white_check_above_anchor(pixels, box, anchor):
            continue
        candidates.append((box, mask, int(width * height)))
    if not candidates:
        return None
    box, mask, _ = min(candidates, key=lambda value: value[2])
    return box, mask & (difference >= 20)


def find_correct_toast(frame, reference_frame):
    """返回新出现的灰色正确提示 exclusive bbox；证据不足时返回 None。"""
    component = _correct_toast_component(frame, reference_frame)
    return component[0] if component is not None else None


def covered_toast_stem_rows(frame, reference_frame, toast_box, rows):
    """返回旧题干深色字墨被新灰色提示面板实际抹掉的行下标。"""
    import numpy as np

    component = _correct_toast_component(frame, reference_frame)
    if component is None or component[0] != toast_box:
        return ()
    _, new_gray_pixels = component
    ink = np.asarray(reference_frame.image.convert("RGB")).max(axis=2) < 160
    result = []
    for index, line in enumerate(rows):
        x1, y1 = max(0, math.floor(line.x1)), max(0, math.floor(line.y1))
        x2 = min(reference_frame.width, math.ceil(line.x2))
        y2 = min(reference_frame.height, math.ceil(line.y2))
        if x1 >= x2 or y1 >= y2:
            continue
        patch = ink[y1:y2, x1:x2]
        covered = patch & new_gray_pixels[y1:y2, x1:x2]
        if covered.sum() >= max(4, math.ceil(.02 * patch.sum())):
            result.append(index)
    return tuple(result)
