// Labelled examples (#17): what the nightly analysis says of the uses the
// user pointed at in the Live tab (`examples_status` of
// energy_mapper/appliances, the add-on's GET /api/examples). This module
// tells what an appliance card says of the examples the analysis does not
// track because they are under the step threshold (#100): seen by the
// whole-house curve, or too low for it. Pure logic, unit tested, no DOM.

import type { LocaleInfo } from "./format.ts";
import type { Translate } from "./i18n.ts";
import { roundedPower } from "./names.ts";

/** One labelled example, as the analysed days found it. */
export interface ExampleStatusItem {
  t_on: number;
  t_off: number | null;
  power_w: number | null;
  status: "found" | "other" | "pending" | string;
  /** why it is pending; `small_untracked` comes with add-on 0.18.2+ */
  reason: "too_small" | "small_untracked" | string | null;
  /** the day's small use it is (adaptive threshold), when one overlaps it */
  small_use?: { day: string; start: number; end: number; power_w: number } | null;
}

export interface ExamplesStatus {
  key: string;
  total: number;
  found: number;
  other: number;
  pending: number;
  examples: ExampleStatusItem[];
}

function median(values: number[]): number {
  const v = [...values].sort((a, b) => a - b);
  const mid = v.length >> 1;
  return v.length % 2 ? v[mid] : (v[mid - 1] + v[mid]) / 2;
}

/** The lines of an appliance card about its labelled examples under the
 * step threshold: those the whole-house curve shows (not tracked yet), then
 * those too low for it, each with their usual power. Worded like the
 * teaching lines (`teach_small_untracked`, `teach_too_small`). */
export function exampleLines(status: ExamplesStatus | null | undefined, t: Translate, info: LocaleInfo): string[] {
  const lines: string[] = [];
  for (const [reason, key] of [
    ["small_untracked", "examples_small_untracked"],
    ["too_small", "examples_too_small"],
  ] as const) {
    const powers = (status?.examples ?? []).filter((e) => e.status === "pending" && e.reason === reason).map((e) => e.power_w ?? 0);
    if (!powers.length) continue;
    lines.push(t(powers.length === 1 ? `${key}_one` : key, { power: roundedPower(median(powers), info) }));
  }
  return lines;
}
