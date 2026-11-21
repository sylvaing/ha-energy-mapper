// The "To review" section of the panel (#10): pure helpers, unit tested.

import type {
  EntitySuggestion,
  ReviewListing,
  ReviewSignature,
  SignatureExample,
} from "./types.ts";

export interface ReviewGroups {
  proposed: ReviewSignature[];
  waiting: ReviewSignature[];
  ignored: ReviewSignature[];
}

/** The listing split by state; proposals with the most energy first (the
 * add-on's order is kept within a state). */
export function reviewGroups(listing: ReviewListing | null | undefined): ReviewGroups {
  const out: ReviewGroups = { proposed: [], waiting: [], ignored: [] };
  for (const s of listing?.signatures ?? []) {
    if (s.review_state === "proposed" || s.review_state === "waiting" || s.review_state === "ignored") {
      out[s.review_state].push(s);
    }
  }
  return out;
}

/** Whether the section has anything to show. */
export function hasReview(groups: ReviewGroups): boolean {
  return groups.proposed.length + groups.waiting.length + groups.ignored.length > 0;
}

/** Usual hours of a signature: the ranges of consecutive hours whose weight
 * is at least ``share`` of the busiest hour, the busiest ranges first (at
 * most ``max``). A range crossing midnight is one range ([22, 1]). */
export function usualHours(hours: number[] | undefined, share = 0.5, max = 3): Array<[number, number]> {
  const h = Array.from({ length: 24 }, (_, i) => Math.max(hours?.[i] ?? 0, 0));
  const peak = Math.max(...h);
  if (!(peak > 0)) return [];
  const on = h.map((v) => v >= share * peak);
  if (on.every(Boolean)) return [[0, 23]];
  // start the scan after an hour that is off, so that a range crossing midnight is not split
  const first = on.indexOf(false);
  const ranges: Array<{ from: number; to: number; weight: number }> = [];
  let cur: { from: number; to: number; weight: number } | null = null;
  for (let k = 1; k <= 24; k++) {
    const i = (first + k) % 24;
    if (on[i]) {
      if (cur) {
        cur.to = i;
        cur.weight += h[i];
      } else cur = { from: i, to: i, weight: h[i] };
    } else if (cur) {
      ranges.push(cur);
      cur = null;
    }
  }
  if (cur) ranges.push(cur);
  return ranges
    .sort((a, b) => b.weight - a.weight)
    .slice(0, max)
    .map((r) => [r.from, r.to]);
}

/** "7 h", "7–9 h", "22–1 h" (the hour after the last one is the end). */
export function formatHours(ranges: Array<[number, number]>): string {
  return ranges.map(([a, b]) => (a === b ? `${a} h` : `${a}–${(b + 1) % 24} h`)).join(", ");
}

/** The time window shown on the chart for an example (ms, with a margin,
 * inside the day). */
export function exampleWindow(example: SignatureExample, start: number, end: number, margin_s = 1200): [number, number] {
  const a = Math.max(start, example.start - margin_s);
  const b = Math.min(end, Math.max(example.end, example.start + 60) + margin_s);
  return [a * 1000, b * 1000];
}

/** What validating does: a new appliance (from the signature, or driven
 * by a switch, or measured by a power sensor), or a part of an existing
 * appliance (merge, #43). */
export type LinkMode = "new" | "switch" | "sensor" | "merge";

export const SWITCH_DOMAINS = [
  "switch",
  "input_boolean",
  "light",
  "fan",
  "binary_sensor",
  "climate",
  "humidifier",
  "water_heater",
  "media_player",
];
/** A sensor of this device class (an operation state) is linked like a switch. */
export const STATE_SENSOR_CLASS = "enum";

/** Entity domains offered for a link mode (sensors: power sensors for
 * "sensor", operation states for "switch"). */
export function linkDomains(mode: LinkMode): string[] {
  return mode === "sensor" ? ["sensor"] : mode === "switch" ? [...SWITCH_DOMAINS, "sensor"] : [];
}

/** Whether an entity can be linked in a mode. */
export function linkable(mode: LinkMode, entityId: string, attributes?: Record<string, unknown>): boolean {
  const domain = entityId.split(".")[0];
  if (!linkDomains(mode).includes(domain)) return false;
  if (domain !== "sensor") return true;
  const dc = attributes?.device_class;
  return mode === "sensor" ? dc !== STATE_SENSOR_CLASS : dc === STATE_SENSOR_CLASS;
}

/** The best entity suggested for a signature, if any. */
export function bestSuggestion(sig: Pick<ReviewSignature, "suggestions">): EntitySuggestion | undefined {
  return sig.suggestions?.[0];
}

/** Whether a suggestion can drive the appliance as a switched load: its
 * state changes at the signature's steps. An attribute, or a state that only
 * contains the uses (a thermostat heating for an hour while its radiator
 * cycles), is a hint for the name or a merge only. */
export function drivesLoad(c: EntitySuggestion): boolean {
  return !c.attribute && (c.match ?? "edge") === "edge";
}

/** What picking a suggestion does: merge into its appliance, link it as a
 * switch, or a new appliance named after it. */
export function suggestionMode(c: EntitySuggestion): LinkMode {
  return c.appliance ? "merge" : drivesLoad(c) ? "switch" : "new";
}

/** "Living room (heating)": the entity, and for an attribute or a
 * containment the states in which the signature runs. */
export function suggestionLabel(c: EntitySuggestion, friendlyName?: (entityId: string) => string | undefined): string {
  const name = friendlyName?.(c.entity_id) ?? c.entity_id;
  return drivesLoad(c) || !c.on_states.length ? name : `${name} (${c.on_states.join(", ")})`;
}

export interface ReviewForm {
  id: string;
  mode: LinkMode;
  name: string;
  entity: string;
  /** merge: the key of the appliance */
  appliance: string;
  missing?: string;
}

/** The form after picking a suggestion. */
export function formForSuggestion(
  sigId: string,
  c: EntitySuggestion,
  friendlyName?: (entityId: string) => string | undefined,
): ReviewForm {
  const mode = suggestionMode(c);
  return {
    id: sigId,
    mode,
    name: friendlyName?.(c.entity_id) ?? "",
    entity: mode === "switch" ? c.entity_id : "",
    appliance: c.appliance?.key ?? "",
  };
}

/** The validation form a signature opens with, from its best suggestion
 * (``formForSuggestion``), else a new appliance. */
export function initialForm(
  sig: Pick<ReviewSignature, "id" | "suggestions">,
  friendlyName?: (entityId: string) => string | undefined,
): ReviewForm {
  const best = bestSuggestion(sig);
  if (!best) return { id: sig.id, mode: "new", name: "", entity: "", appliance: "" };
  return formForSuggestion(sig.id, best, friendlyName);
}

/** The websocket message of a validation, or the key of the missing field.
 * An entity the add-on suggested is linked with the states in which its
 * appliance runs (unless they are the default "on"). */
export function validateMessage(
  sig: Pick<ReviewSignature, "id" | "suggestions">,
  mode: LinkMode,
  name: string,
  entityId: string,
  attributes?: Record<string, unknown>,
  appliance = "",
): { msg: Record<string, unknown> } | { missing: "name" | "entity" | "appliance" } {
  if (mode === "merge") {
    if (!appliance) return { missing: "appliance" };
    return { msg: { type: "energy_mapper/merge_signature", signature_id: sig.id, appliance } };
  }
  const n = name.trim();
  if (!n) return { missing: "name" };
  const msg: Record<string, unknown> = { type: "energy_mapper/validate", signature_id: sig.id, name: n };
  if (mode !== "new") {
    const e = entityId.trim();
    if (!e || !linkable(mode, e, attributes)) return { missing: "entity" };
    msg.entity_id = e;
    const states = sig.suggestions?.find((s) => s.entity_id === e && drivesLoad(s))?.on_states ?? [];
    if (mode === "switch" && states.length && !(states.length === 1 && states[0] === "on")) {
      msg.on_states = [...states];
    }
  }
  return { msg };
}

/** Entities offered by the fallback picker (when Home Assistant's own entity
 * picker is not loaded): those of the mode's domains, power sensors only for
 * a sub-meter, operation-state sensors only for a switch, sorted by name. */
export function entityChoices(
  states: Record<string, { entity_id: string; attributes?: Record<string, unknown> }> | undefined,
  mode: LinkMode,
): Array<{ id: string; name: string }> {
  const out: Array<{ id: string; name: string }> = [];
  for (const [id, st] of Object.entries(states ?? {})) {
    if (!linkable(mode, id, st.attributes)) continue;
    if (mode === "sensor" && st.attributes?.device_class !== "power") continue;
    out.push({ id, name: String(st.attributes?.friendly_name ?? id) });
  }
  return out.sort((a, b) => a.name.localeCompare(b.name) || a.id.localeCompare(b.id));
}
