// Pure data shaping of the reports for the panel (unit tested, no DOM).

import type { CurvePoint, Days, Peak, Registry, Report } from "./types.ts";

export type SeriesKind = "base" | "submetered" | "estimated" | "known" | "signature" | "other";

export interface StackSeries {
  /** stable id, also used for its colour: "base", "known:<name>", "sig:<registry id>"... */
  id: string;
  kind: SeriesKind;
  /** key of the value in the curve points (name of the known load, daily signature ids) */
  keys: string[];
  /** display name (translated by the panel for the fixed kinds) */
  name: string;
  energy_kwh: number;
}

/** Display name of the signatures: daily cluster id -> registry label, else registry id. */
export function signatureNames(report: Report, registry: Registry | null): Map<string, string> {
  const labels = new Map<string, string>();
  for (const s of registry?.signatures ?? []) if (s.label) labels.set(s.id, s.label);
  const names = new Map<string, string>();
  for (const s of report.signatures ?? []) {
    const rid = s.registry_id ?? null;
    names.set(s.id, (rid && labels.get(rid)) || rid || s.id);
  }
  return names;
}

/** Registry id of a daily signature (itself when it has none). */
export function registryIdOf(report: Report, dailyId: string): string {
  return report.signatures?.find((s) => s.id === dailyId)?.registry_id || dailyId;
}

function sortByEnergy<T extends { energy_kwh: number; name: string }>(items: T[]): T[] {
  return items.sort((a, b) => b.energy_kwh - a.energy_kwh || a.name.localeCompare(b.name));
}

/** The stacked series, bottom to top: base load, sub-meters, switched loads,
 * signatures (by decreasing energy), then the unexplained rest. Several daily
 * clusters matched to the same registry signature share one series. */
export function stackSeries(report: Report, registry: Registry | null): StackSeries[] {
  const e = report.energy_kwh;
  const out: StackSeries[] = [{ id: "base", kind: "base", keys: ["base"], name: "base", energy_kwh: e.base }];
  // sub-meters: what plugs and sensors measure, then the estimates subtracted
  // with them (reports older than add-on 0.8.1 have no `estimated`: all measured)
  const estimated = e.estimated ?? 0;
  const measured = Math.max((e.submetered ?? 0) - estimated, 0);
  if (measured > 0 || report.curve.some((p) => p.submetered - (p.estimated ?? 0) > 0)) {
    out.push({ id: "submetered", kind: "submetered", keys: ["submetered"], name: "submetered", energy_kwh: measured });
  }
  if (estimated > 0 || report.curve.some((p) => (p.estimated ?? 0) > 0)) {
    out.push({ id: "estimated", kind: "estimated", keys: ["estimated"], name: "estimated", energy_kwh: estimated });
  }
  const knownNames = new Set<string>(Object.keys(e.known ?? {}));
  for (const p of report.curve) for (const k of Object.keys(p.known ?? {})) knownNames.add(k);
  out.push(
    ...sortByEnergy(
      [...knownNames].map((name) => ({
        id: `known:${name}`,
        kind: "known" as const,
        keys: [name],
        name,
        energy_kwh: e.known?.[name] ?? 0,
      })),
    ),
  );
  const names = signatureNames(report, registry);
  const dailyIds = new Set<string>(Object.keys(e.signatures ?? {}));
  for (const p of report.curve) for (const k of Object.keys(p.signatures ?? {})) dailyIds.add(k);
  const groups = new Map<string, StackSeries>();
  for (const id of dailyIds) {
    const rid = registryIdOf(report, id);
    const sid = `sig:${rid}`;
    const group = groups.get(sid) ?? {
      id: sid,
      kind: "signature" as const,
      keys: [],
      name: names.get(id) ?? id,
      energy_kwh: 0,
    };
    group.keys.push(id);
    group.energy_kwh += e.signatures?.[id] ?? 0;
    groups.set(sid, group);
  }
  out.push(...sortByEnergy([...groups.values()]));
  out.push({ id: "other", kind: "other", keys: ["other"], name: "other", energy_kwh: e.other });
  return out;
}

/** Power of one stacked series at one point (W). */
export function seriesValue(point: CurvePoint, series: StackSeries): number {
  switch (series.kind) {
    case "base":
      return point.base ?? 0;
    case "submetered":
      return Math.max((point.submetered ?? 0) - (point.estimated ?? 0), 0);
    case "estimated":
      return point.estimated ?? 0;
    case "other":
      return point.other ?? 0;
    case "known":
      return series.keys.reduce((sum, k) => sum + (point.known?.[k] ?? 0), 0);
    case "signature":
      return series.keys.reduce((sum, k) => sum + (point.signatures?.[k] ?? 0), 0);
  }
}

export interface Summary {
  house: number;
  grid_import: number;
  grid_export: number;
  pv: number;
  base: number;
  base_w: number | null;
  submetered: number;
  estimated: number;
  known: number;
  signatures: number;
  other: number;
  has_pv: boolean;
}

const sum = (values: Record<string, number> | undefined) =>
  Object.values(values ?? {}).reduce((a, b) => a + b, 0);

export function summary(report: Report): Summary {
  const e = report.energy_kwh;
  return {
    house: e.house ?? 0,
    grid_import: e.grid_import ?? 0,
    grid_export: e.grid_export ?? 0,
    pv: e.pv ?? 0,
    base: e.base ?? 0,
    base_w: e.base_w ?? null,
    submetered: Math.max((e.submetered ?? 0) - (e.estimated ?? 0), 0),
    estimated: e.estimated ?? 0,
    known: sum(e.known),
    signatures: sum(e.signatures),
    other: e.other ?? 0,
    has_pv: Boolean(report.config?.pv_entity) || (e.pv ?? 0) > 0 || (e.grid_export ?? 0) > 0,
  };
}

/** 32-bit FNV-1a: a stable hash of the series ids. */
export function hash(text: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

/** Palette index of each id, picked among ``candidates``: derived from the
 * id itself, so a series keeps its colour from one day (and one reload) to
 * the next; collisions within the same chart move to the next free one. */
export function assignColors(ids: string[], candidates: number[]): Map<string, number> {
  const out = new Map<string, number>();
  const n = candidates.length;
  if (!n) return out;
  let used = new Set<number>();
  for (const id of [...new Set(ids)].sort()) {
    if (used.size >= n) used = new Set(); // more series than colours: reuse
    let i = hash(id) % n;
    while (used.has(i)) i = (i + 1) % n;
    used.add(i);
    out.set(id, candidates[i]);
  }
  return out;
}

/** Chart window (ms) around a peak, with ``margin_s`` on each side, kept inside the day. */
export function peakWindow(peak: Peak, start: number, end: number, margin_s = 1800): [number, number] {
  const a = Math.max(start, peak.start - margin_s);
  const b = Math.min(end, Math.max(peak.end, peak.start + 60) + margin_s);
  return [a * 1000, b * 1000];
}

/** Share of the house power that no part explains (0..1). */
export function unexplainedShare(peak: Peak): number {
  if (!peak.house_w) return 0;
  return Math.min(Math.max(peak.unexplained_w / peak.house_w, 0), 1);
}

export interface DayEntry {
  day: string;
  /** only a summary is left: no curve */
  summary: boolean;
}

/** All the days with a report, newest first. */
export function dayEntries(days: Days | null): DayEntry[] {
  if (!days) return [];
  const full = new Set(days.days ?? []);
  const all = new Set([...(days.days ?? []), ...(days.summary_days ?? [])]);
  return [...all].sort().reverse().map((day) => ({ day, summary: !full.has(day) }));
}

/** The day ``step`` days later (+1) or earlier (-1) among the available ones, or null. */
export function neighbourDay(entries: DayEntry[], day: string | null, step: 1 | -1): string | null {
  const sorted = entries.map((e) => e.day).sort();
  if (!day) return null;
  if (step > 0) return sorted.find((d) => d > day) ?? null;
  return [...sorted].reverse().find((d) => d < day) ?? null;
}

/** Hour profile scaled to 0..1 (24 values). */
export function hourProfile(hours: number[] | undefined): number[] {
  const h = Array.from({ length: 24 }, (_, i) => Math.max(hours?.[i] ?? 0, 0));
  const max = Math.max(...h);
  return max > 0 ? h.map((v) => v / max) : h;
}

/** Totals of one day (websocket command energy_mapper/day_stats). */
export interface DayStats {
  house_kwh: number | null;
  max_house_w: number | null;
  notable_peaks: number;
  last_notable: number | null;
}

export type PresetKey = "yesterday" | "day_before_yesterday" | "highest_consumption" | "last_notable_peak";

/** Days to ask energy_mapper/day_stats for: the available ones of the last ``window`` days. */
export function statsDays(entries: DayEntry[], today: string, window = 30): string[] {
  const from = shiftDay(today, -window);
  return entries.map((e) => e.day).filter((d) => d >= from && d < today);
}

function shiftDay(day: string, n: number): string {
  const [y, m, d] = day.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d + n)).toISOString().slice(0, 10);
}

/** The presets of the day picker, in display order; those that cannot be
 * computed (no data) are left out. ``today`` is the current day of Home Assistant. */
export function dayPresets(
  today: string,
  stats: Record<string, DayStats>,
  window = 30,
): { key: PresetKey; day: string }[] {
  const out: { key: PresetKey; day: string }[] = [
    { key: "yesterday", day: shiftDay(today, -1) },
    { key: "day_before_yesterday", day: shiftDay(today, -2) },
  ];
  const from = shiftDay(today, -window);
  const recent = Object.entries(stats)
    .filter(([day]) => day >= from && day < today)
    .sort(([a], [b]) => (a < b ? -1 : 1));
  let highest: [string, number] | null = null;
  for (const [day, s] of recent) {
    if (s.house_kwh != null && (!highest || s.house_kwh > highest[1])) highest = [day, s.house_kwh];
  }
  if (highest) out.push({ key: "highest_consumption", day: highest[0] });
  const notable = recent.filter(([, s]) => s.notable_peaks > 0).pop();
  if (notable) out.push({ key: "last_notable_peak", day: notable[0] });
  return out;
}
