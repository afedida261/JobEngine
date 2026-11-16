"""Atomic local exports with bounded recovery from Windows sharing errors."""

import json
import tempfile
import time
import warnings
from pathlib import Path


class PersistenceError(OSError):
    def __init__(self, path, cause):
        self.path = Path(path)
        self.winerror = getattr(cause, "winerror", None)
        super().__init__(
            cause.errno,
            f"Could not persist {self.path} ({type(cause).__name__}, winerror={self.winerror}).",
        )


def atomic_write_text(path, text):
    path = Path(path)
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        temporary.chmod(0o600)
        for attempt in range(6):
            try:
                temporary.replace(path)
                return
            except OSError as exc:
                transient = isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {
                    5,
                    32,
                    33,
                }
                if not transient or attempt == 5:
                    raise
                time.sleep(0.01 * 2**attempt)
    except OSError as exc:
        raise PersistenceError(path, exc) from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                warnings.warn(
                    f"Could not remove persistence temporary file: {temporary}", RuntimeWarning
                )


def atomic_write_json(path, value):
    atomic_write_text(path, json.dumps(value, indent=2, ensure_ascii=False))
