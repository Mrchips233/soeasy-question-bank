"""Download the two official model artifacts and verify the published SHA-256.

Only needed when running from source. Release ZIPs already contain the models.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import urllib.request


def main() -> None:
    folder = Path(__file__).resolve().parent / "models"
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    for model in manifest["models"]:
        filename = model["filename"]
        if Path(filename).name != filename:
            raise ValueError("Invalid model filename")
        target = folder / filename
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == model["sha256"]:
            print(f"Already verified: {filename}")
            continue
        url = model["source_url"]
        if not url.startswith("https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/"):
            raise ValueError("Unexpected model download host")
        temporary = target.with_suffix(target.suffix + ".download")
        with urllib.request.urlopen(url, timeout=120) as response, temporary.open("wb") as stream:
            while block := response.read(1024 * 1024):
                stream.write(block)
        if temporary.stat().st_size != model["size_bytes"] or hashlib.sha256(temporary.read_bytes()).hexdigest() != model["sha256"]:
            raise RuntimeError(f"Model verification failed: {filename}")
        temporary.replace(target)
        print(f"Verified: {filename}")


if __name__ == "__main__":
    main()
