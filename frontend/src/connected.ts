// Learning from a connected appliance (#79): a washing machine, a dryer or a
// dishwasher that reports its operation state (run / stop), often its phase
// (drying, cooling...), a coarse energy counter and its programme, but no
// useful power sensor. The integration proposes the entities of a device
// (energy_mapper/connected_devices); the user confirms them in a form, and
// energy_mapper/teach starts the learning with `mode: "state"`. This module
// holds the form's logic and what a learning card says about it. Pure logic,
// unit tested, no DOM.

import { formatEnergy, formatNumber, formatPercent, type LocaleInfo } from "./format.ts";
import type { Translate } from "./i18n.ts";
import { roundedPower } from "./names.ts";
import type { Finding } from "./types.ts";

export interface ConnectedEntity {
  entity_id: string;
  name: string;
  /** what it can be: an operation or phase state, a programme, a counter, a power sensor */
  role: "state" | "program" | "energy" | "power";
  options: string[];
  unit?: string | null;
  /** whether the recorder keeps it (null: unknown) */
  recorded: boolean | null;
  appliance: { key: string; name: string } | null;
}

export interface ConnectedProposal {
  entity_id: string;
  on_states: string[];
  phase_entity: string | null;
  energy_entity: string | null;
  program_entity: string | null;
  power_entity: string | null;
}

export interface ConnectedDevice {
  device_id: string;
  name: string;
  /** the appliance that already uses one of its entities */
  appliance: { key: string; name: string } | null;
  entities: ConnectedEntity[];
  proposal: ConnectedProposal;
}

export interface ConnectedListing {
  /** false: the add-on is too old to learn from a state */
  supported: boolean;
  devices: ConnectedDevice[];
}

/** The form "Learn from a connected appliance": on an appliance (`key`), or
 * a new appliance named `name` (`key` null). */
export interface ConnectedForm {
  key: string | null;
  device: string;
  name: string;
  entity: string;
  on_states: string[];
  phase: string;
  energy: string;
  program: string;
  /** a new appliance: also tracked by its state (a switched load) */
  track: boolean;
  busy: boolean;
  error: string | null;
}

/** The devices, the one already used by the appliance `key` first. */
export function deviceChoices(listing: ConnectedListing | null, key: string | null): ConnectedDevice[] {
  const devices = [...(listing?.devices ?? [])];
  return devices.sort(
    (a, b) =>
      Number(b.appliance?.key === key && key !== null) - Number(a.appliance?.key === key && key !== null) ||
      a.name.localeCompare(b.name),
  );
}

/** The form filled with the device's proposal. */
export function formFor(device: ConnectedDevice | undefined, key: string | null, name = ""): ConnectedForm {
  const p = device?.proposal;
  return {
    key,
    device: device?.device_id ?? "",
    name: key === null ? (name || device?.name || "") : name,
    entity: p?.entity_id ?? "",
    on_states: [...(p?.on_states ?? [])],
    phase: p?.phase_entity ?? "",
    energy: p?.energy_entity ?? "",
    program: p?.program_entity ?? "",
    track: key === null && !device?.appliance,
    busy: false,
    error: null,
  };
}

/** Another operation state picked: its running states are proposed again. */
export function withState(form: ConnectedForm, device: ConnectedDevice | undefined, entity: string): ConnectedForm {
  const options = stateOptions(device, entity);
  const kept = form.on_states.filter((s) => options.includes(s));
  const proposed = device?.proposal.entity_id === entity ? device.proposal.on_states : [];
  return { ...form, entity, on_states: kept.length ? kept : [...proposed], phase: form.phase === entity ? "" : form.phase };
}

export function stateOptions(device: ConnectedDevice | undefined, entity: string): string[] {
  return device?.entities.find((e) => e.entity_id === entity)?.options ?? [];
}

export function toggleState(form: ConnectedForm, state: string): ConnectedForm {
  const on = form.on_states.includes(state) ? form.on_states.filter((s) => s !== state) : [...form.on_states, state];
  return { ...form, on_states: on, error: null };
}

/** The entities of a role, for the selects of the form. */
export function entitiesOf(device: ConnectedDevice | undefined, ...roles: ConnectedEntity["role"][]): ConnectedEntity[] {
  return (device?.entities ?? []).filter((e) => roles.includes(e.role));
}

export function canStart(form: ConnectedForm): boolean {
  return !form.busy && Boolean(form.entity) && form.on_states.length > 0 && (form.key !== null || Boolean(form.name.trim()));
}

/** The websocket message that starts the learning. */
export function teachMessage(form: ConnectedForm): Record<string, unknown> {
  const msg: Record<string, unknown> = {
    type: "energy_mapper/teach",
    entity_id: form.entity,
    mode: "state",
    on_states: form.on_states,
    phase_entity: form.phase || null,
    energy_entity: form.energy || null,
    program_entity: form.program || null,
  };
  if (form.key !== null) msg.appliance = form.key;
  else {
    msg.name = form.name.trim();
    msg.track = form.track;
  }
  return msg;
}

/** The power sensors the last analysis found flat (stuck while their
 * device's energy counter increases: `flat_power_sensor` findings). */
export function flatPowerSensors(findings: Finding[] | undefined | null): Set<string> {
  const out = new Set<string>();
  for (const f of findings ?? []) {
    const power = f.params?.power_entity;
    if (f.kind === "flat_power_sensor" && typeof power === "string") out.add(power);
  }
  return out;
}

/** What the form warns about: the chosen entities the recorder does not
 * keep, and a flat power sensor of the device. */
export function formNotes(
  form: ConnectedForm,
  device: ConnectedDevice | undefined,
  flat: Set<string>,
  t: Translate,
): string[] {
  const notes: string[] = [];
  const chosen = [form.entity, form.phase, form.energy, form.program].filter(Boolean);
  for (const id of chosen) {
    const e = device?.entities.find((x) => x.entity_id === id);
    if (e && e.recorded === false) {
      notes.push(id === form.program ? t("connected_program_unrecorded", { name: e.name }) : t("connected_unrecorded", { name: e.name }));
    }
  }
  const power = device?.proposal.power_entity;
  if (power && flat.has(power)) {
    const name = device?.entities.find((x) => x.entity_id === power)?.name ?? power;
    notes.push(t("connected_flat_power", { name }));
  }
  return notes;
}

export interface NamedPhase {
  state: string;
  power_w: number;
  duration_s: number;
}

export interface ProgramInfo {
  program: string;
  uses: number;
  kind: string;
  power_w: number;
  duration_s: number;
  energy_kwh: number;
}

export interface ConnectedLearned {
  source?: string | null;
  measured_kwh?: number | null;
  counter_ratio?: number | null;
  counter_kwh?: number | null;
  named_phases?: NamedPhase[];
  program?: string | null;
  programs?: ProgramInfo[];
  energy_kwh?: number;
}

/** The lines a card learning from a connected appliance adds: the energy of
 * a use (checked against its counter), its phases by the phase state, its
 * programmes, and a programme state the recorder does not keep. */
export function connectedLines(
  learned: ConnectedLearned | null | undefined,
  programRecorded: boolean | null | undefined,
  programName: string | null,
  t: Translate,
  info: LocaleInfo,
): string[] {
  const lines: string[] = [];
  if (learned && learned.source === "state") {
    if (learned.measured_kwh != null) {
      const energy = formatEnergy(learned.energy_kwh ?? learned.measured_kwh, info);
      lines.push(
        learned.counter_ratio != null
          ? t("connected_energy_counter", {
              energy,
              ratio: formatPercent(learned.counter_ratio, info),
              counted: formatEnergy(learned.counter_kwh ?? 0, info),
            })
          : learned.counter_kwh != null
            ? t("connected_energy_counter_low", { energy, counted: formatEnergy(learned.counter_kwh, info) })
            : t("connected_energy", { energy }),
      );
    }
    const phases = (learned.named_phases ?? []).slice(0, 6);
    if (phases.length) {
      lines.push(
        t("connected_phases", {
          list: phases.map((p) => `${p.state} ${roundedPower(p.power_w, info)}`).join(" · "),
        }),
      );
    }
    const programs = learned.programs ?? [];
    if (programs.length > 1) {
      lines.push(
        t("connected_programs", {
          list: programs
            .slice(0, 5)
            .map((p) => `${p.program} (${formatNumber(p.uses, info)})`)
            .join(", "),
        }),
      );
    }
  }
  if (programRecorded === false) lines.push(t("connected_program_unrecorded", { name: programName ?? "" }));
  return lines;
}
