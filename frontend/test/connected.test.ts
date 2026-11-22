import assert from "node:assert/strict";
import { test } from "node:test";

import {
  canStart,
  connectedLines,
  deviceChoices,
  entitiesOf,
  flatPowerSensors,
  formFor,
  formNotes,
  teachMessage,
  toggleState,
  withState,
  type ConnectedDevice,
  type ConnectedListing,
} from "../src/connected.ts";
import { localeInfo } from "../src/format.ts";
import { translator } from "../src/i18n.ts";

// synthetic devices (no real data)
const WASHER: ConnectedDevice = {
  device_id: "d1",
  name: "Washer dryer",
  appliance: null,
  entities: [
    { entity_id: "sensor.wd_operation", name: "Operation", role: "state", options: ["run", "stop", "pause"], recorded: true, appliance: null },
    { entity_id: "sensor.wd_job", name: "Job", role: "state", options: ["none", "drying", "cooling"], recorded: true, appliance: null },
    { entity_id: "select.wd_program", name: "Programme", role: "program", options: ["eco", "quick"], recorded: false, appliance: null },
    { entity_id: "sensor.wd_energy", name: "Energy", role: "energy", options: [], unit: "kWh", recorded: true, appliance: null },
    { entity_id: "sensor.wd_power", name: "Power", role: "power", options: [], unit: "W", recorded: true, appliance: null },
  ],
  proposal: {
    entity_id: "sensor.wd_operation",
    on_states: ["run"],
    phase_entity: "sensor.wd_job",
    energy_entity: "sensor.wd_energy",
    program_entity: "select.wd_program",
    power_entity: "sensor.wd_power",
  },
};
const OVEN: ConnectedDevice = {
  ...WASHER,
  device_id: "d2",
  name: "Oven",
  appliance: { key: "load:sensor.oven_state", name: "Oven" },
};
const listing: ConnectedListing = { supported: true, devices: [WASHER, OVEN] };
const t = translator("en");
const info = localeInfo(undefined, "en", "UTC");

test("deviceChoices: the appliance's own device first", () => {
  assert.deepEqual(deviceChoices(listing, null).map((d) => d.name), ["Oven", "Washer dryer"]);
  assert.deepEqual(deviceChoices(listing, "load:sensor.oven_state").map((d) => d.name), ["Oven", "Washer dryer"]);
  assert.deepEqual(deviceChoices({ supported: true, devices: [OVEN, WASHER] }, "x").map((d) => d.name), ["Oven", "Washer dryer"]);
  assert.deepEqual(deviceChoices(null, null), []);
});

test("formFor and teachMessage: a new appliance tracked by its state", () => {
  const form = formFor(WASHER, null);
  assert.equal(form.name, "Washer dryer");
  assert.equal(form.track, true);
  assert.ok(canStart(form));
  assert.deepEqual(teachMessage(form), {
    type: "energy_mapper/teach",
    entity_id: "sensor.wd_operation",
    mode: "state",
    on_states: ["run"],
    phase_entity: "sensor.wd_job",
    energy_entity: "sensor.wd_energy",
    program_entity: "select.wd_program",
    name: "Washer dryer",
    track: true,
  });
  // an existing appliance: no name, no track
  const onCard = formFor(OVEN, "load:sensor.oven_state", "Oven");
  assert.equal(onCard.track, false);
  const msg = teachMessage({ ...onCard, phase: "", energy: "" });
  assert.equal(msg.appliance, "load:sensor.oven_state");
  assert.equal(msg.phase_entity, null);
  assert.ok(!("name" in msg) && !("track" in msg));
  // a device used by an appliance is not tracked a second time
  assert.equal(formFor(OVEN, null).track, false);
});

test("states: switching the operation state, toggling the running states", () => {
  let form = formFor(WASHER, null);
  form = withState(form, WASHER, "sensor.wd_job");
  assert.equal(form.entity, "sensor.wd_job");
  assert.deepEqual(form.on_states, []);
  assert.equal(form.phase, ""); // the phase cannot be the operation state
  assert.ok(!canStart(form));
  form = toggleState(form, "drying");
  assert.deepEqual(form.on_states, ["drying"]);
  assert.ok(canStart(form));
  form = toggleState(form, "drying");
  assert.deepEqual(form.on_states, []);
  assert.ok(!canStart({ ...formFor(WASHER, null), name: " " }));
  assert.deepEqual(entitiesOf(WASHER, "energy").map((e) => e.entity_id), ["sensor.wd_energy"]);
});

test("formNotes: what the recorder does not keep, a flat power sensor", () => {
  const form = formFor(WASHER, null);
  const flat = flatPowerSensors([
    { kind: "flat_power_sensor", entity_id: "sensor.wd_energy", detail: "", params: { power_entity: "sensor.wd_power" } },
    { kind: "tracking_gap", entity_id: "x", detail: "" },
  ]);
  assert.deepEqual([...flat], ["sensor.wd_power"]);
  assert.deepEqual(formNotes(form, WASHER, flat, t), [
    t("connected_program_unrecorded", { name: "Programme" }),
    t("connected_flat_power", { name: "Power" }),
  ]);
  assert.deepEqual(formNotes({ ...form, program: "" }, WASHER, new Set(), t), []);
});

test("connectedLines", () => {
  assert.deepEqual(connectedLines(null, null, null, t, info), []);
  assert.deepEqual(connectedLines({ source: "state", measured_kwh: 1.5, energy_kwh: 1.5 }, null, null, t, info), [
    "Energy of a use: 1.50 kWh (measured on the curve)",
  ]);
  assert.deepEqual(
    connectedLines({ source: "state", measured_kwh: 0.5, energy_kwh: 0.5, counter_kwh: 0.4 }, null, null, t, info),
    ["Energy of a use: 0.50 kWh on the curve; its counter counted 0.40 kWh so far, too little to check it"],
  );
  const fr = connectedLines({ source: "state", measured_kwh: 1.5, energy_kwh: 1.5 }, false, "Programme", translator("fr"), localeInfo(undefined, "fr", "UTC"));
  assert.equal(fr.length, 2);
  assert.match(fr[1], /^Le programme \(Programme\)/);
});
