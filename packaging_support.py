"""Build helpers for publishing without redistributing Microsoft runtime DLLs.

Only copies third-party binaries into the build's staging directory. The
installed Python environment is never modified. GEOS import-name edits are
recorded separately so recipients can reproduce or replace the LGPL library.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import struct

import pefile


_MSVC_NAME = re.compile(
    r"^(?:msvcp|vcruntime|concrt|vccorlib)\d+(?:_[a-z0-9]+)*(?:-[0-9a-f]+)?\.dll$",
    re.IGNORECASE,
)


def is_msvc_runtime(name: str) -> bool:
    return bool(_MSVC_NAME.fullmatch(Path(name).name))


def is_windows_runtime_copy(name: str) -> bool:
    filename = Path(name).name.lower()
    return (is_msvc_runtime(filename) or filename in {"ucrtbase.dll", "msvcrt.dll"}
            or filename.startswith(("api-ms-win-", "ext-ms-win-")))


def public_binaries(entries, staging: Path):
    """Drop runtime copies and restore hashed MSVC imports to system DLL names."""
    result, modifications = [], []
    staging.mkdir(parents=True, exist_ok=True)
    for destination, source, kind in entries:
        if is_windows_runtime_copy(destination) or is_windows_runtime_copy(source):
            continue
        path = Path(source)
        if path.suffix.lower() not in {".dll", ".pyd"}:
            result.append((destination, source, kind))
            continue
        pe = pefile.PE(str(path), fast_load=True)
        try:
            pe.parse_data_directories(directories=[
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"],
            ])
            changes = []
            for item in [*getattr(pe, "DIRECTORY_ENTRY_IMPORT", []),
                         *getattr(pe, "DIRECTORY_ENTRY_DELAY_IMPORT", [])]:
                name = item.dll.decode("ascii")
                if is_msvc_runtime(name) and "-" in name:
                    # GEOS wheels rename this dependency for isolation. Point
                    # it to the user's Microsoft-installed runtime instead.
                    replacement = name.split("-", 1)[0] + ".dll"
                    rva = getattr(item.struct, "Name", None)
                    if rva is None:
                        rva = item.struct.szName
                    changes.append((pe.get_offset_from_rva(rva), name, replacement))
            checksum_offset = pe.OPTIONAL_HEADER.get_field_absolute_offset("CheckSum")
        finally:
            pe.close()
        if not changes:
            result.append((destination, source, kind))
            continue
        original = path.read_bytes()
        data = bytearray(original)
        for offset, old_name, new_name in changes:
            old = old_name.encode("ascii")
            new = new_name.encode("ascii")
            assert data[offset:offset + len(old)] == old
            assert len(new) <= len(old)
            data[offset:offset + len(old) + 1] = new + b"\0" * (len(old) + 1 - len(new))
        updated = pefile.PE(data=bytes(data), fast_load=True)
        try:
            struct.pack_into("<I", data, checksum_offset, updated.generate_checksum())
        finally:
            updated.close()
        target = staging / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        result.append((destination, str(target), kind))
        modifications.append({
            "file": destination.replace("\\", "/"),
            "original_sha256": hashlib.sha256(original).hexdigest(),
            "modified_sha256": hashlib.sha256(data).hexdigest(),
            "changes": [{"original_import": old, "system_import": new}
                        for _, old, new in changes],
            "note": "Only import DLL names and the PE checksum were changed; GEOS code was not recompiled.",
        })
    return result, modifications
