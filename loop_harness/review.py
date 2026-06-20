"""Human-review queue persistence."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


def add_to_review(suspect, commit: str, model: str, *, review_file: Path,
                  lock: threading.Lock) -> None:
    """Append one review item with an atomic read-modify-write."""
    with lock:
        items = []
        if review_file.exists():
            try:
                loaded = json.loads(review_file.read_text())
                if isinstance(loaded, list):
                    items = loaded
            except json.JSONDecodeError:
                pass
        items.append({
            'commit': commit,
            'file': suspect.file,
            'line': suspect.line,
            'why': suspect.why,
            'snippet': suspect.snippet[:200],
            'model': model,
            'created_at': time.time(),
            'reason': 'toca dinero/stock/estado/auth - requiere revision humana',
        })
        tmp = review_file.with_suffix('.tmp')
        tmp.write_text(json.dumps(items, indent=2))
        os.replace(tmp, review_file)

