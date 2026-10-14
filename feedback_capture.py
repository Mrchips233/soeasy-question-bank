"""Cheap pixel candidates for freezing a transient feedback screenshot.

A candidate says only that a new centered gray panel appeared.  It must never
be used as answer or correctness evidence; the caller must OCR the frozen image
and validate its text and question identity separately.  This module does not
capture the desktop or invoke OCR.
"""
from dataclasses import dataclass
import math

import cv2
import numpy as np
from PIL import Image


@dataclass(frozen=True, slots=True)
class FeedbackCandidate:
    """Exclusive pixel bounds and geometry/change scores, without answer data."""

    bbox: tuple[int, int, int, int]
    score: float
    changed_fraction: float


def find_feedback_candidate(current_image: Image.Image,
                            reference_image: Image.Image) -> FeedbackCandidate | None:
    """Find a newly appeared centered, approximately square, dark gray panel.

    Only the central square is examined, at a bounded sampling resolution.  The
    real saved samples use a 170 px panel at window widths of 674 and 732 px;
    relative limits also allow resized windows.  Bounds may differ from the
    physical panel by up to one sampling step, so crop OCR should add padding.
    Equal sizes are required because a resized/moved capture is not a valid
    pixel reference.  A static panel and ordinary option selection return None.
    """
    if current_image.size != reference_image.size:
        return None
    image_width, image_height = current_image.size
    short_side = min(image_width, image_height)
    if short_side < 80:
        return None

    radius = math.ceil(.35 * short_side)
    center_x, center_y = image_width / 2, image_height / 2
    roi = (max(0, math.floor(center_x) - radius),
           max(0, math.floor(center_y) - radius),
           min(image_width, math.floor(center_x) + radius),
           min(image_height, math.floor(center_y) + radius))
    step = max(1, math.ceil(short_side / 400))
    pixels = np.asarray(current_image.crop(roi).convert("RGB"))[::step, ::step]
    low, high = pixels.min(axis=2), pixels.max(axis=2)
    gray = ((high - low <= 6) & (low >= 40) & (high <= 115)).astype(np.uint8)
    if np.count_nonzero(gray) < (.10 * short_side / step) ** 2 * .85:
        return None

    _, labels, stats, _ = cv2.connectedComponentsWithStats(gray, connectivity=8)
    previous = None
    candidates = []
    sampled_height, sampled_width = gray.shape
    for label, (x, y, width, height, area) in enumerate(stats[1:], 1):
        # A panel must have all its sides visible inside the cropped region.
        if x <= 0 or y <= 0 or x + width >= sampled_width or y + height >= sampled_height:
            continue
        physical_width, physical_height = width * step, height * step
        fill_fraction = float(area / (width * height))
        if not (.75 <= width / height <= 1.3
                and .10 * short_side <= physical_width <= .40 * short_side
                and .10 * short_side <= physical_height <= .40 * short_side
                and fill_fraction >= .85):
            continue
        panel_center_x = roi[0] + (x + width / 2) * step
        panel_center_y = roi[1] + (y + height / 2) * step
        if (abs(panel_center_x - center_x) > .10 * short_side
                or abs(panel_center_y - center_y) > .10 * short_side):
            continue

        component = labels[y:y + height, x:x + width] == label
        # Resizing/anti-aliasing can append a one-pixel gray fringe to a
        # component.  Look just inside that fringe for the first filled side.
        # Never bypass a filled but unchanged border to seek changed pixels.
        side_indices = []
        for dimension, reverse in ((0, False), (0, True), (1, False), (1, True)):
            length = component.shape[dimension]
            for inset in range(min(3, length)):
                index = length - 1 - inset if reverse else inset
                side = component[index] if dimension == 0 else component[:, index]
                if side.mean() >= .60:
                    side_indices.append((dimension, index))
                    break
        if len(side_indices) != 4:
            continue
        sides = [component[index] if dimension == 0 else component[:, index]
                 for dimension, index in side_indices]
        if previous is None:
            previous = np.asarray(reference_image.crop(roi).convert("RGB"))[::step, ::step]
        current_region = pixels[y:y + height, x:x + width].astype(np.int16)
        old_region = previous[y:y + height, x:x + width].astype(np.int16)
        changed = np.abs(current_region - old_region).max(axis=2) >= 20
        changed_fraction = float(changed[component].mean())
        if changed_fraction < .85:
            continue
        changed_sides = [changed[index] if dimension == 0 else changed[:, index]
                         for dimension, index in side_indices]
        edge_changes = [float(changed_side[side].mean())
                        for side, changed_side in zip(sides, changed_sides)]
        if min(edge_changes) < .80:
            continue

        bbox = (int(roi[0] + x * step), int(roi[1] + y * step),
                int(roi[0] + (x + width) * step),
                int(roi[1] + (y + height) * step))
        # This score describes pixels/geometry only, never OCR confidence.
        score = min(fill_fraction, changed_fraction, min(edge_changes))
        candidates.append(FeedbackCandidate(bbox, score, changed_fraction))
    return max(candidates, key=lambda candidate: candidate.score) if candidates else None
