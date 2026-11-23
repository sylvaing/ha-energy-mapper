// Readable provisional names of the signatures that have no label yet (#52):
// "2.6 kW appliance, ~50 min, in the evening" instead of "medium-2600w".
// Pure functions, unit tested.

import { formatDuration, formatNumber, type LocaleInfo } from "./format.ts";
import type { StringKey, Translate } from "./i18n.ts";

/** What a name is built from: a registry signature carries everything, a
 * daily signature has no hours; any field may be missing. */
export interface NameSource {
  kind?: string | null;
  power_w?: number | null;
  duration_s?: number | null;
  hours?: number[] | null;
  /** cold appliances (#89): how often a run starts */
  period_s?: number | null;
}

export type TimeOfDay = "morning" | "midday" | "afternoon" | "evening" | "night";

/** The parts of the day, in display order, with their hours. */
const PERIODS: Array<[TimeOfDay, number[]]> = [
  ["morning", [6, 7, 8, 9, 10]],
  ["midday", [11, 12, 13]],
  ["afternoon", [14, 15, 16, 17]],
  ["evening", [18, 19, 20, 21]],
  ["night", [22, 23, 0, 1, 2, 3, 4, 5]],
];

/** When a signature usually runs, from its 24-hour histogram: one part of
 * the day, two (e.g. midday and evening), "all_day" when the uses are
 * spread, or null without any hour. */
export function dominantTimeOfDay(hours: number[] | null | undefined): TimeOfDay[] | "all_day" | null {
  const h = Array.from({ length: 24 }, (_, i) => {
    const v = Number(hours?.[i] ?? 0);
    return Number.isFinite(v) && v > 0 ? v : 0;
  });
  const total = h.reduce((a, b) => a + b, 0);
  if (!(total > 0)) return null;
  const shares = PERIODS.map(([p, hs], order) => ({ p, order, share: hs.reduce((a, i) => a + h[i], 0) / total }));
  const [first, second] = [...shares].sort((a, b) => b.share - a.share || a.order - b.order);
  if (first.share >= 0.6) return [first.p];
  if (second.share >= 0.25 && first.share + second.share >= 0.75) {
    return [first, second].sort((a, b) => a.order - b.order).map((x) => x.p);
  }
  if (first.share >= 0.45) return [first.p];
  return "all_day";
}

/** Power rounded for a name: "45 W", "940 W", "1 kW", "2.6 kW", "12 kW". */
export function roundedPower(w: number, info: LocaleInfo): string {
  const a = Math.abs(w);
  if (a < 100) return `${formatNumber(Math.round(w), info)} W`;
  if (a < 995) return `${formatNumber(Math.round(w / 10) * 10, info)} W`;
  const kw = a < 9950 ? Math.round(w / 100) / 10 : Math.round(w / 1000);
  return `${formatNumber(kw, info, Number.isInteger(kw) ? 0 : 1)} kW`;
}

/** Duration rounded for a name: to 5 s under a minute, the minute under 10
 * minutes, 5 minutes under an hour, then the half hour. */
export function roundedDuration(seconds: number): number {
  if (seconds < 57.5) return Math.max(Math.round(seconds / 5) * 5, 5);
  if (seconds < 570) return Math.round(seconds / 60) * 60;
  if (seconds < 3450) return Math.round(seconds / 300) * 300;
  return Math.round(seconds / 1800) * 1800;
}

const valid = (v: number | null | undefined): v is number => typeof v === "number" && Number.isFinite(v) && v > 0;

/** "2.6 kW appliance, ~50 min, in the evening"; cycling: "900 W appliance in
 * cycles, in the evening"; multi-phase (#26): "Cycle with 2 kW heating,
 * ~1 h 30, in the evening"; cold (#89): "Runs in cycles all day, about 80 W,
 * every 45 min". Missing fields leave their part out. */
export function provisionalName(sig: NameSource, t: Translate, info: LocaleInfo): string {
  if (sig.kind === "cold") {
    const parts: string[] = [t("name_cold")];
    if (valid(sig.power_w)) parts.push(t("name_cold_power", { power: roundedPower(sig.power_w, info) }));
    if (valid(sig.period_s)) {
      parts.push(t("name_cold_every", { period: formatDuration(roundedDuration(sig.period_s), info) }));
    }
    return parts.join(", ");
  }
  const cycling = sig.kind === "cycling";
  const multiphase = sig.kind === "multiphase";
  const power = valid(sig.power_w) ? roundedPower(sig.power_w, info) : null;
  const named = multiphase ? "name_power_multiphase" : cycling ? "name_power_cycles" : "name_power";
  const unknown = multiphase ? "name_unknown_multiphase" : cycling ? "name_unknown_cycles" : "name_unknown";
  const parts: string[] = [power ? t(named, { power }) : t(unknown)];
  if (!cycling && valid(sig.duration_s)) parts.push(`~${formatDuration(roundedDuration(sig.duration_s), info)}`);
  const when = dominantTimeOfDay(sig.hours);
  if (when === "all_day") parts.push(t("tod_all_day"));
  else if (when?.length === 1) parts.push(t(`tod_${when[0]}` as StringKey));
  else if (when?.length === 2) {
    parts.push(t("tod_pair", { a: t(`tod_${when[0]}` as StringKey), b: t(`tod_${when[1]}` as StringKey) }));
  }
  return parts.join(", ");
}

/** One phase of a multi-phase cycle (#26), as the registry stores it. */
export interface PhaseInfo {
  kind: string;
  power_w: number;
  duration_s: number;
}

/** "heating 2.1 kW 20 min → motor 100 W 30 min → …": the phases of a
 * multi-phase signature in order; at most ``max`` of them, then "…". */
export function phasesSummary(phases: PhaseInfo[] | null | undefined, t: Translate, info: LocaleInfo, max = 6): string {
  const valid = (phases ?? []).filter((p) => Number.isFinite(p?.power_w) && Number.isFinite(p?.duration_s));
  const parts = valid.slice(0, max).map((p) => {
    const kind = t(p.kind === "heat" ? "phase_heat" : "phase_motor");
    return `${kind} ${roundedPower(p.power_w, info)} ${formatDuration(roundedDuration(p.duration_s), info)}`;
  });
  if (valid.length > max) parts.push("…");
  return parts.join(" → ");
}

/** The same names made distinct (the chart legend toggles series by name):
 * the second "X" becomes "X (2)", and so on, in the order given. */
export function distinctNames<K>(names: Map<K, string>): Map<K, string> {
  const count = new Map<string, number>();
  const out = new Map<K, string>();
  for (const [key, name] of names) {
    const n = (count.get(name) ?? 0) + 1;
    count.set(name, n);
    out.set(key, n === 1 ? name : `${name} (${n})`);
  }
  return out;
}
