// Pure data shaping of the reports for the panel (unit tested, no DOM).

import { formatDuration, formatNumber, formatPower, formatTime, type LocaleInfo } from "./format.ts";
import type { Translate } from "./i18n.ts";
import type { NameSource } from "./names.ts";
import type { CurvePoint, Days, Peak, PeakPart, Registry, RegistrySignature, Report } from "./types.ts";

export type SeriesKind = "base" | "submetered" | "estimated" | "known" | "signature" | "other";

export interface StackSeries {
  /** stable id, also used for its colour: "base", "known:<name>", "sig:<registry id>"... */
  id: string;
  kind: SeriesKind;
  /** key of the value in the curve points (name of the known load, daily signature ids) */
  keys: string[];
  /** display name (translated by the panel for the fixed kinds) */
  name: string;
  /** signatures: the registry id (or daily id), shown as secondary text */
  ref?: string;
  energy_kwh: number;
}

/** Name of a signature without label (see names.ts): built from what it is. */
export type Namer = (sig: NameSource) => string;

/** Display name of the signatures: daily cluster id -> registry label, else a
 * provisional name built from the registry signature (or the daily one when it
 * has no registry match) by ``namer``, else the registry id. The unclassified
 * cluster "?" keeps its id. */
export function signatureNames(report: Report, registry: Registry | null, namer?: Namer): Map<string, string> {
  const byId = new Map<string, RegistrySignature>();
  for (const s of registry?.signatures ?? []) byId.set(s.id, s);
  const names = new Map<string, string>();
  for (const s of report.signatures ?? []) {
    const rid = s.registry_id ?? null;
    const reg = rid ? byId.get(rid) : undefined;
    if (reg?.label) names.set(s.id, reg.label);
    else if (s.id === "?" && !rid) names.set(s.id, s.id);
    else if (namer) names.set(s.id, namer(reg ?? s));
    else names.set(s.id, rid || s.id);
  }
  return names;
}

/** Display name of a registry signature: its label, else a provisional name, else its id. */
export function registryName(sig: RegistrySignature, namer?: Namer): string {
  return sig.label || (namer ? namer(sig) : sig.id);
}

/** Display name of the signature part of a peak (same rules as signatureNames). */
export function peakPartName(
  part: PeakPart,
  report: Report,
  registry: Registry | null,
  namer?: Namer,
): string {
  const rid = part.registry_id || null;
  const reg = rid ? registry?.signatures?.find((s) => s.id === rid) : undefined;
  if (reg?.label) return reg.label;
  if (part.name === "?" && !rid) return part.name;
  const daily = report.signatures?.find((s) => s.id === part.name);
  const source = reg ?? daily;
  if (namer && source) return namer(source);
  return rid || part.name;
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
export function stackSeries(report: Report, registry: Registry | null, namer?: Namer): StackSeries[] {
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
  const names = signatureNames(report, registry, namer);
  const dailyIds = new Set<string>(Object.keys(e.signatures ?? {}));
  for (const p of report.curve) for (const k of Object.keys(p.signatures ?? {})) dailyIds.add(k);
  const labelled = new Set((registry?.signatures ?? []).filter((r) => r.label).map((r) => r.id));
  const groups = new Map<string, StackSeries>();
  for (const id of dailyIds) {
    const rid = registryIdOf(report, id);
    const sid = `sig:${rid}`;
    const group = groups.get(sid) ?? {
      id: sid,
      kind: "signature" as const,
      keys: [],
      name: names.get(id) ?? id,
      // the id only for debugging the unnamed ones
      ref: labelled.has(rid) || rid === "?" ? undefined : rid,
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

/** The categories of the split bar of the day (#48), in display order. */
export const SPLIT_KEYS = ["base", "measured", "estimated", "named", "to_name", "other"] as const;
export type SplitKey = (typeof SPLIT_KEYS)[number];

export interface SplitPart {
  key: SplitKey;
  energy_kwh: number;
  /** share of the sum of the parts (0..1) */
  share: number;
}

/** Registry ids of the signatures that have a name: a label, or merged into an appliance. */
export function namedSignatureIds(registry: Registry | null): Set<string> {
  return new Set((registry?.signatures ?? []).filter((r) => r.label || r.appliance).map((r) => r.id));
}

/** Category of the split bar a stacked series belongs to: switched loads and
 * labelled signatures are named appliances, the other signatures (and the
 * unclassified cluster) are still to name. */
export function seriesCategory(series: Pick<StackSeries, "id" | "kind">, named: Set<string>): SplitKey {
  switch (series.kind) {
    case "base":
      return "base";
    case "submetered":
      return "measured";
    case "estimated":
      return "estimated";
    case "other":
      return "other";
    case "known":
      return "named";
    case "signature":
      return named.has(series.id.slice("sig:".length)) ? "named" : "to_name";
  }
}

/** Energy of each category of the day, in display order, empty ones left out.
 * Reports without `estimated` count all the sub-metered energy as measured. */
export function splitParts(report: Report, registry: Registry | null): SplitPart[] {
  const named = namedSignatureIds(registry);
  const totals = new Map<SplitKey, number>(SPLIT_KEYS.map((k) => [k, 0]));
  for (const s of stackSeries(report, registry)) {
    const key = seriesCategory(s, named);
    totals.set(key, totals.get(key)! + Math.max(s.energy_kwh, 0));
  }
  const total = [...totals.values()].reduce((a, b) => a + b, 0);
  return SPLIT_KEYS.filter((k) => totals.get(k)! >= 0.0005).map((key) => ({
    key,
    energy_kwh: totals.get(key)!,
    share: total > 0 ? totals.get(key)! / total : 0,
  }));
}

/** Ids of the stacked series of one category (to highlight them in the chart). */
export function categorySeries(stack: StackSeries[], key: SplitKey, registry: Registry | null): string[] {
  const named = namedSignatureIds(registry);
  return stack.filter((s) => seriesCategory(s, named) === key).map((s) => s.id);
}

/** One row of the "What used energy" list of the Today tab (#50). */
export interface UsageRow {
  /** the id of its chart series (same colour, same highlight) */
  id: string;
  kind: SeriesKind;
  /** display name (translated by the panel for the fixed kinds) */
  name: string;
  energy_kwh: number;
  /** energy relative to the largest row (0..1), for the bar */
  ratio: number;
  /** a signature that has no name yet (no label, not merged into an
   * appliance), the unclassified cluster included */
  unnamed: boolean;
  /** signatures: the registry id (for the review), when there is one */
  registry_id: string | null;
  /** signatures: activations; switched loads: on periods */
  runs: number | null;
  cycling: boolean;
  /** cold appliances (#89): the share of the energy (0..1) that is an
   * estimate, where the appliance could not be followed; null otherwise */
  estimated: number | null;
  /** first and last second with some power, null when unknown */
  span: [number, number] | null;
  /** switched loads: time on (s) */
  on_s: number | null;
  /** base load: its power (W) */
  power_w: number | null;
}

/** Spacing of the curve points (s): the smallest gap, 60 s by default. */
function curveStep(curve: CurvePoint[]): number {
  let step = Infinity;
  for (let i = 1; i < curve.length; i++) {
    const d = curve[i].t - curve[i - 1].t;
    if (d > 0 && d < step) step = d;
  }
  return Number.isFinite(step) ? step : 60;
}

/** The periods (s) where a series has some power in the curve: consecutive
 * points with power make one period, a gap in the curve ends it. */
export function seriesPeriods(curve: CurvePoint[], series: StackSeries): [number, number][] {
  const step = curveStep(curve);
  const out: [number, number][] = [];
  let prev: number | null = null;
  for (const p of curve) {
    if (seriesValue(p, series) > 0) {
      if (prev !== null && p.t - prev <= step * 1.5) out[out.length - 1][1] = p.t + step;
      else out.push([p.t, p.t + step]);
      prev = p.t;
    } else prev = null;
  }
  return out;
}

/** The rows of the list: one per stacked series with some energy, by
 * decreasing energy, the unexplained rest last. */
export function usageRows(report: Report, registry: Registry | null, namer?: Namer): UsageRow[] {
  const named = namedSignatureIds(registry);
  const curve = report.curve ?? [];
  const rows: UsageRow[] = [];
  for (const s of stackSeries(report, registry, namer)) {
    if (!(s.energy_kwh >= 0.0005)) continue;
    const periods = s.kind === "base" || s.kind === "other" ? [] : seriesPeriods(curve, s);
    const curveSpan: [number, number] | null = periods.length ? [periods[0][0], periods[periods.length - 1][1]] : null;
    const row: UsageRow = {
      id: s.id,
      kind: s.kind,
      name: s.name,
      energy_kwh: s.energy_kwh,
      ratio: 0,
      unnamed: false,
      registry_id: null,
      runs: null,
      cycling: false,
      estimated: null,
      span: curveSpan,
      on_s: null,
      power_w: null,
    };
    if (s.kind === "base") row.power_w = report.energy_kwh.base_w ?? null;
    if (s.kind === "known") {
      row.runs = periods.length || null;
      row.on_s = report.known_loads?.find((k) => k.name === s.name)?.on_s ?? null;
    }
    if (s.kind === "signature") {
      const rid = s.id.slice("sig:".length);
      const keys = new Set(s.keys);
      const daily = (report.signatures ?? []).filter((d) => keys.has(d.id));
      row.registry_id = daily.some((d) => d.registry_id) ? rid : null;
      row.unnamed = !named.has(rid);
      const acts = (report.activations ?? []).filter((a) => keys.has(a.signature));
      row.cycling = acts.length ? acts.some((a) => a.cycling) : daily.some((d) => d.kind === "cycling");
      if (acts.length) {
        row.runs = acts.length;
        row.span = [Math.min(...acts.map((a) => a.start)), Math.max(...acts.map((a) => a.end))];
      } else {
        const count = daily.reduce((n, d) => n + (d.count ?? 0), 0);
        row.runs = count || null;
      }
      const cold = daily.filter((d) => d.kind === "cold");
      if (cold.length) {
        // a cold appliance (#89) runs in cycles; a part of its energy may be an estimate
        row.cycling = true;
        const total = cold.reduce((n, d) => n + (d.energy_kwh ?? 0), 0);
        const estimated = cold.reduce((n, d) => n + (d.estimated_kwh ?? 0), 0);
        row.estimated = total > 0 && estimated > 0 ? Math.min(estimated / total, 1) : null;
      }
    }
    rows.push(row);
  }
  rows.sort(
    (a, b) =>
      Number(a.kind === "other") - Number(b.kind === "other") ||
      b.energy_kwh - a.energy_kwh ||
      a.name.localeCompare(b.name),
  );
  const max = Math.max(0, ...rows.map((r) => r.energy_kwh));
  for (const r of rows) r.ratio = max > 0 ? r.energy_kwh / max : 0;
  return rows;
}

/** "19:02 – 19:53", one time when both are the same minute. */
export function timeSpan(span: [number, number], info: LocaleInfo): string {
  const [a, b] = [formatTime(span[0], info), formatTime(span[1], info)];
  return a === b ? a : `${a} – ${b}`;
}

/** The short detail of a row: "1 run · 19:02 – 19:53", "to name · in cycles ·
 * 18:00 – 22:30", "2 runs · on 2 h 25", "measured · 09:00 – 15:00", "221 W
 * always on"; empty when there is nothing to say. */
export function usageDetail(row: UsageRow, t: Translate, info: LocaleInfo): string {
  const span = row.span ? timeSpan(row.span, info) : null;
  const runs =
    row.runs == null
      ? null
      : row.runs === 1
        ? t("usage_runs_one")
        : t("usage_runs", { count: formatNumber(row.runs, info) });
  const parts: (string | null)[] = [];
  switch (row.kind) {
    case "base":
      parts.push(row.power_w != null ? t("always_on", { power: formatPower(row.power_w, info) }) : null);
      break;
    case "submetered":
      parts.push(t("usage_measured"), span);
      break;
    case "estimated":
      parts.push(t("usage_estimated"), span);
      break;
    case "other":
      break;
    case "known":
      parts.push(runs, row.on_s ? t("usage_on", { duration: formatDuration(row.on_s, info) }) : span);
      break;
    case "signature":
      parts.push(row.unnamed ? t("usage_to_name") : null, row.cycling ? t("usage_cycles") : runs, span);
      // said plainly: an estimate, not a detection
      if (row.estimated != null && row.estimated >= 0.005) {
        parts.push(t("usage_partly_estimated", { share: formatNumber(Math.round(row.estimated * 100), info) }));
      }
      break;
  }
  return parts.filter((p): p is string => Boolean(p)).join(" · ");
}

/** Time (s) of the highest house power of the day: the curve point at the
 * maximum, else the highest peak; null when unknown. */
export function maxPowerTime(report: Report): number | null {
  let best: { t: number; w: number } | null = null;
  for (const p of report.curve ?? []) if (!best || p.house > best.w) best = { t: p.t, w: p.house };
  if (best) return best.t;
  let peak: Peak | null = null;
  for (const p of report.peaks ?? []) if (!peak || p.house_w > peak.house_w) peak = p;
  return peak ? peak.max_ts : null;
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
