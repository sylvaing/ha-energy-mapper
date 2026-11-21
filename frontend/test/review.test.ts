import assert from "node:assert/strict";
import { test } from "node:test";

import { translator } from "../src/i18n.ts";
import {
  bestSuggestion,
  drivesLoad,
  entityChoices,
  exampleWindow,
  formatHours,
  hasReview,
  formForSuggestion,
  initialForm,
  linkable,
  reviewGroups,
  suggestionLabel,
  suggestionMode,
  usualHours,
  validateMessage,
} from "../src/review.ts";
import type { EntitySuggestion, ReviewListing, ReviewSignature } from "../src/types.ts";

function sig(id: string, state: ReviewSignature["review_state"]): ReviewSignature {
  return {
    id,
    kind: "steady",
    band: "short",
    label: null,
    power_w: 2000,
    duration_s: 150,
    on_s: null,
    period_s: null,
    duty: null,
    weight: 5,
    count: 8,
    days_seen: 4,
    per_week: 2,
    first_seen: "2027-03-01",
    last_seen: "2027-03-09",
    hours: Array(24).fill(0),
    energy_kwh: 0.7,
    review_state: state,
  };
}

function listing(...sigs: ReviewSignature[]): ReviewListing {
  return {
    criteria: { min_uses: 5, min_days: 3, wait_days: 14, wait_factor: 2 },
    now: 0,
    counts: {},
    signatures: sigs,
  };
}

test("review groups keep the add-on's order within a state", () => {
  const g = reviewGroups(
    listing(sig("a", "proposed"), sig("w", "waiting"), sig("b", "proposed"), sig("i", "ignored"), sig("v", "validated")),
  );
  assert.deepEqual(
    [g.proposed, g.waiting, g.ignored].map((x) => x.map((s) => s.id)),
    [["a", "b"], ["w"], ["i"]],
  );
  assert.equal(hasReview(g), true);
  assert.equal(hasReview(reviewGroups(null)), false);
  assert.equal(hasReview(reviewGroups(listing(sig("v", "validated")))), false);
});

test("usual hours: busiest ranges first, across midnight", () => {
  const h = Array(24).fill(0);
  h[7] = 4;
  h[8] = 3;
  h[19] = 5;
  h[12] = 1; // below half of the busiest hour
  assert.deepEqual(usualHours(h), [
    [7, 8],
    [19, 19],
  ]);
  assert.equal(formatHours(usualHours(h)), "7–9 h, 19 h");
  const night = Array(24).fill(0);
  night[23] = 2;
  night[0] = 2;
  night[1] = 1.5;
  assert.deepEqual(usualHours(night), [[23, 1]]);
  assert.equal(formatHours(usualHours(night)), "23–2 h");
  assert.deepEqual(usualHours(Array(24).fill(1)), [[0, 23]]);
  assert.deepEqual(usualHours(undefined), []);
  assert.deepEqual(usualHours(Array(24).fill(0)), []);
  // at most three ranges
  const many = Array(24).fill(0).map((_, i) => (i % 2 ? 1 : 0));
  assert.equal(usualHours(many).length, 3);
});

test("example window stays inside the day", () => {
  assert.deepEqual(exampleWindow({ day: "x", start: 1000, end: 1100, power_w: 1 }, 0, 86400), [0, 2300 * 1000]);
  assert.deepEqual(exampleWindow({ day: "x", start: 86000, end: 86000, power_w: 1 }, 0, 86400), [
    84800 * 1000,
    86400 * 1000,
  ]);
});

test("validate message: a name, and an entity of the right kind for a link", () => {
  const s = sig("cyc-1400w", "proposed");
  assert.deepEqual(validateMessage(s, "new", "  Hob ", ""), {
    msg: { type: "energy_mapper/validate", signature_id: "cyc-1400w", name: "Hob" },
  });
  assert.deepEqual(validateMessage(s, "new", " ", ""), { missing: "name" });
  assert.deepEqual(validateMessage(s, "switch", "Radiator", "switch.radiator"), {
    msg: { type: "energy_mapper/validate", signature_id: "cyc-1400w", name: "Radiator", entity_id: "switch.radiator" },
  });
  assert.deepEqual(validateMessage(s, "switch", "Radiator", "sensor.x"), { missing: "entity" });
  assert.deepEqual(validateMessage(s, "sensor", "Kettle", ""), { missing: "entity" });
  assert.equal("msg" in validateMessage(s, "sensor", "Kettle", "sensor.kettle_power"), true);
});

test("entity choices of the fallback picker", () => {
  const states = {
    "switch.b": { entity_id: "switch.b", attributes: { friendly_name: "B relay" } },
    "switch.a": { entity_id: "switch.a", attributes: { friendly_name: "A relay" } },
    "sensor.p": { entity_id: "sensor.p", attributes: { friendly_name: "Power", device_class: "power" } },
    "sensor.t": { entity_id: "sensor.t", attributes: { device_class: "temperature" } },
    "sensor.w": { entity_id: "sensor.w", attributes: { friendly_name: "Washer state", device_class: "enum" } },
    "automation.x": { entity_id: "automation.x" },
  };
  assert.deepEqual(
    entityChoices(states, "switch").map((e) => e.id),
    ["switch.a", "switch.b", "sensor.w"],
  );
  assert.deepEqual(entityChoices(states, "sensor"), [{ id: "sensor.p", name: "Power" }]);
  assert.deepEqual(entityChoices(states, "new"), []);
  assert.deepEqual(entityChoices(undefined, "switch"), []);
});

test("review strings exist in French", () => {
  const fr = translator("fr");
  assert.equal(fr("review"), "À examiner");
  assert.equal(fr("review_uses", { count: "8", days: "4" }), "8 utilisations sur 4 jours");
  for (const mode of ["new", "switch", "sensor"]) {
    assert.notEqual(fr(`review_mode_${mode}` as never), translator("en")(`review_mode_${mode}` as never));
  }
});

function suggestion(entity_id: string, score: number, on_states = ["on"]): EntitySuggestion {
  return {
    entity_id,
    score,
    on_share: score,
    off_share: score,
    explained: 0.9,
    matches: 11,
    steps: 12,
    days: 4,
    on_states,
    inverted: on_states[0] === "off",
  };
}

test("suggested entities pre-fill the validation form (#41)", () => {
  const plain = sig("a", "proposed");
  assert.equal(bestSuggestion(plain), undefined);
  assert.deepEqual(initialForm(plain), { id: "a", mode: "new", name: "", entity: "", appliance: "" });
  const s = { ...plain, suggestions: [suggestion("fan.attic", 0.92), suggestion("switch.pilot", 0.85, ["off"])] };
  assert.equal(bestSuggestion(s)?.entity_id, "fan.attic");
  assert.deepEqual(
    initialForm(s, (e) => (e === "fan.attic" ? "Attic fan" : undefined)),
    { id: "a", mode: "switch", name: "Attic fan", entity: "fan.attic", appliance: "" },
  );
  assert.deepEqual(initialForm(s), { id: "a", mode: "switch", name: "", entity: "fan.attic", appliance: "" });
});

test("validate message: the on-states of a suggested entity", () => {
  const washer = suggestion("sensor.washer_state", 0.9, ["rinsing", "running"]);
  const s = { ...sig("a", "proposed"), suggestions: [suggestion("fan.attic", 0.92), suggestion("switch.pilot", 0.85, ["off"]), washer] };
  // "on" is the default: not sent
  assert.deepEqual(validateMessage(s, "switch", "Fan", "fan.attic"), {
    msg: { type: "energy_mapper/validate", signature_id: "a", name: "Fan", entity_id: "fan.attic" },
  });
  assert.deepEqual(validateMessage(s, "switch", "Radiator", "switch.pilot"), {
    msg: { type: "energy_mapper/validate", signature_id: "a", name: "Radiator", entity_id: "switch.pilot", on_states: ["off"] },
  });
  // an operation-state sensor is linked like a switch, with its states
  assert.deepEqual(validateMessage(s, "switch", "Washer", "sensor.washer_state", { device_class: "enum" }), {
    msg: {
      type: "energy_mapper/validate",
      signature_id: "a",
      name: "Washer",
      entity_id: "sensor.washer_state",
      on_states: ["rinsing", "running"],
    },
  });
  // another entity: the add-on's automatic polarity
  assert.deepEqual(validateMessage(s, "switch", "Other", "light.other"), {
    msg: { type: "energy_mapper/validate", signature_id: "a", name: "Other", entity_id: "light.other" },
  });
});

test("which entities can be linked", () => {
  assert.equal(linkable("switch", "media_player.tv"), true);
  assert.equal(linkable("switch", "sensor.w", { device_class: "enum" }), true);
  assert.equal(linkable("switch", "sensor.p", { device_class: "power" }), false);
  assert.equal(linkable("sensor", "sensor.p", { device_class: "power" }), true);
  assert.equal(linkable("sensor", "sensor.w", { device_class: "enum" }), false);
  assert.equal(linkable("new", "switch.a"), false);
  assert.equal(linkable("switch", "automation.x"), false);
});

test("suggestion strings exist in French", () => {
  const fr = translator("fr");
  assert.equal(fr("review_probably", { name: "Ventilateur", score: "92 %" }), "Probablement : Ventilateur (92 %)");
  assert.notEqual(fr("review_unrecorded", { domains: "x" }), translator("en")("review_unrecorded", { domains: "x" }));
});

function contained(entity_id: string, extra: Partial<EntitySuggestion> = {}): EntitySuggestion {
  return {
    entity_id,
    attribute: "hvac_action",
    match: "containment",
    score: 0.97,
    matches: 80,
    sessions: 80,
    time_share: 0.3,
    used: 0.8,
    chance: 1e-30,
    days: 20,
    on_states: ["heating"],
    inverted: false,
    ...extra,
  };
}

test("attribute and containment suggestions name an appliance or merge it (#43)", () => {
  const friendly = (e: string) => (e === "climate.room" ? "Room" : undefined);
  const attr = contained("climate.room");
  assert.equal(drivesLoad(attr), false);
  assert.equal(drivesLoad(suggestion("fan.attic", 0.9)), true);
  assert.equal(drivesLoad({ ...suggestion("switch.pilot", 0.9), match: "containment" }), false);
  assert.equal(suggestionLabel(attr, friendly), "Room (heating)");
  assert.equal(suggestionLabel(suggestion("fan.attic", 0.9), friendly), "fan.attic");
  // not linked to an appliance: a new appliance named after it
  assert.equal(suggestionMode(attr), "new");
  assert.deepEqual(initialForm({ id: "a", suggestions: [attr] }, friendly), {
    id: "a",
    mode: "new",
    name: "Room",
    entity: "",
    appliance: "",
  });
  // part of an appliance: merge into it
  const part = contained("climate.room", { appliance: { key: "load:switch.radiator", name: "Radiator", via: "device" } });
  assert.equal(suggestionMode(part), "merge");
  assert.deepEqual(formForSuggestion("a", part, friendly), {
    id: "a",
    mode: "merge",
    name: "Room",
    entity: "",
    appliance: "load:switch.radiator",
  });
  // an attribute is never sent as the on-states of a switch
  const s = { id: "a", suggestions: [contained("switch.pilot", { attribute: undefined, on_states: ["off"] })] };
  assert.deepEqual(validateMessage(s, "switch", "Pilot", "switch.pilot"), {
    msg: { type: "energy_mapper/validate", signature_id: "a", name: "Pilot", entity_id: "switch.pilot" },
  });
});

test("merge message (#43)", () => {
  const s = { id: "a", suggestions: [] };
  assert.deepEqual(validateMessage(s, "merge", "", "", undefined, "load:switch.radiator"), {
    msg: { type: "energy_mapper/merge_signature", signature_id: "a", appliance: "load:switch.radiator" },
  });
  assert.deepEqual(validateMessage(s, "merge", "x", "", undefined, ""), { missing: "appliance" });
  const fr = translator("fr");
  assert.equal(fr("review_merge_into", { name: "Radiateur" }), "Fusionner dans Radiateur");
  assert.notEqual(fr("review_mode_merge"), translator("en")("review_mode_merge"));
});
