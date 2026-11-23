import assert from "node:assert/strict";
import { test } from "node:test";

import { localeInfo } from "../src/format.ts";
import { translator } from "../src/i18n.ts";
import {
  distinctNames,
  phasesSummary,
  dominantTimeOfDay,
  provisionalName,
  roundedDuration,
  roundedPower,
} from "../src/names.ts";

const en = localeInfo({ language: "en", number_format: "language", time_format: "24", time_zone: "server" }, "en", "UTC");
const fr = localeInfo({ language: "fr", number_format: "language", time_format: "24", time_zone: "server" }, "fr", "UTC");
const tEn = translator("en");
const tFr = translator("fr");

/** A 24-hour histogram with ``weight`` at each listed hour. */
function hours(entries: Record<number, number>): number[] {
  return Array.from({ length: 24 }, (_, i) => entries[i] ?? 0);
}

test("dominant time of day: one part of the day", () => {
  assert.deepEqual(dominantTimeOfDay(hours({ 19: 5, 20: 3 })), ["evening"]);
  assert.deepEqual(dominantTimeOfDay(hours({ 7: 4, 8: 2, 20: 1 })), ["morning"]);
  assert.deepEqual(dominantTimeOfDay(hours({ 12: 2 })), ["midday"]);
  assert.deepEqual(dominantTimeOfDay(hours({ 15: 2, 16: 2 })), ["afternoon"]);
  // the night crosses midnight
  assert.deepEqual(dominantTimeOfDay(hours({ 23: 2, 1: 3, 3: 1 })), ["night"]);
});

test("dominant time of day: two parts, in the order of the day", () => {
  assert.deepEqual(dominantTimeOfDay(hours({ 19: 5, 12: 4 })), ["midday", "evening"]);
  assert.deepEqual(dominantTimeOfDay(hours({ 7: 3, 19: 3, 15: 1 })), ["morning", "evening"]);
});

test("dominant time of day: spread uses, nothing, bad values", () => {
  assert.equal(dominantTimeOfDay(Array(24).fill(1)), "all_day");
  assert.equal(dominantTimeOfDay(hours({ 3: 1, 8: 1, 12: 1, 15: 1, 19: 1 })), "all_day");
  assert.equal(dominantTimeOfDay(undefined), null);
  assert.equal(dominantTimeOfDay([]), null);
  assert.equal(dominantTimeOfDay(Array(24).fill(0)), null);
  assert.deepEqual(dominantTimeOfDay([...hours({ 19: 2 }).slice(0, 23), Number.NaN]), ["evening"]);
  // a short histogram: the missing hours count as zero
  assert.deepEqual(dominantTimeOfDay([0, 0, 0, 0, 0, 0, 0, 5]), ["morning"]);
});

test("rounded power", () => {
  assert.equal(roundedPower(45.4, en), "45 W");
  assert.equal(roundedPower(937, en), "940 W");
  assert.equal(roundedPower(996, en), "1 kW");
  assert.equal(roundedPower(2600, en), "2.6 kW");
  assert.equal(roundedPower(2649, fr), "2,6 kW");
  assert.equal(roundedPower(1000, fr), "1 kW");
  assert.equal(roundedPower(12340, en), "12 kW");
});

test("rounded duration", () => {
  assert.equal(roundedDuration(12), 10);
  assert.equal(roundedDuration(1), 5);
  assert.equal(roundedDuration(200), 180);
  assert.equal(roundedDuration(2950), 3000);
  assert.equal(roundedDuration(5000), 5400);
  assert.equal(roundedDuration(7300), 7200);
});

test("provisional name, English and French", () => {
  const sig = { kind: "steady", power_w: 2600, duration_s: 3020, hours: hours({ 19: 4, 20: 2 }) };
  assert.equal(provisionalName(sig, tEn, en), "2.6 kW appliance, ~50 min, in the evening");
  assert.equal(provisionalName(sig, tFr, fr), "Appareil de 2,6 kW, ~50 min, le soir");
  const long = { kind: "steady", power_w: 350, duration_s: 7100, hours: hours({ 15: 3, 16: 2 }) };
  assert.equal(provisionalName(long, tFr, fr), "Appareil de 350 W, ~2 h, l’après-midi");
  const spread = { kind: "steady", power_w: 1900, duration_s: 230, hours: Array(24).fill(1) };
  assert.equal(provisionalName(spread, tEn, en), "1.9 kW appliance, ~4 min, all day");
  const two = { kind: "steady", power_w: 800, duration_s: 1500, hours: hours({ 12: 3, 19: 3 }) };
  assert.equal(provisionalName(two, tEn, en), "800 W appliance, ~25 min, at midday and in the evening");
  assert.equal(provisionalName(two, tFr, fr), "Appareil de 800 W, ~25 min, à midi et le soir");
});

test("provisional name of a cycling signature: no duration", () => {
  const sig = { kind: "cycling", power_w: 900, duration_s: 3600, hours: hours({ 20: 1 }) };
  assert.equal(provisionalName(sig, tEn, en), "900 W appliance in cycles, in the evening");
  assert.equal(provisionalName(sig, tFr, fr), "Appareil de 900 W en cycles, le soir");
});

test("provisional name of a cold appliance: its rhythm, not its hours", () => {
  const sig = { kind: "cold", power_w: 79, duration_s: 540, period_s: 2580, hours: Array(24).fill(1) };
  assert.equal(provisionalName(sig, tEn, en), "Runs in cycles all day, about 79 W, every 45 min");
  assert.equal(provisionalName(sig, tFr, fr), "Tourne en cycles toute la journée, environ 79 W, toutes les 45 min");
  assert.equal(provisionalName({ kind: "cold", power_w: 106 }, tEn, en), "Runs in cycles all day, about 110 W");
  assert.equal(provisionalName({ kind: "cold" }, tFr, fr), "Tourne en cycles toute la journée");
});

test("provisional name with missing fields: fewer parts", () => {
  // a daily signature has no hours
  assert.equal(provisionalName({ kind: "steady", power_w: 2000, duration_s: 120 }, tEn, en), "2 kW appliance, ~2 min");
  assert.equal(provisionalName({ power_w: 2000 }, tFr, fr), "Appareil de 2 kW");
  assert.equal(provisionalName({ duration_s: 0, power_w: null, hours: [] }, tEn, en), "Appliance");
  assert.equal(provisionalName({ kind: "cycling" }, tFr, fr), "Appareil en cycles");
});

test("distinct names: alike names get a number", () => {
  const out = distinctNames(
    new Map([
      ["a", "2 kW appliance"],
      ["b", "Kettle"],
      ["c", "2 kW appliance"],
      ["d", "2 kW appliance"],
    ]),
  );
  assert.deepEqual([...out.values()], ["2 kW appliance", "Kettle", "2 kW appliance (2)", "2 kW appliance (3)"]);
});

test("multi-phase signatures (#26): provisional name and phases summary", () => {
  const sig = { kind: "multiphase", power_w: 2050, duration_s: 5400, hours: hours({ 20: 3 }) };
  assert.equal(provisionalName(sig, tEn, en), "Cycle with 2.1 kW heating, ~1 h 30, in the evening");
  assert.equal(provisionalName(sig, tFr, fr), "Cycle avec chauffe de 2,1 kW, ~1 h 30, le soir");
  assert.equal(provisionalName({ kind: "multiphase" }, tEn, en), "Multi-phase cycle");
  const phases = [
    { kind: "motor", power_w: 92, duration_s: 480 },
    { kind: "heat", power_w: 2100, duration_s: 1200 },
    { kind: "motor", power_w: 110, duration_s: 1800 },
  ];
  assert.equal(phasesSummary(phases, tEn, en), "motor 92 W 8 min → heating 2.1 kW 20 min → motor 110 W 30 min");
  assert.equal(phasesSummary(phases, tFr, fr), "moteur 92 W 8 min → chauffe 2,1 kW 20 min → moteur 110 W 30 min");
  assert.equal(phasesSummary(phases, tEn, en, 2), "motor 92 W 8 min → heating 2.1 kW 20 min → …");
  assert.equal(phasesSummary(null, tEn, en), "");
});
