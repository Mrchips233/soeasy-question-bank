"""Read-only OCR monitor; Tk is updated only from the application's UI thread."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Mapping


def overlaps(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> bool:
    """Rectangles are (left, top, width, height); touching edges do not overlap."""
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def find_monitor_position(screen: tuple[int, int, int, int],
                          region: Mapping[str, int] | None,
                          preferred: tuple[int, int] = (480, 560),
                          minimum: tuple[int, int] = (270, 220),
                          margin: int = 12) -> tuple[int, int, int, int] | None:
    """Find an entire free rectangle beside the capture region, without desktop I/O."""
    sx, sy, sw, sh = screen
    if sw <= 0 or sh <= 0:
        return None
    left, top = sx + margin, sy + margin
    right, bottom = sx + sw - margin, sy + sh - margin
    if region is None:
        spaces = [(left, top, right, bottom)]
    else:
        rx, ry = int(region["left"]), int(region["top"])
        rr, rb = rx + int(region["width"]), ry + int(region["height"])
        # Keep a gap even when the source touches a screen edge or lies off-screen.
        spaces = [(max(left, rr + margin), top, right, bottom),
                  (left, top, min(right, rx - margin), bottom),
                  (left, max(top, rb + margin), right, bottom),
                  (left, top, right, min(bottom, ry - margin))]
    choices = []
    for index, (x, y, edge_x, edge_y) in enumerate(spaces):
        width, height = min(preferred[0], edge_x - x), min(preferred[1], edge_y - y)
        if width >= minimum[0] and height >= minimum[1]:
            choices.append((width * height, -index, (x, y, width, height)))
    return max(choices)[2] if choices else None


_STAGES = {
    "next_question": "等待下一题",
    "initial": "读取当前题", "current_page": "读取当前页面",
    "reveal_restore": "等待提示消失", "reveal_feedback_frozen": "读取查看答案反馈",
    "feedback_unverified": "读取答题反馈",
    "preview": "仅识别当前题", "question": "读取题目", "stable_question": "读取题目",
    "before_selection": "选择选项前", "after_selection": "选择选项后",
    "wait_answer": "读取答案", "answer": "读取答案", "before_dismiss": "关闭答案前",
    "after_dismiss": "关闭答案后", "before_next": "切换下一题前",
    "no_wrong_feedback": "判断答案反馈", "stopped": "采集已停止",
}


class LiveOCRWindow:
    """An independent window that remains visible when the main window minimizes.

    During capture it is undecorated and fixed in the safe rectangle, so the user
    cannot drag or resize it into the screenshot. Calibration always hides it.
    """
    def __init__(self, root: tk.Tk, stop: Callable[[], None], log: Callable[[str], None]):
        self.root, self.log = root, log
        self.top = tk.Toplevel(root)
        self.top.withdraw()
        # Keep the same window style for its lifetime. Switching native styles
        # around minimize/destroy is fragile on Windows Tk.
        self.top.overrideredirect(True)
        self.top.title("当前 OCR 文字")
        self.top.geometry("480x560+20+20")
        self.top.minsize(270, 220)
        self.top.protocol("WM_DELETE_WINDOW", self.hide)
        self.active = False
        self.region: dict[str, int] | None = None
        self.safe_bounds: tuple[int, int, int, int] | None = None
        self.latest: dict = {}
        self.hidden_by_user = False
        self.calibrating = False
        self._resume_after_calibration = False
        self._no_space_reported = False
        box = ttk.Frame(self.top, padding=10, relief="solid", borderwidth=1)
        box.pack(fill="both", expand=True)
        header = ttk.Frame(box)
        header.pack(fill="x")
        ttk.Label(header, text="当前 OCR 文字", font=("Microsoft YaHei", 12, "bold")).pack(side="left")
        ttk.Button(header, text="隐藏", command=self.hide, width=6).pack(side="right")
        self.status = tk.StringVar(value="等待识别…")
        self.status_label = ttk.Label(box, textvariable=self.status, wraplength=450)
        self.status_label.pack(anchor="w", pady=(7, 4))
        self.hint_label = ttk.Label(box, text="只读，自动刷新；采集时固定在截图区域外。", wraplength=450)
        self.hint_label.pack(anchor="w", pady=(0, 8))
        text_row = ttk.Frame(box)
        text_row.pack(fill="both", expand=True)
        self.text = tk.Text(text_row, wrap="word", font=("Microsoft YaHei", 11),
                            state="disabled", height=10)
        scrollbar = ttk.Scrollbar(text_row, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        actions = ttk.Frame(box)
        actions.pack(fill="x", pady=(8, 0))
        ttk.Button(actions, text="复制当前文字", command=self.copy).pack(side="left")
        self.stop_btn = ttk.Button(actions, text="停止采集", command=stop, state="disabled")
        self.stop_btn.pack(side="right")
        self.top.bind("<Configure>", self._keep_safe, add=True)

    def _screen(self) -> tuple[int, int, int, int]:
        # Collection and calibration use the primary monitor; no screenshot needed.
        return (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())

    def update(self, payload: Mapping) -> None:
        self.latest = dict(payload)
        stage = str(payload.get("stage") or "识别")
        fields = [_STAGES.get(stage, stage)]
        if payload.get("number") is not None:
            fields.append(f"第 {payload['number']} 题")
        if payload.get("attempt") is not None:
            fields.append(f"尝试 {payload['attempt']}")
        if payload.get("frame_id") is not None:
            fields.append(f"画面 {payload['frame_id']}")
        self.status.set(" · ".join(fields))
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", str(payload.get("text") or "（这次没有识别到文字）"))
        self.text.configure(state="disabled")

    def begin(self, region: Mapping[str, int]) -> None:
        self.active = True
        self.region = dict(region)
        self.hidden_by_user = False
        self._no_space_reported = False
        self.stop_btn.configure(state="normal")
        self.update({"stage": "等待 OCR 模型加载", "text": "加载完成后自动显示每次识别出的文字。"})
        self.show()

    def end(self) -> None:
        self.active = False
        self.safe_bounds = None
        self.stop_btn.configure(state="disabled")
        self.top.attributes("-topmost", False)
        if self.latest:
            self.status.set(self.status.get() + " · 已停止")

    def show(self) -> bool:
        if self.calibrating:
            return False
        if self.active:
            position = find_monitor_position(self._screen(), self.region)
            if position is None:
                self.top.withdraw()
                if not self._no_space_reported:
                    self.log("截图区域外没有足够空间显示 OCR 窗口，已自动隐藏以免遮挡题目；"
                             "停止后点击“实时 OCR”可查看最后一次识别文字。缩小框选区域可实时查看。")
                    self._no_space_reported = True
                return False
            self.safe_bounds = position
            x, y, width, height = position
            self.top.withdraw()
            self.top.geometry(f"{width}x{height}{x:+d}{y:+d}")
            self.top.attributes("-topmost", True)
        else:
            self.top.attributes("-topmost", False)
        self.hidden_by_user = False
        self.top.deiconify()
        self.top.lift()
        return True

    def hide(self) -> None:
        self.hidden_by_user = True
        self.top.withdraw()

    def toggle(self) -> None:
        if self.top.state() == "withdrawn":
            self.show()
        else:
            self.hide()

    def _keep_safe(self, event) -> None:
        if event.widget is not self.top:
            return
        wraplength = max(200, self.top.winfo_width() - 24)
        self.status_label.configure(wraplength=wraplength)
        self.hint_label.configure(wraplength=wraplength)
        if not self.active or not self.safe_bounds:
            return
        x, y, width, height = self.safe_bounds
        if (self.top.winfo_x(), self.top.winfo_y(), self.top.winfo_width(),
                self.top.winfo_height()) != self.safe_bounds:
            self.top.geometry(f"{width}x{height}{x:+d}{y:+d}")

    def suspend_for_calibration(self) -> None:
        self._resume_after_calibration = self.top.state() != "withdrawn"
        self.calibrating = True
        self.top.withdraw()

    def resume_after_calibration(self) -> None:
        self.calibrating = False
        if self._resume_after_calibration:
            self.show()
        self._resume_after_calibration = False

    def copy(self) -> None:
        self.top.clipboard_clear()
        self.top.clipboard_append(str(self.latest.get("text") or ""))
