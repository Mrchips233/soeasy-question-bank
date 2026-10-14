"""Windows 免安装版入口；保留无控制台启动失败的诊断信息。"""
from __future__ import annotations

import ctypes
from datetime import datetime
import os
from pathlib import Path
import sys
import traceback


def main() -> int:
    # windowed EXE 没有标准输出；第三方日志库仍可能访问它们。
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
            from public_selftest import main as selftest
            return selftest(Path(sys.argv[2]))
        from app import main as app_main
        app_main()
        return 0
    except Exception:
        details = traceback.format_exc()
        log = base / "启动错误.log"
        try:
            with log.open("a", encoding="utf-8") as stream:
                stream.write(f"\n{datetime.now().astimezone().isoformat()}\n{details}")
            message = f"程序启动失败，详细信息已保存到：\n{log}\n\n{details.splitlines()[-1]}"
        except OSError:
            message = f"程序启动失败：\n{details.splitlines()[-1]}"
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(None, message, "SoEasy 题库采集器", 0x10)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
