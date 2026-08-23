"""Tiny .env loader (no dependency). Existing environment wins over the file."""
from __future__ import annotations

import os


def load(path: str = ".env") -> None:
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and v and not v.startswith("PASTE_") and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass
