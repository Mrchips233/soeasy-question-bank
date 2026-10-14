"""Bounded cache for OCR results from exactly identical input pixels.

Usage in an OCR reader:
    payload = cache.get(image, resolved_scale, whole_frame)
    if payload is not None:
        return Frame(image, **payload)
    frame = ...  # Perform OCR and any required validation/refinement.
    cache.put(image, resolved_scale, whole_frame, frame)

Keys retain normalized RGB bytes, never a PIL image.  Returned data is always
deep-copied; the caller supplies the new image and any transient debug fields.
"""
from __future__ import annotations

from collections import OrderedDict
import copy
import math
import threading
from time import perf_counter


class ExactOCRCache:
    """LRU bounded by entry count and normalized image-key bytes.

    Pixel bytes are compared exactly.  Size, resolved OCR scale and whether the
    reader performs full-frame refinement are also part of the key.  The byte
    budget covers image keys, while the entry limit bounds result payloads.
    """

    def __init__(self, capacity=32, max_bytes=64 * 1024 * 1024):
        if not isinstance(capacity, int) or capacity < 1:
            raise ValueError("OCR cache capacity must be a positive integer")
        if max_bytes is not None and (not isinstance(max_bytes, int) or max_bytes < 1):
            raise ValueError("OCR cache byte budget must be positive or None")
        self.capacity = capacity
        self.max_bytes = max_bytes
        self._entries = OrderedDict()
        self._cached_bytes = 0
        self._lock = threading.RLock()
        self.hits = self.misses = self.puts = self.evictions = 0
        self.skipped_errors = self.skipped_empty = self.skipped_oversized = 0
        self.key_time_seconds = self.lookup_time_seconds = 0.0
        self.copy_time_seconds = self.store_time_seconds = 0.0

    def _key(self, image, scale, whole_frame):
        started = perf_counter()
        if scale is not None:
            scale = float(scale)
            if not math.isfinite(scale) or scale <= 0:
                raise ValueError("OCR scale must be finite and positive")
        normalized = image.convert("RGB")
        key = (tuple(normalized.size), scale, bool(whole_frame), normalized.tobytes())
        with self._lock:
            self.key_time_seconds += perf_counter() - started
        return key

    def get(self, image, scale, whole_frame):
        """Return copied Frame keyword data, or None for a strict pixel miss."""
        started = perf_counter()
        key = self._key(image, scale, whole_frame)
        with self._lock:
            stored = self._entries.get(key)
            if stored is None:
                self.misses += 1
                self.lookup_time_seconds += perf_counter() - started
                return None
            self._entries.move_to_end(key)
            copy_started = perf_counter()
            result = copy.deepcopy(stored)
            self.copy_time_seconds += perf_counter() - copy_started
            self.hits += 1
            self.lookup_time_seconds += perf_counter() - started
            return result

    def put(self, image, scale, whole_frame, frame):
        """Cache successful nonempty OCR only; return whether it was stored."""
        started = perf_counter()
        lines = getattr(frame, "lines", None)
        passes = getattr(frame, "ocr_passes", [])
        error = getattr(frame, "refinement_error", "")
        with self._lock:
            if error or any(isinstance(item, dict) and item.get("error") for item in passes):
                self.skipped_errors += 1
                self.store_time_seconds += perf_counter() - started
                return False
            if not lines:
                self.skipped_empty += 1
                self.store_time_seconds += perf_counter() - started
                return False
        key = self._key(image, scale, whole_frame)
        key_bytes = len(key[-1])
        with self._lock:
            if self.max_bytes is not None and key_bytes > self.max_bytes:
                self.skipped_oversized += 1
                self.store_time_seconds += perf_counter() - started
                return False
            copy_started = perf_counter()
            payload = copy.deepcopy({"lines": lines, "ocr_passes": passes,
                                     "refinement_error": error})
            self.copy_time_seconds += perf_counter() - copy_started
            if key in self._entries:
                self._cached_bytes -= key_bytes
            self._entries[key] = payload
            self._entries.move_to_end(key)
            self._cached_bytes += key_bytes
            self.puts += 1
            while (len(self._entries) > self.capacity or
                   (self.max_bytes is not None and self._cached_bytes > self.max_bytes)):
                removed_key, _ = self._entries.popitem(last=False)
                self._cached_bytes -= len(removed_key[-1])
                self.evictions += 1
            self.store_time_seconds += perf_counter() - started
            return True

    def clear(self):
        """Release cached payloads and pixels, retaining cumulative statistics."""
        with self._lock:
            self._entries.clear()
            self._cached_bytes = 0

    def stats(self):
        """A JSON-safe snapshot; lookup/store timings include key/copy time."""
        with self._lock:
            return {"hits": self.hits, "misses": self.misses,
                    "lookups": self.hits + self.misses, "puts": self.puts,
                    "evictions": self.evictions, "entries": len(self._entries),
                    "cached_bytes": self._cached_bytes, "capacity": self.capacity,
                    "max_bytes": self.max_bytes, "skipped_errors": self.skipped_errors,
                    "skipped_empty": self.skipped_empty,
                    "skipped_oversized": self.skipped_oversized,
                    "key_time_seconds": self.key_time_seconds,
                    "lookup_time_seconds": self.lookup_time_seconds,
                    "copy_time_seconds": self.copy_time_seconds,
                    "store_time_seconds": self.store_time_seconds}

    def __len__(self):
        with self._lock:
            return len(self._entries)
