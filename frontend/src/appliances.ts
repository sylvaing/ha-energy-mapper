// The Appliances tab (#54): one card per appliance (config subentry of the
// integration), built from the day's report and the appliances' daily energy
// statistics. Pure logic, unit tested, no DOM.

import { addDays, dayOf, startOfDay } from "./format.ts";
import type { UsageRow } from "./data.ts";
import type { ExamplesStatus } from "./examples.ts";
import type { Registry, Report } from "./types.ts";

/** One source of an appliance (energy_mapper/appliances). */
export interface ApplianceSource {
  type: "switched_load" | "signature" | "submeter" | string;
  /** signature: the registry id */
  id?: string;
  /** switched load, sub-meter: the entity */
  entity_id?: string;
}

/** An appliance (energy_mapper/appliances, integration 0.7.0+). */
export interface ApplianceInfo {
  key: string;
  name: string;
  subentry_id: string | null;
  /** its hourly energy statistic, null when it has none (power sensor only) */
  statistic_id: string | null;
  power_w: number | null;
  sources: ApplianceSource[];
  /** a power sensor teaching it its signature (#14, integration 0.9.0+) */
  /** what teaches it (#14): a power sensor, or a connected appliance's state (#79, `mode: "state"`) */
  teacher?: {
    entity_id: string;
    since?: number | null;
    mode?: "power" | "state";
    on_states?: string[];
    phase_entity?: string | null;
    energy_entity?: string | null;
    program_entity?: string | null;
  } | null;
  /** what the analysed days found of its labelled examples (#17), null without any */
  examples_status?: ExamplesStatus | null;
}

export interface ApplianceListing {
  entry_id: string;
  appliances: ApplianceInfo[];
}

/** How an appliance is tracked: recognised in the curve, a switch whose
 * on-time gives its energy, a power sensor that measures it. */
export type Tracking = "curve" | "switch" | "sensor";

const TRACKING: Record<string, Tracking> = { signature: "curve", switched_load: "switch", submeter: "sensor" };

/** The ways an appliance is tracked, one each, in a stable order (several:
 * merged sources). */
export function trackingOf(appliance: Pick<ApplianceInfo, "sources">): Tracking[] {
  const kinds = new Set(appliance.sources.map((s) => TRACKING[s.type]).filter(Boolean));
  return (["curve", "switch", "sensor"] as const).filter((k) => kinds.has(k));
}

/** Whether its energy is estimated (curve, switch) rather than measured. */
export function isEstimated(appliance: Pick<ApplianceInfo, "sources">): boolean {
  return trackingOf(appliance).some((k) => k !== "sensor");
}

/** Ids of the chart series of the day that belong to the appliance: its
 * switched loads ("known:<name>", through the report's known loads) and its
 * signatures ("sig:<registry id>"). Sub-meters have no series of their own. */
export function applianceSeries(appliance: Pick<ApplianceInfo, "sources">, report: Report | null): string[] {
  if (!report) return [];
  const loads = new Map((report.known_loads ?? []).map((k) => [k.entity_id, k.name]));
  const out: string[] = [];
  for (const s of appliance.sources) {
    if (s.type === "switched_load" && s.entity_id) {
      const name = loads.get(s.entity_id);
      if (name !== undefined) out.push(`known:${name}`);
    } else if (s.type === "signature" && s.id) out.push(`sig:${s.id}`);
  }
  return out;
}

export interface DayUsage {
  energy_kwh: number;
  runs: number | null;
  /** the series with the most energy, to select on the Today tab */
  series: string | null;
}

/** Energy and runs of the appliance on the report's day, from the rows of
 * "What used energy"; null when none of its series used energy. */
export function dayUsage(appliance: Pick<ApplianceInfo, "sources">, report: Report | null, rows: UsageRow[]): DayUsage | null {
  const ids = new Set(applianceSeries(appliance, report));
  const mine = rows.filter((r) => ids.has(r.id));
  if (!mine.length) return null;
  const runs = mine.some((r) => r.runs != null) ? mine.reduce((n, r) => n + (r.runs ?? 0), 0) : null;
  const top = mine.reduce((a, b) => (b.energy_kwh > a.energy_kwh ? b : a));
  return { energy_kwh: mine.reduce((e, r) => e + r.energy_kwh, 0), runs, series: top.id };
}

/** The last day the add-on saw the appliance run ("YYYY-MM-DD"), from the
 * registry (signatures and switched loads); null when unknown. */
export function lastUse(appliance: Pick<ApplianceInfo, "sources">, registry: Registry | null): string | null {
  let last: string | null = null;
  const keep = (day: string | null | undefined) => {
    if (day && (!last || day > last)) last = day;
  };
  for (const s of appliance.sources) {
    if (s.type === "signature") keep(registry?.signatures?.find((r) => r.id === s.id)?.last_seen);
    else if (s.type === "switched_load") keep(registry?.switched_loads?.find((l) => l.entity_id === s.entity_id)?.last_seen);
  }
  return last;
}

// -- daily energy statistics ----------------------------------------------------

export type Period = "day" | "week" | "month";
export const PERIODS: readonly Period[] = ["day", "week", "month"];
export const PERIOD_DAYS: Record<Period, number> = { day: 1, week: 7, month: 30 };
/** Days of the statistics asked for at once: enough for every period. */
export const STATS_DAYS = 30;

/** The ``n`` days ending on ``day``, oldest first. */
export function daysEnding(day: string, n: number): string[] {
  return Array.from({ length: n }, (_, i) => addDays(day, i - n + 1));
}

/** The recorder/statistics_during_period request for the daily energy of the
 * ``n`` days ending on ``day`` (days of Home Assistant's time zone). */
export function statsRequest(statisticIds: string[], day: string, timeZone: string, n = STATS_DAYS): Record<string, unknown> {
  const first = daysEnding(day, n)[0];
  return {
    type: "recorder/statistics_during_period",
    start_time: new Date(startOfDay(first, timeZone)).toISOString(),
    end_time: new Date(startOfDay(addDays(day, 1), timeZone)).toISOString(),
    statistic_ids: statisticIds,
    period: "day",
    types: ["change"],
    units: { energy: "kWh" },
  };
}

/** One row of the answer: ``start`` in ms (an ISO string before HA 2023.3). */
export interface StatRow {
  start: number | string;
  change?: number | null;
}

/** kWh of each statistic on each day: statistic id -> day -> kWh. */
export type DailyEnergy = Record<string, Record<string, number>>;

export function dailyEnergy(answer: Record<string, StatRow[]> | null | undefined, timeZone: string): DailyEnergy {
  const out: DailyEnergy = {};
  for (const [id, rows] of Object.entries(answer ?? {})) {
    const days: Record<string, number> = {};
    for (const row of rows ?? []) {
      const ms = typeof row.start === "number" ? row.start : Date.parse(row.start);
      if (!Number.isFinite(ms) || row.change == null) continue;
      const day = dayOf(ms, timeZone);
      days[day] = (days[day] ?? 0) + Math.max(row.change, 0);
    }
    out[id] = days;
  }
  return out;
}

// -- the cards ------------------------------------------------------------------

export interface ApplianceBar {
  day: string;
  energy_kwh: number;
  /** relative to the largest bar of the card (0..1) */
  ratio: number;
}

export interface ApplianceCard {
  key: string;
  name: string;
  subentry_id: string | null;
  tracking: Tracking[];
  estimated: boolean;
  /** energy of the period, null when unknown */
  energy_kwh: number | null;
  /** runs of the day (period "day" only), null when unknown */
  runs: number | null;
  /** days with some energy in the period (periods of several days), null when unknown */
  used_days: number | null;
  last_use: string | null;
  /** daily energy of the last 7 days (30 for the month), oldest first; empty without statistics */
  bars: ApplianceBar[];
  /** the series to select on the Today tab, null when it did not run that day */
  series: string | null;
}

export interface CardInputs {
  day: string | null;
  period: Period;
  report: Report | null;
  rows: UsageRow[];
  registry: Registry | null;
  /** daily energy statistics of the 30 days ending on ``day``, null: not available */
  stats: DailyEnergy | null;
}

const round = (kwh: number) => Math.round(kwh * 1e6) / 1e6;

/** One card per appliance, by decreasing energy of the period, then by name. */
export function applianceCards(appliances: ApplianceInfo[], input: CardInputs): ApplianceCard[] {
  const { day, period, report, rows, registry, stats } = input;
  const reportOfDay = report && report.day === day ? report : null;
  const cards = appliances.map((a): ApplianceCard => {
    const usage = dayUsage(a, reportOfDay, rows);
    const daily = a.statistic_id && stats ? (stats[a.statistic_id] ?? {}) : null;
    const span = (n: number) => (day ? daysEnding(day, n) : []);
    let energy: number | null = null;
    let runs: number | null = null;
    let usedDays: number | null = null;
    if (period === "day") {
      // the report of the day, as on the Today tab; else the statistics
      if (usage) energy = usage.energy_kwh;
      else if (daily && day && daily[day] != null) energy = daily[day];
      // analysed and not in the report: it did not run (a sensor is not in it)
      else if (reportOfDay && isEstimated(a)) energy = 0;
      runs = usage ? usage.runs : null;
    } else if (daily) {
      const days = span(PERIOD_DAYS[period]);
      energy = round(days.reduce((e, d) => e + (daily[d] ?? 0), 0));
      usedDays = days.filter((d) => (daily[d] ?? 0) >= 0.0005).length;
    }
    const bars: ApplianceBar[] = daily
      ? span(period === "month" ? 30 : 7).map((d) => ({ day: d, energy_kwh: daily[d] ?? 0, ratio: 0 }))
      : [];
    const max = Math.max(0, ...bars.map((b) => b.energy_kwh));
    for (const b of bars) b.ratio = max > 0 ? b.energy_kwh / max : 0;
    return {
      key: a.key,
      name: a.name,
      subentry_id: a.subentry_id,
      tracking: trackingOf(a),
      estimated: isEstimated(a),
      energy_kwh: energy,
      runs,
      used_days: usedDays,
      last_use: lastUse(a, registry),
      bars,
      series: usage?.series ?? null,
    };
  });
  return cards.sort(
    (a, b) => (b.energy_kwh ?? -1) - (a.energy_kwh ?? -1) || a.name.localeCompare(b.name) || a.key.localeCompare(b.key),
  );
}

/** The statistic ids to ask for (the appliances that have one), sorted. */
export function statisticIds(appliances: ApplianceInfo[]): string[] {
  return [...new Set(appliances.map((a) => a.statistic_id).filter((s): s is string => Boolean(s)))].sort();
}
