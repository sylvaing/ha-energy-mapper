"""Daily reports stored as JSON files: ``<data>/reports/YYYY-MM-DD.json``."""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import pathlib
import re
import tempfile

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_day(value: str) -> dt.date | None:
    if not isinstance(value, str) or not DAY_RE.match(value):
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _write_atomic(path: pathlib.Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, separators=(",", ":"))
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class ReportStore:
    def __init__(self, data_dir: str | os.PathLike):
        self.root = pathlib.Path(data_dir)
        self.reports = self.root / "reports"
        self.reports.mkdir(parents=True, exist_ok=True)
        self._state_path = self.root / "state.json"

    def days(self) -> list[str]:
        return sorted(p.stem for p in self.reports.glob("*.json") if parse_day(p.stem))

    def has(self, day: str) -> bool:
        return (self.reports / f"{day}.json").is_file()

    def load(self, day: str) -> dict | None:
        if not parse_day(day):
            return None
        try:
            return json.loads((self.reports / f"{day}.json").read_text())
        except (OSError, ValueError):
            return None

    def save(self, day: str, report: dict) -> None:
        if not parse_day(day):
            raise ValueError(f"bad day: {day!r}")
        _write_atomic(self.reports / f"{day}.json", report)

    # small persistent state (last successful run)
    def state(self) -> dict:
        try:
            return json.loads(self._state_path.read_text())
        except (OSError, ValueError):
            return {}

    def set_state(self, **values) -> None:
        _write_atomic(self._state_path, {**self.state(), **values})
