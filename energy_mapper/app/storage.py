"""Everything the add-on keeps, under ``<data>`` (part of the HA backups).

    reports/YYYY-MM-DD.json.gz    full daily report, with the per-minute curve
    summaries/YYYY-MM-DD.json.gz  the same without the curve, kept longer
    registry.json                 signature registry (nilm_core.registry)
    state.json                    small persistent state (last run...)

Reports written by older versions as plain ``reports/YYYY-MM-DD.json`` are
compressed (and get their summary) when the store is opened.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import gzip
import json
import logging
import os
import pathlib
import re
import tempfile

_LOGGER = logging.getLogger(__name__)

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
GZ = ".json.gz"


def parse_day(value: str) -> dt.date | None:
    if not isinstance(value, str) or not DAY_RE.match(value):
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _write_atomic(path: pathlib.Path, data, compress: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        raw = json.dumps(data, separators=(",", ":")).encode()
        with os.fdopen(fd, "wb") as f:
            f.write(gzip.compress(raw, compresslevel=6, mtime=0) if compress else raw)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _read(path: pathlib.Path):
    data = path.read_bytes()
    if path.name.endswith(".gz"):
        data = gzip.decompress(data)
    return json.loads(data)


def summary_of(report: dict) -> dict:
    """A day summary: the report without its per-minute curve."""
    return {k: v for k, v in report.items() if k != "curve"}


def _days_in(folder: pathlib.Path) -> list[str]:
    return sorted(day for p in folder.glob("*" + GZ) if parse_day(day := p.name.removesuffix(GZ)))


class ReportStore:
    def __init__(self, data_dir: str | os.PathLike):
        self.root = pathlib.Path(data_dir)
        self.reports = self.root / "reports"
        self.summaries = self.root / "summaries"
        self.reports.mkdir(parents=True, exist_ok=True)
        self.summaries.mkdir(parents=True, exist_ok=True)
        self._state_path = self.root / "state.json"
        self.registry_path = self.root / "registry.json"
        # summaries already read, for the nightly registry update (read-only)
        self._cache: dict[str, dict] = {}
        self.migrate()

    def _report_path(self, day: str) -> pathlib.Path:
        return self.reports / f"{day}{GZ}"

    def _summary_path(self, day: str) -> pathlib.Path:
        return self.summaries / f"{day}{GZ}"

    def migrate(self) -> int:
        """Compress the plain JSON reports of older versions; returns how many."""
        n = 0
        for plain in sorted(self.reports.glob("*.json")):
            if not parse_day(plain.stem):
                continue
            try:
                report = json.loads(plain.read_text())
            except (OSError, ValueError):
                _LOGGER.warning("unreadable report %s left as is", plain.name)
                continue
            self.save(plain.stem, report)
            plain.unlink()
            n += 1
        if n:
            _LOGGER.info("%d report(s) compressed", n)
        return n

    # -- daily reports ------------------------------------------------------
    def days(self) -> list[str]:
        """Days with a full report (curve included)."""
        return _days_in(self.reports)

    def summary_days(self) -> list[str]:
        """Days with a summary (a superset of ``days()``)."""
        return sorted(set(_days_in(self.summaries)) | set(self.days()))

    def has(self, day: str) -> bool:
        return self._report_path(day).is_file() or self._summary_path(day).is_file()

    def load(self, day: str) -> dict | None:
        if not parse_day(day):
            return None
        try:
            return _read(self._report_path(day))
        except (OSError, ValueError, EOFError):
            return None

    def load_summary(self, day: str) -> dict | None:
        if not parse_day(day):
            return None
        try:
            return _read(self._summary_path(day))
        except (OSError, ValueError, EOFError):
            report = self.load(day)
            return summary_of(report) if report is not None else None

    def summaries_all(self) -> dict[str, dict]:
        """All the day summaries, read once and then kept in memory: callers
        must not modify them."""
        out = {}
        for day in self.summary_days():
            s = self._cache.get(day)
            if s is None:
                s = self.load_summary(day)
                if s is None:
                    continue
                self._cache[day] = s
            out[day] = s
        return out

    def save(self, day: str, report: dict) -> None:
        if not parse_day(day):
            raise ValueError(f"bad day: {day!r}")
        summary = summary_of(report)
        _write_atomic(self._summary_path(day), summary, compress=True)
        _write_atomic(self._report_path(day), report, compress=True)
        self._cache[day] = json.loads(json.dumps(summary))  # a copy, as read back

    # -- retention ------------------------------------------------------------
    def prune(self, today: dt.date, report_days: int, summary_days: float) -> dict:
        """Delete full reports older than ``report_days`` days and summaries
        older than ``summary_days`` days (a summary is never deleted while
        its full report is kept)."""
        removed = {"reports": [], "summaries": []}
        for day in self.days():
            if (today - dt.date.fromisoformat(day)).days > report_days:
                self._report_path(day).unlink(missing_ok=True)
                removed["reports"].append(day)
        kept = set(self.days())
        for day in _days_in(self.summaries):
            if day not in kept and (today - dt.date.fromisoformat(day)).days > summary_days:
                self._summary_path(day).unlink(missing_ok=True)
                self._cache.pop(day, None)
                removed["summaries"].append(day)
        return removed

    def size_bytes(self) -> int:
        total = 0
        for folder in (self.reports, self.summaries):
            with os.scandir(folder) as it:
                total += sum(e.stat().st_size for e in it if e.is_file())
        for path in (self.registry_path, self._state_path):
            with contextlib.suppress(OSError):
                total += path.stat().st_size
        return total

    # -- signature registry ---------------------------------------------------
    def load_registry(self) -> dict | None:
        """The stored registry, or None when missing or corrupt."""
        try:
            data = json.loads(self.registry_path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("signatures"), list):
            return None
        return data

    def save_registry(self, registry: dict) -> None:
        _write_atomic(self.registry_path, registry)

    # -- small persistent state (last successful run) -------------------------
    def state(self) -> dict:
        try:
            return json.loads(self._state_path.read_text())
        except (OSError, ValueError):
            return {}

    def set_state(self, **values) -> None:
        _write_atomic(self._state_path, {**self.state(), **values})
