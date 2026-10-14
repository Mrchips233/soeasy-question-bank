"""运行 python app.py，或双击 启动.cmd。"""
from __future__ import annotations

import json
import os
import queue
import threading
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from live_ocr import LiveOCRWindow
from question_bank import (BASE, BankStore, Collector, DebugLog, Desktop, OCREngine,
                           StopControl, Stopped, atomic_json, enable_dpi_awareness,
                           parse_question, question_output_dir, recognition_details)


QUESTION_TYPES = {"single": "单选题", "true_false": "判断题", "multiple": "多选题"}


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("SoEasy 截图题库采集器")
        root.geometry("740x730")
        root.minsize(640, 630)
        self.config_path = BASE / "config.json"
        self.config = {}
        if self.config_path.exists():
            try:
                self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.busy = False
        self.last_log_dir = None
        self.output_var = tk.StringVar(value=self.config.get("output", str(BASE / "output")))
        self.delay_var = tk.StringVar(value=str(self.config.get("delay", 0.6)))
        question_type = self.config.get("question_type", "single")
        if question_type not in QUESTION_TYPES:
            question_type = "single"
        self.question_type_var = tk.StringVar(value=question_type)
        self._last_question_type = question_type
        self._choice_options_count = str(self.config.get("expected_options", 4)) if question_type != "true_false" else "4"
        self.count_var = tk.StringVar(value="2" if question_type == "true_false" else self._choice_options_count)
        self.output_hint_var = tk.StringVar()
        self.overwrite_var = tk.BooleanVar(value=False)
        self.region_var = tk.StringVar(value=self.region_text())
        self.progress_var = tk.StringVar(value="请先框选小程序区域，再试运行 3 题。")
        box = ttk.Frame(root, padding=18)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text="截图识别题目并导出 Word", font=("Microsoft YaHei", 16, "bold")).pack(anchor="w")
        ttk.Label(box, text="把小程序放在主屏幕，打开练习题，完整显示题干、选项和底部按钮。\n"
                            "开始后主窗口最小化，实时 OCR 窗口显示识别文字；按 Esc 或将鼠标移到屏幕左上角停止。",
                  wraplength=690).pack(anchor="w", pady=(10, 12))
        region_row = ttk.Frame(box)
        region_row.pack(fill="x", pady=5)
        self.calibrate_btn = ttk.Button(region_row, text="1  框选小程序区域", command=self.calibrate)
        self.calibrate_btn.pack(side="left")
        ttk.Label(region_row, textvariable=self.region_var).pack(side="left", padx=12)
        out_row = ttk.Frame(box)
        out_row.pack(fill="x", pady=8)
        ttk.Label(out_row, text="输出目录").pack(side="left")
        self.output_entry = ttk.Entry(out_row, textvariable=self.output_var)
        self.output_entry.pack(side="left", fill="x", expand=True, padx=8)
        self.browse_btn = ttk.Button(out_row, text="选择", command=self.browse)
        self.browse_btn.pack(side="left")
        mode_row = ttk.Frame(box)
        mode_row.pack(fill="x", pady=(5, 0))
        ttk.Label(mode_row, text="题型").pack(side="left", padx=(0, 12))
        self.question_type_buttons = []
        for code, label in QUESTION_TYPES.items():
            button = ttk.Radiobutton(mode_row, text=label, variable=self.question_type_var, value=code)
            button.pack(side="left", padx=(0, 16))
            self.question_type_buttons.append(button)
        ttk.Label(mode_row, text="与小程序当前练习页面一致").pack(side="left")
        ttk.Label(box, textvariable=self.output_hint_var, wraplength=690).pack(anchor="w", pady=(5, 4))
        settings = ttk.Frame(box)
        settings.pack(fill="x", pady=5)
        ttk.Label(settings, text="界面等待秒数").pack(side="left")
        self.delay_entry = ttk.Entry(settings, textvariable=self.delay_var, width=7)
        self.delay_entry.pack(side="left", padx=(8, 20))
        ttk.Label(settings, text="每题选项数").pack(side="left")
        self.count_entry = ttk.Combobox(settings, textvariable=self.count_var,
                                        values=(4, 2, 3, 5, 6, 7, 8, 0), width=5, state="readonly")
        self.count_entry.pack(side="left", padx=8)
        ttk.Label(settings, text="0 = 自动识别").pack(side="left")
        repair = ttk.Frame(box)
        repair.pack(fill="x", pady=(7, 0))
        self.overwrite_check = ttk.Checkbutton(repair, text="重新采集并覆盖已有题（保留其他题）",
                                               variable=self.overwrite_var)
        self.overwrite_check.pack(side="left")
        self.repair_btn = ttk.Button(repair, text="重采当前题", command=lambda: self.start(1, overwrite=True))
        self.repair_btn.pack(side="left", padx=12)
        actions = ttk.Frame(box)
        actions.pack(fill="x", pady=12)
        self.preview_btn = ttk.Button(actions, text="2  仅识别当前题", command=self.preview)
        self.preview_btn.pack(side="left", padx=(0, 8))
        self.trial_btn = ttk.Button(actions, text="3  试采集 3 题", command=lambda: self.start(3))
        self.trial_btn.pack(side="left", padx=(0, 8))
        self.start_btn = ttk.Button(actions, text="采集到最后一题", command=lambda: self.start(0))
        self.start_btn.pack(side="left", padx=(0, 8))
        self.stop_btn = ttk.Button(actions, text="停止", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left")
        ttk.Label(box, textvariable=self.progress_var).pack(anchor="w", pady=(2, 8))
        self.log_box = tk.Text(box, height=14, wrap="word", font=("Microsoft YaHei", 10), state="disabled")
        bottom = ttk.Frame(box)
        # Reserve the fixed controls before giving the remaining height to logs.
        bottom.pack(side="bottom", fill="x", pady=(10, 0))
        self.export_btn = ttk.Button(bottom, text="重新导出已有题库", command=self.export)
        self.export_btn.pack(side="left")
        ttk.Button(bottom, text="打开输出目录", command=self.open_output).pack(side="left", padx=8)
        ttk.Button(bottom, text="打开调试日志", command=self.open_logs).pack(side="left")
        self.ocr_btn = ttk.Button(bottom, text="实时 OCR", command=self.toggle_ocr)
        self.ocr_btn.pack(side="left", padx=8)
        ttk.Label(bottom, text="停止时更新 Word。").pack(side="right")
        self.log_box.pack(fill="both", expand=True)
        self.controls = [self.calibrate_btn, self.browse_btn, self.output_entry, self.delay_entry,
                         self.count_entry, self.overwrite_check, self.repair_btn,
                         self.preview_btn, self.trial_btn, self.start_btn, self.export_btn,
                         *self.question_type_buttons]
        self.question_type_var.trace_add("write", self.question_type_changed)
        self.output_var.trace_add("write", self.update_output_hint)
        self.question_type_changed()
        # 不设置 transient：实时窗口在主窗口最小化后仍保持独立显示。
        self.live_ocr = LiveOCRWindow(root, self.stop, self.log)
        root.after(100, self.pump)
        root.protocol("WM_DELETE_WINDOW", self.close)

    def region_text(self):
        r = self.config.get("region")
        return f"({r['left']}, {r['top']})  {r['width']} × {r['height']}" if r else "尚未框选"

    def current_output_dir(self):
        output = Path(self.output_var.get()).expanduser().resolve()
        return question_output_dir(output, self.question_type_var.get())

    def update_output_hint(self, *_):
        self.output_hint_var.set(f"当前题库位置：{self.current_output_dir()}")

    def question_type_changed(self, *_):
        question_type = self.question_type_var.get()
        if question_type == "true_false":
            if self._last_question_type != "true_false":
                self._choice_options_count = self.count_var.get()
            self.count_var.set("2")
        elif self._last_question_type == "true_false":
            self.count_var.set(self._choice_options_count)
        self._last_question_type = question_type
        self.count_entry.configure(state="disabled" if self.busy or question_type == "true_false" else "readonly")
        self.update_output_hint()

    def log(self, message):
        self.events.put(("log", str(message)))

    def on_ocr(self, payload):
        # OCR 在工作线程运行；Tk 更新只通过主线程消息队列执行。
        self.events.put(("ocr", dict(payload)))

    def toggle_ocr(self):
        self.live_ocr.toggle()

    def pump(self):
        latest_ocr = None
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.log_box.configure(state="normal")
                    self.log_box.insert("end", payload + "\n")
                    self.log_box.see("end")
                    self.log_box.configure(state="disabled")
                elif kind == "progress":
                    done, total = payload
                    self.progress_var.set(f"已保存 {done} / {total} 题")
                elif kind == "ocr":
                    latest_ocr = payload
                elif kind == "done":
                    self.set_busy(False)
                    if latest_ocr is not None:
                        self.live_ocr.update(latest_ocr)
                        latest_ocr = None
                    self.live_ocr.end()
                    self.root.deiconify()
                    self.root.lift()
                    if payload:
                        messagebox.showerror("已停止", payload, parent=self.root)
        except queue.Empty:
            pass
        if latest_ocr is not None:
            self.live_ocr.update(latest_ocr)
        self.root.after(100, self.pump)

    def stop(self):
        self.stop_event.set()

    def set_busy(self, busy):
        self.busy = busy
        for control in self.controls:
            control.configure(state="disabled" if busy else ("readonly" if control is self.count_entry else "normal"))
        if not busy and self.question_type_var.get() == "true_false":
            self.count_entry.configure(state="disabled")
        self.stop_btn.configure(state="normal" if busy else "disabled")

    def browse(self):
        path = filedialog.askdirectory(parent=self.root, title="选择题库输出目录")
        if path:
            self.output_var.set(path)

    def settings(self):
        if "region" not in self.config:
            raise ValueError("请先框选小程序区域。")
        delay = float(self.delay_var.get())
        if not 0.2 <= delay <= 10:
            raise ValueError("等待秒数应在 0.2 到 10 之间。")
        output = Path(self.output_var.get()).expanduser().resolve()
        if not self.output_var.get().strip():
            raise ValueError("请选择输出目录。")
        question_type = self.question_type_var.get()
        if question_type not in QUESTION_TYPES:
            raise ValueError("请选择单选题、判断题或多选题。")
        expected_options = 2 if question_type == "true_false" else int(self.count_var.get())
        self.config.update(delay=delay, output=str(output), expected_options=expected_options,
                           question_type=question_type)
        self.config.pop("manual_review", None)
        atomic_json(self.config_path, self.config)
        # 覆盖开关仅用于本次运行，下次打开程序默认继续采集。
        return {**self.config, "overwrite": self.overwrite_var.get()}, output

    def calibrate(self):
        self.live_ocr.suspend_for_calibration()
        self.root.withdraw()
        self.root.after(350, self.select_region)

    def select_region(self):
        from PIL import Image, ImageTk
        import mss
        with mss.mss() as screen:
            # 主屏幕通常位于 (0, 0)，从 monitor 列表明确寻找。
            monitor = next((m for m in screen.monitors[1:] if m["left"] == 0 and m["top"] == 0), screen.monitors[1])
            shot = screen.grab(monitor)
            img = Image.frombytes("RGB", shot.size, shot.rgb)
        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.geometry(f"{monitor['width']}x{monitor['height']}+{monitor['left']}+{monitor['top']}")
        top.attributes("-topmost", True)
        canvas = tk.Canvas(top, width=img.width, height=img.height, highlightthickness=0, cursor="crosshair")
        canvas.pack(fill="both", expand=True)
        photo = ImageTk.PhotoImage(img)
        canvas.image = photo
        canvas.create_image(0, 0, image=photo, anchor="nw")
        canvas.create_rectangle(0, 0, img.width, 56, fill="#14263d", outline="")
        canvas.create_text(img.width / 2, 28, text="拖动框选完整小程序：从蓝色顶部到最底部按钮，松开保存。Esc 取消。",
                           fill="white", font=("Microsoft YaHei", 14))
        selection = {"start": None, "rectangle": None}

        def down(event):
            selection["start"] = (event.x, event.y)
            if selection["rectangle"]:
                canvas.delete(selection["rectangle"])
            selection["rectangle"] = canvas.create_rectangle(event.x, event.y, event.x, event.y,
                                                             outline="#ff3030", width=3)

        def drag(event):
            if selection["start"]:
                canvas.coords(selection["rectangle"], *selection["start"], event.x, event.y)

        def finish(event):
            if not selection["start"]:
                return
            sx, sy = selection["start"]
            left, right = sorted((max(0, min(img.width, sx)), max(0, min(img.width, event.x))))
            upper, lower = sorted((max(0, min(img.height, sy)), max(0, min(img.height, event.y))))
            if right - left < 240 or lower - upper < 400:
                return
            self.config["region"] = {"left": monitor["left"] + left, "top": monitor["top"] + upper,
                                     "width": right - left, "height": lower - upper}
            atomic_json(self.config_path, self.config)
            self.region_var.set(self.region_text())
            top.destroy()
            self.root.deiconify()
            self.live_ocr.resume_after_calibration()
            self.log("区域已保存。窗口移动或缩放后需要重新框选。")

        canvas.bind("<ButtonPress-1>", down)
        canvas.bind("<B1-Motion>", drag)
        canvas.bind("<ButtonRelease-1>", finish)
        top.bind("<Escape>", lambda event: (top.destroy(), self.root.deiconify(),
                                           self.live_ocr.resume_after_calibration()))
        top.focus_force()

    def launch_worker(self, action):
        try:
            config, output = self.settings()
        except (ValueError, OSError) as exc:
            messagebox.showerror("设置错误", str(exc), parent=self.root)
            return
        self.stop_event.clear()
        self.set_busy(True)
        self.root.iconify()
        self.live_ocr.begin(config["region"])

        def worker():
            error = None
            debug = DebugLog(question_output_dir(output, config.get("question_type", "single")),
                             settings=config, warning=self.log)
            self.last_log_dir = debug.directory
            self.log(f"调试日志目录：{debug.directory}")
            try:
                debug.event("worker_start", "启动界面操作", action=action.__name__)
                action(config, output, StopControl(self.stop_event), debug)
            except Stopped as exc:
                debug.event("worker_stopped", str(exc), traceback=traceback.format_exc())
                self.log(str(exc))
            except Exception as exc:
                stack = traceback.format_exc()
                debug.event("worker_failed", str(exc), exception_type=type(exc).__name__, traceback=stack)
                self.log(stack)
                error = f"{exc}\n\n调试日志：{debug.path}"
            finally:
                debug.event("worker_end", "界面操作结束")
                self.log(f"调试日志已保存：{debug.path}")
                self.events.put(("done", error))
        threading.Thread(target=worker, daemon=True).start()

    def preview(self):
        def action(config, output, stop, debug):
            self.log("加载本地 OCR 模型；3 秒后截屏，请将小程序置于前台。")
            debug.event("ocr_loading", "加载本地 OCR 模型")
            engine = OCREngine()
            debug.event("ocr_ready", "OCR 模型已加载",
                        backend=getattr(getattr(engine, "engine", None), "metadata", {}))
            stop.pause(3)
            desktop = Desktop(config["region"], stop)
            frame = None
            try:
                stop.check()
                frame = engine.read(desktop.screenshot())
                frame_id = debug.frame(frame, "preview")
                self.on_ocr({"stage": "preview", "number": None, "attempt": 1,
                             "text": "\n".join(line.text for line in frame.lines),
                             "frame_id": frame_id})
                question = parse_question(frame, expected_options=config["expected_options"],
                                          question_type=config.get("question_type", "single"))
                self.on_ocr({"stage": "preview", "number": question.number, "attempt": 1,
                             "text": "\n".join(line.text for line in frame.lines),
                             "frame_id": frame_id})
                debug.event("preview_question", "当前题识别成功", **recognition_details(frame, question, question))
                self.log(f"预览第 {question.number}/{question.total} 题：{question.stem}")
                for label, text in question.options.items():
                    self.log(text if label == text else f"{label}. {text}")
                self.log("仅识别当前题完成。核对内容后，可以试采集 3 题。")
            except Exception as exc:
                debug.capture(frame, "preview_failed")
                if frame is not None:
                    try:
                        debug.event("preview_failed", str(exc), **recognition_details(frame))
                    except Exception as diagnostic_error:
                        debug.event("diagnostic_error", str(diagnostic_error), stage="preview_failed")
                raise
            finally:
                desktop.close()
        self.launch_worker(action)

    def start(self, limit, overwrite=None):
        def action(config, output, stop, debug):
            replace = config["overwrite"] if overwrite is None else overwrite
            if replace:
                self.log("覆盖模式：重新识别成功后替换同题号；其他题保留，首次覆盖前自动备份 JSON。")
            else:
                self.log("继续采集模式：跳过已保存的题；修正错误可使用“重采当前题”。")
            self.log("加载本地 OCR 模型；5 秒后开始自动采集，请将小程序置于前台。")
            debug.event("ocr_loading", "加载本地 OCR 模型")
            engine = OCREngine()
            debug.event("ocr_ready", "OCR 模型已加载",
                        backend=getattr(getattr(engine, "engine", None), "metadata", {}))
            question_type = config.get("question_type", "single")
            store = BankStore(output, question_type=question_type)
            self.log(f"{QUESTION_TYPES[question_type]}保存位置：{question_output_dir(output, question_type)}")
            stop.pause(5)
            collector = Collector(Desktop(config["region"], stop), engine, store, stop,
                                  log=self.log, delay=config["delay"],
                                  expected_options=config["expected_options"],
                                  question_type=question_type,
                                  overwrite=replace,
                                  debug=debug,
                                  on_ocr=self.on_ocr,
                                  on_progress=lambda done, total: self.events.put(("progress", (done, total))))
            collector.run(max_new=limit)
        self.launch_worker(action)

    def export(self):
        try:
            path = BankStore(Path(self.output_var.get()).expanduser().resolve(),
                             question_type=self.question_type_var.get()).export()
            if path:
                self.log(f"Word 已重新导出：{path}")
            else:
                messagebox.showinfo("尚无数据", "输出目录中还没有保存的题目。", parent=self.root)
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc), parent=self.root)

    def open_output(self):
        path = self.current_output_dir()
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def open_logs(self):
        path = self.last_log_dir or (self.current_output_dir() / "logs")
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def close(self):
        if self.busy:
            self.stop()
            self.log("正在停止并保存 Word，保存后可关闭窗口。")
        else:
            self.root.destroy()


def main():
    if os.name != "nt":
        raise SystemExit("自动采集界面需要 Windows。")
    enable_dpi_awareness()
    root = tk.Tk()
    ttk.Style(root).theme_use("clam")
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
