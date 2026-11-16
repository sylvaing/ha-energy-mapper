// Number, time and duration formatting following the user's Home Assistant
// profile (hass.locale), like the HA frontend does.

import type { HassLocale } from "./types.ts";

export interface LocaleInfo {
  language: string;
  number_format: string;
  time_format: string;
  /** IANA time zone used to display times */
  time_zone: string;
}

/** The locale settings of the panel, from hass (browser time zone when the
 * profile asks for "local" time). */
export function localeInfo(
  locale: HassLocale | undefined,
  language: string,
  serverTimeZone: string,
  browserTimeZone?: string,
): LocaleInfo {
  const local = browserTimeZone ?? Intl.DateTimeFormat().resolvedOptions().timeZone;
  return {
    language: locale?.language || language || "en",
    number_format: locale?.number_format || "language",
    time_format: locale?.time_format || "language",
    time_zone: locale?.time_zone === "local" ? local || serverTimeZone : serverTimeZone,
  };
}

/** Locale given to Intl for numbers (same mapping as the HA frontend). */
export function numberLocale(info: LocaleInfo): string | string[] | undefined {
  switch (info.number_format) {
    case "comma_decimal":
      return ["en-US", "en"];
    case "decimal_comma":
      return ["de", "es", "it"];
    case "space_comma":
      return ["fr", "sv", "cs"];
    case "system":
      return undefined;
    default:
      return info.language;
  }
}

export function formatNumber(value: number, info: LocaleInfo, digits = 0): string {
  const opts: Intl.NumberFormatOptions = {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  };
  if (info.number_format === "none") opts.useGrouping = false;
  try {
    return new Intl.NumberFormat(numberLocale(info), opts).format(value);
  } catch {
    return value.toFixed(digits);
  }
}

/** Energy: "12.3 kWh" (two decimals under 10 kWh). */
export function formatEnergy(kwh: number, info: LocaleInfo): string {
  const digits = Math.abs(kwh) < 10 ? 2 : 1;
  return `${formatNumber(kwh, info, digits)} kWh`;
}

/** Power: "850 W", "2.4 kW". */
export function formatPower(w: number, info: LocaleInfo): string {
  if (Math.abs(w) >= 1000) return `${formatNumber(w / 1000, info, Math.abs(w) >= 10000 ? 1 : 2)} kW`;
  return `${formatNumber(w, info, 0)} W`;
}

export function formatPercent(ratio: number, info: LocaleInfo): string {
  return `${formatNumber(ratio * 100, info, 0)} %`;
}

function hourOptions(info: LocaleInfo): Intl.DateTimeFormatOptions {
  if (info.time_format === "12") return { hour12: true };
  if (info.time_format === "24") return { hourCycle: "h23" };
  return {};
}

function dateLocale(info: LocaleInfo): string | undefined {
  return info.time_format === "system" ? undefined : info.language;
}

/** "14:05" in the display time zone (timestamp in seconds). */
export function formatTime(ts: number, info: LocaleInfo, seconds = false): string {
  return new Intl.DateTimeFormat(dateLocale(info), {
    hour: "numeric",
    minute: "2-digit",
    second: seconds ? "2-digit" : undefined,
    timeZone: info.time_zone,
    ...hourOptions(info),
  }).format(new Date(ts * 1000));
}

/** "Tue, March 9, 2027" for a "YYYY-MM-DD" day (no time zone shift). */
export function formatDay(day: string, info: LocaleInfo, style: "long" | "short" = "long"): string {
  const [y, m, d] = day.split("-").map(Number);
  const date = new Date(Date.UTC(y, m - 1, d, 12));
  return new Intl.DateTimeFormat(
    info.language,
    style === "long"
      ? { weekday: "short", year: "numeric", month: "long", day: "numeric", timeZone: "UTC" }
      : { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" },
  ).format(date);
}

/** "45 s", "3 min", "1 h 20 min" (unit symbols are the same in English and French). */
export function formatDuration(seconds: number, info: LocaleInfo): string {
  const s = Math.round(seconds);
  if (s < 60) return `${formatNumber(s, info)} s`;
  const min = Math.round(s / 60);
  if (min < 60) return `${formatNumber(min, info)} min`;
  const h = Math.floor(min / 60);
  const rest = min % 60;
  return rest ? `${formatNumber(h, info)} h ${String(rest).padStart(2, "0")}` : `${formatNumber(h, info)} h`;
}

/** "YYYY-MM-DD" of the day before ``ts`` in ``timeZone`` (yesterday when ts is now). */
export function previousDay(ts: number, timeZone: string): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date(ts * 1000));
  const get = (type: string) => Number(parts.find((p) => p.type === type)?.value);
  const date = new Date(Date.UTC(get("year"), get("month") - 1, get("day") - 1));
  return date.toISOString().slice(0, 10);
}

// -- days and dates in a time zone ---------------------------------------------

/** "YYYY-MM-DD" of an instant (ms, or Date) in ``timeZone``. */
export function dayOf(instant: number | Date, timeZone: string): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(instant);
}

/** The day ``n`` days after ``day`` (negative: before). */
export function addDays(day: string, n: number): string {
  const [y, m, d] = day.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d + n)).toISOString().slice(0, 10);
}

/** Offset (ms) of ``timeZone`` from UTC at the instant ``ms``. */
function offsetAt(ms: number, timeZone: string): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).formatToParts(ms);
  const get = (type: string) => Number(parts.find((p) => p.type === type)?.value);
  const wall = Date.UTC(get("year"), get("month") - 1, get("day"), get("hour"), get("minute"), get("second"));
  return wall - Math.floor(ms / 1000) * 1000;
}

/** Instant (ms) of the start of ``day`` in ``timeZone``. */
export function startOfDay(day: string, timeZone: string): number {
  const [y, m, d] = day.split("-").map(Number);
  const utc = Date.UTC(y, m - 1, d);
  let t = utc - offsetAt(utc, timeZone);
  t = utc - offsetAt(t, timeZone);
  return t;
}

/** [start, end] of ``day`` in ``timeZone`` (end: the last millisecond of the day). */
export function dayRange(day: string, timeZone: string): [Date, Date] {
  return [new Date(startOfDay(day, timeZone)), new Date(startOfDay(addDays(day, 1), timeZone) - 1)];
}
