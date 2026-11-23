import assert from "node:assert/strict";
import { test } from "node:test";

import { localeInfo } from "../src/format.ts";
import { translator } from "../src/i18n.ts";
import { selfTestHint, selfTestRows, showSelfTest, toneOf, type SelfTestDevice, type SelfTestListing } from "../src/selftest.ts";

// synthetic household (no real data)
const info = localeInfo(undefined, "en", "UTC");
const t = translator("en");
// values never wrap inside: compared with plain spaces
const plain = (s: string) => s.replace(/\u00a0/g, " ");

function device(extra: Partial<SelfTestDevice> = {}): SelfTestDevice {
  return {
    entity_id: "sensor.heater_power",
    name: "sensor.heater_power",
    state: "ok",
    tests: 6,
    uses: 12,
    found: 10,
    precision: 0.9,
    energy_ratio: 0.97,
    rate: 0.833,
    trend: "up",
    last_day: "2027-03-09",
    learned: { power_w: 2500 },
    ...extra,
  };
}

function listing(devices: SelfTestDevice[], extra: Partial<SelfTestListing> = {}): SelfTestListing {
  return { supported: true, enabled: true, window_days: 60, min_uses: 5, devices, ...extra };
}

test("the card needs a supported add-on with a tested device", () => {
  assert.equal(showSelfTest(null), false);
  assert.equal(showSelfTest({ supported: false, enabled: false, devices: [] }), false);
  assert.equal(showSelfTest(listing([])), false);
  assert.equal(showSelfTest(listing([device()])), true);
  assert.deepEqual(selfTestRows(listing([]), t, info), []);
});

test("a success rate with its tests, precision, energy and trend", () => {
  const [row] = selfTestRows(listing([device()]), t, info, (e) => (e === "sensor.heater_power" ? "Water heater" : null));
  assert.equal(row.name, "Water heater");
  assert.equal(plain(row.headline), "Found again 83 % of the time");
  assert.ok(row.headline.includes("83\u00a0%"));
  assert.equal(plain(row.detail), "6 tests · 10 of 12 uses · precision 90 % · energy 97 %");
  assert.equal(row.rate, 0.833);
  assert.equal(row.tone, "good");
  assert.equal(row.trend, "up");
  assert.equal(row.trendText, "Improving");
});

test("not enough data is never a 0 %", () => {
  const [row] = selfTestRows(listing([device({ state: "not_enough_data", tests: 1, uses: 2, found: 0, rate: null, trend: null })]), t, info);
  assert.equal(row.headline, "Not enough data yet (2 of 5 uses)");
  assert.equal(row.detail, "1 test · 0 of 2 uses");
  assert.equal(row.rate, null);
  assert.equal(row.tone, "none");
  assert.equal(row.trendText, "");
  assert.ok(!row.headline.includes("0 %"));
});

test("too small for the curve, and no use yet", () => {
  const rows = selfTestRows(
    listing([
      device({ entity_id: "sensor.fridge_power", name: "Fridge", state: "not_recognisable", tests: 0, rate: null, learned: { power_w: 100 } }),
      device({ entity_id: "sensor.oven_power", name: "Oven", state: "no_use", tests: 0, rate: null, learned: null }),
      // the curve's measured noise lets it show (#88): not "too low", only not tracked
      device({ entity_id: "sensor.freezer_power", name: "Freezer", state: "not_recognisable", tests: 0, rate: null, learned: { power_w: 70, visible: true } }),
    ]),
    t,
    info,
  );
  assert.deepEqual(
    rows.map((r) => [r.name, plain(r.headline), r.detail, r.rate]),
    [
      ["Freezer", "Seen on the whole-house curve, too small to be tracked yet (70 W)", "", null],
      ["Fridge", "Too low for the whole-house curve (100 W)", "", null],
      ["Oven", "No use seen yet", "", null],
    ],
  );
});

test("tones", () => {
  assert.equal(toneOf(0.8), "good");
  assert.equal(toneOf(0.5), "fair");
  assert.equal(toneOf(0.49), "poor");
  assert.equal(toneOf(null), "none");
});

test("hint and French", () => {
  assert.equal(
    selfTestHint(listing([device()], { enabled: false }), t, info),
    "Each night, a measured device is hidden from a copy of the analysis to check that the whole-house curve finds it again. Last 60 days. The self-test is switched off in the settings.",
  );
  const fr = translator("fr");
  const frInfo = localeInfo(undefined, "fr", "UTC");
  const [row] = selfTestRows(listing([device({ trend: "down" })]), fr, frInfo);
  assert.equal(plain(row.headline), "Retrouvé 83 % du temps");
  assert.equal(row.trendText, "En baisse");
  assert.equal(plain(row.detail), "6 tests · 10 utilisations sur 12 · précision 90 % · énergie 97 %");
  const [one] = selfTestRows(listing([device({ state: "not_enough_data", tests: 2, uses: 2, found: 1, rate: null })]), fr, frInfo);
  assert.equal(one.detail, "2 tests · 1 utilisation sur 2");
});
