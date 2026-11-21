import assert from "node:assert/strict";
import { test } from "node:test";

import { translator } from "../src/i18n.ts";
import {
  entityChoices,
  exampleWindow,
  formatHours,
  hasReview,
  reviewGroups,
  usualHours,
  validateMessage,
} from "../src/review.ts";
import type { ReviewListing, ReviewSignature } from "../src/types.ts";

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
    "automation.x": { entity_id: "automation.x" },
  };
  assert.deepEqual(
    entityChoices(states, "switch").map((e) => e.id),
    ["switch.a", "switch.b"],
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
