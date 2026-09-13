"""Small dependency-free terminal UI primitives used by the CLI pipeline."""
from __future__ import annotations

import shutil
import sys


def terminal_width(default: int = 88) -> int:
    return shutil.get_terminal_size((default, 24)).columns


def stage(number: int, total: int, title: str, detail: str = "") -> None:
    line = f"[{number}/{total}] {title}"
    if detail:
        line += f" — {detail}"
    print(f"\n{'─' * min(terminal_width(), 96)}\n{line}")


def info(message: str, indent: int = 2) -> None:
    print(" " * indent + message)


def progress(label: str, current: int, total: int, *, width: int = 24, suffix: str = "") -> None:
    total = max(total, 1)
    current = min(max(current, 0), total)
    filled = int(width * current / total)
    bar = "█" * filled + "░" * (width - filled)
    pct = int(current * 100 / total)
    sys.stdout.write(f"\r  {label:<28} [{bar}] {pct:3d}% ({current}/{total}){suffix}")
    sys.stdout.flush()
    if current >= total:
        sys.stdout.write("\n")
