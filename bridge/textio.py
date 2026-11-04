"""Reading text files that people edit by hand, on any platform.

Windows tools save text in several ways: UTF-8 (with or without a BOM), UTF-16 (PowerShell 5's `>` redirect), or the legacy ANSI
code page (cp1252). Python's default `read_text()` copes with only one of them, and a UTF-8 BOM silently turns the first
key of a .env file into '\ufeffANTHROPIC_API_KEY'. These helpers read all of them correctly.
"""
from __future__ import annotations

import codecs
from pathlib import Path


def kind(data: bytes) -> str:
    """'utf-16', 'utf-8 with BOM', 'utf-8' or 'ansi (cp1252)': how these bytes are encoded."""
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    if data.startswith(codecs.BOM_UTF8):
        return "utf-8 with BOM"
    try:
        data.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "ansi (cp1252)"


def decode(data: bytes) -> str:
    k = kind(data)
    if k == "utf-16":
        return data.decode("utf-16")
    if k == "ansi (cp1252)":
        return data.decode("cp1252", errors="replace")
    return data.decode("utf-8-sig")


def read_text(path: str | Path) -> str:
    return decode(Path(path).read_bytes())
