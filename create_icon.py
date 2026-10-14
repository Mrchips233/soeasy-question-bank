"""Generate this project's original geometric icon; no fonts or stock art used."""
from pathlib import Path
from PIL import Image, ImageDraw


def main() -> None:
    image = Image.new("RGBA", (256, 256), (21, 53, 84, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((55, 30, 201, 223), radius=13, fill=(243, 247, 253, 255))
    for top in (66, 99, 132):
        draw.rounded_rectangle((79, top, 176, top + 8), radius=4, fill=(53, 99, 143, 255))
    draw.line([(88, 178), (113, 196), (165, 155)], fill=(32, 152, 99, 255), width=13, joint="curve")
    root = Path(__file__).resolve().parent
    image.save(root / "app.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


if __name__ == "__main__":
    main()
