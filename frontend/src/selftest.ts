// Nightly self-test (#15): each night the add-on hides a measured device (a
// sub-meter) from a copy of its analysis and checks that the whole-house
// curve finds its uses again. This module tells what the Self-test card of
// the Appliances tab shows. Pure logic, unit tested, no DOM.

import { formatNumber, formatPercent, formatPower, type LocaleInfo } from "./format.ts";
import type { Translate } from "./i18n.ts";

export type SelfTestState = "ok" | "not_enough_data" | "not_recognisable" | "no_use";
export type SelfTestTrend = "up" | "down" | "stable";

/** One device of energy_mapper/self_test. */
export interface SelfTestDevice {
  entity_id: string;
  name: string;
  appliance?: string | null;
  state: SelfTestState;
  /** nights the hidden day was analysed and evaluated */
  tests: number;
  uses: number;
  found: number;
  detections?: number;
  correct?: number;
  recall?: number | null;
  precision: number | null;
  energy_ratio: number | null;
  /** the share of the true uses found again; null while there are too few */
  rate: number | null;
  trend: SelfTestTrend | null;
  last_day: string;
  learned?: { power_w: number; recognisable?: boolean; visible?: boolean } | null;
}

export interface SelfTestListing {
  /** false: an add-on before 0.11.0 */
  supported: boolean;
  enabled: boolean;
  devices_per_night?: number;
  window_days?: number;
  min_uses?: number;
  last_day?: string | null;
  devices: SelfTestDevice[];
}

export type Tone = "good" | "fair" | "poor" | "none";

export interface SelfTestRow {
  entity_id: string;
  name: string;
  /** "Found again 83 % of the time", or why there is no rate */
  headline: string;
  /** tests, uses, precision and energy */
  detail: string;
  rate: number | null;
  tone: Tone;
  trend: SelfTestTrend | null;
  trendText: string;
}

/** The card is shown once the add-on has tested a device. */
export function showSelfTest(listing: SelfTestListing | null | undefined): boolean {
  return Boolean(listing?.supported && listing.devices.length > 0);
}

/** good from 80 %, fair from 50 %, else poor; none without a rate. */
export function toneOf(rate: number | null): Tone {
  if (rate == null) return "none";
  return rate >= 0.8 ? "good" : rate >= 0.5 ? "fair" : "poor";
}

/** A value that never wraps inside ("96 %", "2.4 kW"). */
const nb = (s: string): string => s.replace(/ /g, "\u00a0");

const TREND_KEYS = { up: "selftest_trend_up", down: "selftest_trend_down", stable: "selftest_trend_stable" } as const;

function detailOf(d: SelfTestDevice, t: Translate, info: LocaleInfo): string {
  const parts = [
    d.tests === 1 ? t("selftest_tests_one") : t("selftest_tests", { count: formatNumber(d.tests, info) }),
    t(d.found < 2 ? "selftest_found_one" : "selftest_found", { found: formatNumber(d.found, info), uses: formatNumber(d.uses, info) }),
  ];
  if (d.state === "ok") {
    if (d.precision != null) parts.push(t("selftest_precision", { value: nb(formatPercent(d.precision, info)) }));
    if (d.energy_ratio != null) parts.push(t("selftest_energy", { value: nb(formatPercent(d.energy_ratio, info)) }));
  }
  return parts.join(" · ");
}

/** One row per tested device, by name: the success rate ("found again X %
 * of the time") once the device has enough true uses, else why not, never a
 * misleading 0 %. ``nameOf`` gives the friendly name of an entity. */
export function selfTestRows(
  listing: SelfTestListing | null | undefined,
  t: Translate,
  info: LocaleInfo,
  nameOf: (entityId: string) => string | null = () => null,
): SelfTestRow[] {
  if (!showSelfTest(listing)) return [];
  const minUses = listing?.min_uses ?? 5;
  const rows = listing!.devices.map((d): SelfTestRow => {
    let headline: string;
    let detail = "";
    switch (d.state) {
      case "ok":
        headline = t("selftest_rate", { rate: nb(formatPercent(d.rate ?? 0, info)) });
        detail = detailOf(d, t, info);
        break;
      case "not_enough_data":
        headline = t("selftest_not_enough", { uses: formatNumber(d.uses, info), min: formatNumber(minUses, info) });
        detail = detailOf(d, t, info);
        break;
      case "not_recognisable":
        // too low for the curve's noise, or seen by it but under what is tracked (#88)
        headline = !d.learned?.power_w
          ? t("selftest_too_small_plain")
          : t(d.learned.visible ? "selftest_small_untracked" : "selftest_too_small", {
              power: nb(formatPower(d.learned.power_w, info)),
            });
        break;
      default:
        headline = t("selftest_no_use");
    }
    const rate = d.state === "ok" ? d.rate : null;
    const trend = d.state === "ok" ? d.trend : null;
    return {
      entity_id: d.entity_id,
      name: nameOf(d.entity_id) ?? d.name ?? d.entity_id,
      headline,
      detail,
      rate,
      tone: toneOf(rate),
      trend,
      trendText: trend ? t(TREND_KEYS[trend]) : "",
    };
  });
  return rows.sort((a, b) => a.name.localeCompare(b.name, info.language) || a.entity_id.localeCompare(b.entity_id));
}

/** The hint under the title: what the test does, over how many days, and
 * whether it is switched off. */
export function selfTestHint(listing: SelfTestListing, t: Translate, info: LocaleInfo): string {
  const days = formatNumber(listing.window_days ?? 60, info);
  const parts = [t("selftest_hint", { days })];
  if (!listing.enabled) parts.push(t("selftest_off"));
  return parts.join(" ");
}
