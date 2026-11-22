// Findings ("things to check") in the user's language (#34): the add-on sends
// a stable kind and its parameters, the panel builds the sentence. The
// add-on's English `detail` is the fallback for a kind this panel does not
// know (a newer add-on) or a finding without the parameters it needs (an
// older add-on). Pure functions, unit tested.

import { formatEnergy, formatPower, type LocaleInfo } from "./format.ts";
import type { StringKey, Translate } from "./i18n.ts";
import type { Finding } from "./types.ts";

/** Friendly name of an entity in Home Assistant, if it has one. */
export type FriendlyName = (entityId: string) => string | undefined;

export interface FindingView {
  /** Short title ("No power sensor"); "Note" for an unknown kind. */
  title: string;
  /** The sentence, with the entity id in parentheses when the sentence does not show it. */
  text: string;
}

function str(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function num(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

/** The name a finding talks about: the name the add-on gives (the Energy
 * dashboard's), else the entity's friendly name, else the entity id. */
function nameOf(f: Finding, friendlyName: FriendlyName): string | undefined {
  const entity = str(f.entity_id);
  return str(f.params?.name) ?? (entity ? (str(friendlyName(entity)) ?? entity) : undefined);
}

/** The translated sentence of a finding, or null when this panel cannot build it. */
export function findingSentence(
  f: Finding,
  t: Translate,
  info: LocaleInfo,
  friendlyName: FriendlyName,
): string | null {
  const p = f.params ?? {};
  const name = nameOf(f, friendlyName);
  switch (f.kind) {
    case "no_grid":
      return t("finding_text_no_grid");
    case "grid_ignored":
    case "battery_sign":
    case "unavailable":
    case "unknown_statistic":
      return name ? t(`finding_text_${f.kind}` as StringKey, { name }) : null;
    case "no_power_sensor": {
      const source = str(p.source);
      if (!name || (source !== "grid" && source !== "solar" && source !== "device")) return null;
      return t(`finding_text_no_power_sensor_${source}` as StringKey, { name });
    }
    case "tracking_gap": {
      const declared = num(p.declared_kwh);
      const measured = num(p.measured_kwh);
      if (!name || declared === undefined || measured === undefined) return null;
      return t("finding_text_tracking_gap", {
        name,
        declared: formatEnergy(declared, info),
        measured: formatEnergy(measured, info),
      });
    }
    case "flat_power_sensor": {
      // a power sensor stuck while its device counts energy (#79)
      const power = num(p.power_w);
      const energy = num(p.energy_kwh);
      if (!name || power === undefined || energy === undefined) return null;
      return t("finding_text_flat_power_sensor", {
        name,
        power: formatPower(power, info),
        energy: formatEnergy(energy, info),
      });
    }
    default:
      return null;
  }
}

/** Title and sentence of a finding, as the panel lists it. */
export function findingView(f: Finding, t: Translate, info: LocaleInfo, friendlyName: FriendlyName): FindingView {
  const key = `finding_${f.kind}` as StringKey;
  const title = t(key) === key ? t("finding_other") : t(key);
  const sentence = findingSentence(f, t, info, friendlyName) ?? f.detail ?? "";
  const entity = str(f.entity_id);
  const text = [sentence, entity && !sentence.includes(entity) ? `(${entity})` : ""].filter(Boolean).join(" ");
  return { title, text };
}
