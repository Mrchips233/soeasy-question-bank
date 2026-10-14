"""Recreate the public self-test PNGs from original drawing code and sample text.

No screenshots, third-party question banks, or application logos are used.
The selected system font is used only for rendering and is never copied.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _system_font() -> Path:
    windows_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    candidates = [windows_fonts / "msyh.ttc", windows_fonts / "msyh.ttf",
                  windows_fonts / "simhei.ttf",
                  Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("No Chinese system font was found; pass --font FONT_FILE.")


def draw_sample(font_path: str | Path, *, answer: bool = False) -> Image.Image:
    """Draw original color-mixing test artwork and its explicit answer panel."""
    image = Image.new("RGB", (900, 1300), "#f4f6fa")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((20, 20, 880, 1280), radius=18,
                           fill="white", outline="#cad2de", width=2)

    def text(x: int, y: int, content: str, size: int = 34) -> None:
        draw.text((x, y), content,
                  font=ImageFont.truetype(str(font_path), size), fill="#202b3b")

    text(52, 44, "单选题练习(共7题)", 32)
    draw.line((52, 110, 848, 110), fill="#cad2de", width=2)
    text(52, 150, "1. 蓝色和黄色混合后得到什么颜色？", 34)
    text(750, 270, "1/7", 34)
    for index, option in enumerate(("A. 红色", "B. 绿色", "C. 紫色", "D. 黑色")):
        y = 390 + index * 125
        draw.rounded_rectangle((65, y - 14, 835, y + 76), radius=8,
                               outline="#cad2de", width=2)
        text(100, y, option, 36)
    text(485, 1210, "查看答案", 32)
    text(710, 1210, "下一题", 32)
    if answer:
        draw.rectangle((48, 350, 852, 1030), fill="white")
        draw.rounded_rectangle((170, 490, 730, 840), radius=20,
                               fill="#edf3f8", outline="#57768f", width=2)
        text(347, 530, "答题错误", 38)
        text(295, 630, "正确答案是：B", 38)
        text(397, 750, "知道了", 34)
    return image


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).resolve().parent / "public_test_assets",
                        help="Output directory (default: public_test_assets beside this script)")
    parser.add_argument("--font", type=Path,
                        help="An existing Chinese system font; the font is not copied")
    arguments = parser.parse_args(argv)
    font_path = arguments.font if arguments.font is not None else _system_font()
    if not font_path.is_file():
        parser.error(f"Font file does not exist: {font_path}")
    arguments.output.mkdir(parents=True, exist_ok=True)
    for filename, answer in (("public_question.png", False), ("public_answer.png", True)):
        draw_sample(font_path, answer=answer).save(arguments.output / filename)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
