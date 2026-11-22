// Learning an appliance from a measured device (#14): a power sensor (a
// temporary smart plug) teaches the appliance its signature, then the
// appliance is tracked from the whole-house curve. The integration and the
// add-on do the work (energy_mapper/teach, teaching, stop_teaching); this
// module tells what an appliance card shows. Pure logic, unit tested, no DOM.

import type { ApplianceInfo } from "./appliances.ts";
import { formatNumber, formatPercent, type LocaleInfo } from "./format.ts";
import type { Translate } from "./i18n.ts";
import { connectedLines, type ConnectedLearned } from "./connected.ts";
import { phasesSummary, provisionalName, roundedPower, type PhaseInfo } from "./names.ts";

export interface TeacherInfo {
  entity_id: string;
  /** unix time the learning started */
  since?: number | null;
  /** "state": a connected appliance's operation state (#79); else a power sensor */
  mode?: "power" | "state";
  on_states?: string[];
  phase_entity?: string | null;
  energy_entity?: string | null;
  program_entity?: string | null;
}

export interface TeachingEvaluation {
  uses: number;
  found: number;
  detections: number;
  correct: number;
  precision: number | null;
  recall: number | null;
  f1: number | null;
  energy_ratio: number | null;
}

export interface TeachingLearned {
  kind: string;
  band: string;
  power_w: number;
  duration_s: number;
  uses: number;
  runs: number;
  phases: PhaseInfo[];
  recognisable: boolean;
}

/** What a connected appliance's learning adds (#79). */
export type TeachingLearnedFull = TeachingLearned & ConnectedLearned;

/** The learning status of one appliance (energy_mapper/teaching). */
export interface TeachingStatus {
  key: string;
  entity_id: string;
  since: number | null;
  days: number;
  uses: number;
  energy_kwh: number;
  learned: TeachingLearnedFull | null;
  /** "state": learning from a connected appliance (#79) */
  mode?: "power" | "state";
  program_entity?: string | null;
  /** false: its programme state is not recorded (null: no programme, or not known yet) */
  program_recorded?: boolean | null;
  signature: { id: string; in_registry: boolean; linked: boolean } | null;
  conflict: { id: string; appliance: string } | null;
  evaluation: TeachingEvaluation | null;
}

export interface TeachingListing {
  /** false: an add-on before 0.11.0, nothing can be learned */
  supported: boolean;
  teachers: TeachingStatus[];
}

/** What a card shows about learning:
 * - none: nothing (no listing, or the add-on is too old);
 * - idle: no teacher (administrators may start one);
 * - learning: a sensor teaches it; ``status`` once the add-on has some, and
 *   what keeps it from being finished (``blocker``). */
export type TeachState =
  | { kind: "none" }
  | { kind: "idle" }
  | {
      kind: "learning";
      teacher: TeacherInfo;
      status: TeachingStatus | null;
      blocker: "no_use" | "too_small" | "conflict" | null;
    };

export function teachState(listing: TeachingListing | null, appliance: ApplianceInfo | undefined): TeachState {
  if (!listing || !appliance) return { kind: "none" };
  const teacher = appliance.teacher;
  if (!teacher) return listing.supported ? { kind: "idle" } : { kind: "none" };
  const status = listing.teachers.find((s) => s.key === appliance.key && s.entity_id === teacher.entity_id) ?? null;
  const learned = status?.learned ?? null;
  const blocker = !learned ? "no_use" : !learned.recognisable ? "too_small" : status?.conflict ? "conflict" : null;
  return { kind: "learning", teacher, status, blocker };
}

type States = Record<string, { entity_id?: string; state?: string; attributes?: Record<string, unknown> }> | undefined;

const POWER_UNITS = new Set(["W", "kW"]);

/** The power sensors that may teach an appliance: not a source or the
 * teacher of another appliance; its own sensors (sub-meter sources) first,
 * marked as suggested, then the others by name. A sensor the last analysis
 * found flat (#79, stuck while its device counts energy) is marked and
 * last: it cannot teach. */
export function sensorChoices(
  states: States,
  appliances: ApplianceInfo[],
  key: string,
  flat: Set<string> = new Set(),
): { id: string; name: string; suggested: boolean; flat: boolean }[] {
  const taken = new Set<string>();
  const own = new Set<string>();
  for (const a of appliances) {
    const ids = [...a.sources.map((s) => s.entity_id), a.teacher?.entity_id].filter((x): x is string => Boolean(x));
    for (const id of ids) (a.key === key ? own : taken).add(id);
  }
  const out: { id: string; name: string; suggested: boolean; flat: boolean }[] = [];
  for (const [id, st] of Object.entries(states ?? {})) {
    if (!id.startsWith("sensor.") || taken.has(id)) continue;
    const attrs = st.attributes ?? {};
    if (attrs.device_class !== "power" && !POWER_UNITS.has(String(attrs.unit_of_measurement ?? ""))) continue;
    const isFlat = flat.has(id);
    out.push({ id, name: String(attrs.friendly_name ?? id), suggested: own.has(id) && !isFlat, flat: isFlat });
  }
  return out.sort(
    (a, b) =>
      Number(a.flat) - Number(b.flat) ||
      Number(b.suggested) - Number(a.suggested) ||
      a.name.localeCompare(b.name) ||
      a.id.localeCompare(b.id),
  );
}

/** The learned signature in words: its provisional name, and the phases of
 * a multi-phase one. */
export function learnedText(learned: TeachingLearned, t: Translate, info: LocaleInfo): string {
  const name = provisionalName({ kind: learned.kind, power_w: learned.power_w, duration_s: learned.duration_s }, t, info);
  const phases = learned.kind === "multiphase" ? phasesSummary(learned.phases, t, info, 4) : "";
  return phases ? `${name} (${phases})` : name;
}

/** The status lines of a card that is learning: what was seen, what was
 * learned, and how well the curve recognises it. */
export function teachingLines(
  state: Extract<TeachState, { kind: "learning" }>,
  t: Translate,
  info: LocaleInfo,
  applianceName: (key: string) => string,
  programName?: (entityId: string) => string,
): string[] {
  const st = state.status;
  if (!st || !st.days) return [t("teach_waiting")];
  const lines: string[] = [];
  const days = st.days === 1 ? t("teach_days_one") : t("teach_days", { count: formatNumber(st.days, info) });
  const uses = !st.uses ? t("teach_no_use") : st.uses === 1 ? t("teach_uses_one") : t("teach_uses", { count: formatNumber(st.uses, info) });
  lines.push(`${days} · ${uses}`);
  if (st.learned) lines.push(t("teach_learned", { name: learnedText(st.learned, t, info) }));
  if (state.blocker === "too_small" && st.learned) {
    lines.push(t("teach_too_small", { power: roundedPower(st.learned.power_w, info) }));
  } else if (state.blocker === "conflict" && st.conflict) {
    lines.push(t("teach_conflict", { name: applianceName(st.conflict.appliance) }));
  }
  // a connected appliance (#79): its energy, phases and programmes
  lines.push(...connectedLines(st.learned, st.program_recorded, programName?.(st.program_entity ?? "") ?? null, t, info));
  const ev = st.evaluation;
  if (st.learned && state.blocker !== "too_small" && ev && ev.uses) {
    lines.push(
      t("teach_accuracy", {
        found: formatNumber(ev.found, info),
        uses: formatNumber(ev.uses, info),
        recall: ev.recall == null ? "–" : formatPercent(ev.recall, info),
        precision: ev.precision == null ? "–" : formatPercent(ev.precision, info),
      }),
    );
  }
  return lines;
}
