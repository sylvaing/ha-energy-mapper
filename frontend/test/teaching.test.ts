import assert from "node:assert/strict";
import { test } from "node:test";

import type { ApplianceInfo } from "../src/appliances.ts";
import { localeInfo } from "../src/format.ts";
import { translator } from "../src/i18n.ts";
import { learnedText, sensorChoices, teachingLines, teachState, type TeachingListing, type TeachingStatus } from "../src/teaching.ts";

// synthetic household (no real data)
const OVEN: ApplianceInfo = {
  key: "submeter:sensor.oven_plug_power",
  name: "Oven",
  subentry_id: "s1",
  statistic_id: null,
  power_w: null,
  sources: [{ type: "submeter", entity_id: "sensor.oven_plug_power" }],
  teacher: { entity_id: "sensor.oven_plug_power", since: 1_800_000_000 },
};
const HEATER: ApplianceInfo = {
  key: "load:switch.water_heater",
  name: "Water heater",
  subentry_id: "s2",
  statistic_id: "energy_mapper:load_switch_water_heater_energy",
  power_w: 2000,
  sources: [{ type: "switched_load", entity_id: "switch.water_heater" }],
};

function status(extra: Partial<TeachingStatus> = {}): TeachingStatus {
  return {
    key: OVEN.key,
    entity_id: "sensor.oven_plug_power",
    since: 1_800_000_000,
    days: 6,
    uses: 5,
    energy_kwh: 6.1,
    learned: { kind: "steady", band: "medium", power_w: 2400, duration_s: 2700, uses: 5, runs: 5, phases: [], recognisable: true },
    signature: { id: "medium-2400w", in_registry: true, linked: false },
    conflict: null,
    evaluation: { uses: 5, found: 4, detections: 5, correct: 4, precision: 0.8, recall: 0.8, f1: 0.8, energy_ratio: 0.95 },
    ...extra,
  };
}

const listing = (teachers: TeachingStatus[] = [status()], supported = true): TeachingListing => ({ supported, teachers });
const info = localeInfo(undefined, "en", "UTC");
const t = translator("en");

test("teachState", () => {
  assert.deepEqual(teachState(null, OVEN), { kind: "none" });
  assert.deepEqual(teachState(listing(), undefined), { kind: "none" });
  assert.deepEqual(teachState(listing(), HEATER), { kind: "idle" });
  // an add-on too old: nothing to offer
  assert.deepEqual(teachState(listing([], false), HEATER), { kind: "none" });
  const s = teachState(listing(), OVEN);
  assert.equal(s.kind, "learning");
  assert.equal(s.kind === "learning" && s.blocker, null);
  // a status of another sensor is not this one
  const other = teachState(listing([status({ entity_id: "sensor.other" })]), OVEN);
  assert.equal(other.kind === "learning" && other.status, null);
  assert.equal(other.kind === "learning" && other.blocker, "no_use");
  const small = status({ learned: { ...status().learned!, power_w: 120, recognisable: false } });
  assert.equal((teachState(listing([small]), OVEN) as { blocker: string }).blocker, "too_small");
  const owned = status({ conflict: { id: "medium-2400w", appliance: "load:switch.water_heater" } });
  assert.equal((teachState(listing([owned]), OVEN) as { blocker: string }).blocker, "conflict");
});

test("teachingLines", () => {
  const lines = teachingLines(teachState(listing(), OVEN) as never, t, info, (k) => (k === HEATER.key ? HEATER.name : k));
  assert.deepEqual(lines, [
    "6 days · 5 uses seen",
    "Learned: 2.4 kW appliance, ~45 min",
    "Found from the curve: 4 of 5 uses (recall 80 %), precision 80 %",
  ]);
  const small = status({ learned: { ...status().learned!, power_w: 120, recognisable: false, visible: false }, evaluation: null });
  assert.equal(teachingLines(teachState(listing([small]), OVEN) as never, t, info, (k) => k)[2], t("teach_too_small", { power: "120 W" }));
  const seen = status({ learned: { ...small.learned!, visible: true } , evaluation: null });
  assert.equal(teachingLines(teachState(listing([seen]), OVEN) as never, t, info, (k) => k)[2], t("teach_small_untracked", { power: "120 W" }));
  const waiting = teachingLines(teachState(listing([]), OVEN) as never, t, info, (k) => k);
  assert.deepEqual(waiting, [t("teach_waiting")]);
  const owned = status({ conflict: { id: "medium-2400w", appliance: HEATER.key } });
  assert.ok(
    teachingLines(teachState(listing([owned]), OVEN) as never, t, info, () => HEATER.name).includes(
      "This signature already belongs to Water heater.",
    ),
  );
  const fr = teachingLines(teachState(listing([status({ days: 1, uses: 0, learned: null, evaluation: null })]), OVEN) as never, translator("fr"), localeInfo(undefined, "fr", "UTC"), (k) => k);
  assert.deepEqual(fr, ["1 jour · aucune utilisation vue pour l'instant"]);
});

test("learnedText of a multi-phase appliance", () => {
  const text = learnedText(
    {
      kind: "multiphase",
      band: "cycle",
      power_w: 2050,
      duration_s: 5400,
      uses: 4,
      runs: 4,
      recognisable: true,
      phases: [
        { kind: "motor", power_w: 90, duration_s: 480 },
        { kind: "heat", power_w: 2050, duration_s: 1200 },
      ],
    },
    t,
    info,
  );
  assert.match(text, /^Cycle with 2.1 kW heating, ~1 h 30 \(motor 90 W 8 min → heating 2.1 kW 20 min\)$/);
});

test("sensorChoices", () => {
  const states = {
    "sensor.oven_plug_power": { attributes: { device_class: "power", unit_of_measurement: "W", friendly_name: "Oven plug" } },
    "sensor.kettle_plug": { attributes: { unit_of_measurement: "kW", friendly_name: "Kettle plug" } },
    "sensor.dryer_plug": { attributes: { device_class: "power", friendly_name: "Dryer plug" } },
    "sensor.oven_energy": { attributes: { device_class: "energy", unit_of_measurement: "kWh" } },
    "switch.oven_plug": { attributes: { friendly_name: "Oven plug switch" } },
  };
  const heaterPlug: ApplianceInfo = { ...HEATER, teacher: { entity_id: "sensor.dryer_plug" } };
  const free = { ...OVEN, teacher: null };
  assert.deepEqual(sensorChoices(states, [free, heaterPlug], OVEN.key), [
    { id: "sensor.oven_plug_power", name: "Oven plug", suggested: true, flat: false },
    { id: "sensor.kettle_plug", name: "Kettle plug", suggested: false, flat: false },
  ]);
  // a power sensor stuck at 0 W (#79) comes last and cannot teach
  assert.deepEqual(sensorChoices(states, [free, heaterPlug], OVEN.key, new Set(["sensor.oven_plug_power"])), [
    { id: "sensor.kettle_plug", name: "Kettle plug", suggested: false, flat: false },
    { id: "sensor.oven_plug_power", name: "Oven plug", suggested: false, flat: true },
  ]);
  // a sensor of another appliance is not offered
  assert.deepEqual(
    sensorChoices(states, [free, heaterPlug], HEATER.key).map((x) => x.id),
    ["sensor.dryer_plug", "sensor.kettle_plug"],
  );
  assert.deepEqual(sensorChoices(undefined, [], OVEN.key), []);
});

test("teachingLines of a connected appliance (#79)", () => {
  const learned = {
    ...status().learned!,
    kind: "steady",
    power_w: 330,
    duration_s: 6000,
    source: "state",
    measured_kwh: 0.55,
    energy_kwh: 0.52,
    counter_ratio: 0.95,
    counter_kwh: 1.6,
    named_phases: [
      { state: "drying", power_w: 340, duration_s: 5400 },
      { state: "cooling", power_w: 110, duration_s: 480 },
    ],
    programs: [
      { program: "cotton", uses: 2, kind: "steady", power_w: 330, duration_s: 6000, energy_kwh: 0.5 },
      { program: "synthetic", uses: 1, kind: "steady", power_w: 300, duration_s: 4000, energy_kwh: 0.3 },
    ],
  };
  const dryer: ApplianceInfo = {
    ...HEATER,
    key: "load:sensor.dryer_state",
    teacher: { entity_id: "sensor.dryer_state", mode: "state", on_states: ["run"], program_entity: "select.dryer_program" },
  };
  const st = status({
    key: dryer.key,
    entity_id: "sensor.dryer_state",
    mode: "state",
    learned,
    program_entity: "select.dryer_program",
    program_recorded: false,
  });
  const lines = teachingLines(teachState(listing([st]), dryer) as never, t, info, (k) => k, () => "Dryer programme");
  assert.deepEqual(lines.slice(2, 6), [
    "Energy of a use: 0.52 kWh, by its counter (1.60 kWh counted, 95 % of the curve's measure)",
    "Phases: drying 340 W · cooling 110 W",
    "Programmes: cotton (2), synthetic (1)",
    "The programme (Dryer programme) is not kept by the recorder of Home Assistant: one signature is learned for all programmes. Record it to learn one per programme.",
  ]);
});
