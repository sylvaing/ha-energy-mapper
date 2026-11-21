"""Hourly energy of the appliances in Home Assistant's long-term statistics.

The analysis is nightly: a day's values are known the next morning. A plain
``total_increasing`` sensor would put the whole day in the hour of the
analysis, so the hourly kWh of each appliance (``GET /api/hourly/<day>``)
are imported as **external statistics** with the timestamp of each hour,
like utility integrations publish delayed meter data:
``energy_mapper:<appliance>_energy``, ``has_sum``, kWh. They can be added to
the Energy dashboard as individual devices.

Sums stay monotonic across days and re-imports: importing some hours reads
the stored rows from just before the first of them, keeps the hourly values
of the rows that are not re-imported (``state`` holds the hour's kWh), and
rewrites the cumulative sums from there, so a re-analysed day replaces its
hours instead of adding to them, and the days after it are shifted.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable, Mapping
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .appliances import Appliance
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# rows read before the first re-imported hour to find the sum to continue
LOOKBACK = dt.timedelta(days=7)
# sums are rounded to 1 µWh: avoids rewriting rows for float noise
SUM_DIGITS = 9

type Row = tuple[int, float, float]  # (hour start, kWh in the hour, cumulative kWh)


def hourly_values(
    appliance: Appliance, payloads: Mapping[str, Mapping[str, Any]], days: Iterable[str]
) -> dict[int, float]:
    """kWh of ``appliance`` in each UTC hour of ``days`` (zeros included, so a
    re-import overwrites a value that disappeared). ``payloads`` are hourly
    answers by day; an hour shared with a neighbouring day (time zones with a
    half-hour offset) adds the neighbour's part when its payload is given."""
    values: dict[int, float] = {}
    for day in days:
        for hour in payloads[day].get("hours") or []:
            values[int(hour["start"])] = 0.0
    for payload in payloads.values():
        for hour in payload.get("hours") or []:
            start = int(hour["start"])
            if start in values:
                values[start] += float(
                    (hour.get(appliance.hourly_kind) or {}).get(appliance.source_id) or 0.0
                )
    return {h: round(v, 6) for h, v in sorted(values.items())}


def merge_rows(
    values: Mapping[int, float],
    existing: Iterable[tuple[int, float | None, float | None]],
    sum_before: float,
) -> list[Row]:
    """Rows to write so that ``values`` replace their hours.

    ``existing``: the stored rows ``(start, state, sum)`` from the first hour
    of ``values`` on, sorted; ``sum_before``: the sum of the last row before
    it (0 when none). Stored hours that are not re-imported keep their value
    (``state``, else the difference of the sums) and are rewritten only when
    their sum moves."""
    if not values:
        return []
    old_value: dict[int, float] = {}
    old_sum: dict[int, float | None] = {}
    previous = sum_before
    for start, state, total in existing:
        if state is not None:
            value = state
        elif total is not None:
            value = total - previous
        else:
            value = 0.0
        old_value[start] = max(value, 0.0)
        old_sum[start] = total
        if total is not None:
            previous = total
    rows: list[Row] = []
    total = round(sum_before, SUM_DIGITS)
    for start in sorted(set(values) | set(old_value)):
        value = max(values[start], 0.0) if start in values else old_value[start]
        total = round(total + value, SUM_DIGITS)
        stored = old_sum.get(start)
        if start in values or stored is None or abs(stored - total) > 10**-SUM_DIGITS:
            rows.append((start, value, total))
    return rows


# unit_class: required from Home Assistant 2026.11, unknown to older versions (2025.10)
_HAS_UNIT_CLASS = "unit_class" in (StatisticMetaData.__required_keys__ | StatisticMetaData.__optional_keys__)


def metadata(appliance: Appliance) -> StatisticMetaData:
    meta = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=appliance.name,
        source=DOMAIN,
        statistic_id=appliance.statistic_id,
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    )
    if _HAS_UNIT_CLASS:
        meta["unit_class"] = EnergyConverter.UNIT_CLASS
    return meta


def _read_rows(
    hass: HomeAssistant, statistic_id: str, first: int
) -> tuple[float, list[tuple[int, float | None, float | None]]]:
    """(sum before ``first``, stored rows from ``first`` on). Runs in the recorder executor."""
    first_dt = dt_util.utc_from_timestamp(first)
    rows = statistics_during_period(
        hass, first_dt - LOOKBACK, None, {statistic_id}, "hour", None, {"state", "sum"}
    ).get(statistic_id, [])
    before = [r for r in rows if r["start"] < first]
    if not before:
        # a gap of more than a week before the first hour: look further back
        older = statistics_during_period(
            hass, dt_util.utc_from_timestamp(0), first_dt - LOOKBACK, {statistic_id}, "hour", None, {"sum"}
        ).get(statistic_id, [])
        before = older
    sum_before = 0.0
    for r in reversed(before):
        if r.get("sum") is not None:
            sum_before = float(r["sum"])
            break
    after = [(int(r["start"]), r.get("state"), r.get("sum")) for r in rows if r["start"] >= first]
    return sum_before, after


async def async_import(hass: HomeAssistant, appliance: Appliance, values: Mapping[int, float]) -> int:
    """Import the hourly ``values`` of ``appliance``; returns the rows written."""
    if not values:
        return 0
    first = min(values)
    sum_before, existing = await get_instance(hass).async_add_executor_job(
        _read_rows, hass, appliance.statistic_id, first
    )
    rows = merge_rows(values, existing, sum_before)
    if rows:
        async_add_external_statistics(
            hass,
            metadata(appliance),
            [
                StatisticData(start=dt_util.utc_from_timestamp(start), state=value, sum=total)
                for start, value, total in rows
            ],
        )
    _LOGGER.debug("%s: %d hourly rows written", appliance.statistic_id, len(rows))
    return len(rows)


def _our_ids(hass: HomeAssistant) -> set[str]:
    return set(get_metadata(hass, statistic_source=DOMAIN))


async def async_statistic_ids(hass: HomeAssistant) -> set[str]:
    """Ids of the statistics this integration imported (source ``energy_mapper``)."""
    instance = get_instance(hass)
    # imports queued by a previous run must be in the database
    await instance.async_block_till_done()
    return await instance.async_add_executor_job(_our_ids, hass)


def async_clear(hass: HomeAssistant, statistic_ids: Iterable[str]) -> None:
    """Delete our statistics (only ids of source ``energy_mapper``)."""
    ids = sorted(s for s in statistic_ids if s.startswith(f"{DOMAIN}:"))
    if ids:
        _LOGGER.info("Clearing the statistics of removed appliances: %s", ", ".join(ids))
        get_instance(hass).async_clear_statistics(ids)
