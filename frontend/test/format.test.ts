import assert from "node:assert/strict";
import { test } from "node:test";

import {
  formatDay,
  formatDuration,
  formatEnergy,
  formatNumber,
  formatPower,
  formatTime,
  localeInfo,
  previousDay,
} from "../src/format.ts";
import { translator } from "../src/i18n.ts";

const en = localeInfo({ language: "en", number_format: "language", time_format: "24", time_zone: "server" }, "en", "Europe/Berlin");
const fr = localeInfo({ language: "fr", number_format: "language", time_format: "language", time_zone: "server" }, "fr", "Europe/Berlin");
const nbsp = (s: string) => s.replace(/[  ]/g, " ");

test("time zone: server or browser", () => {
  assert.equal(en.time_zone, "Europe/Berlin");
  const local = localeInfo({ language: "en", time_zone: "local" }, "en", "Europe/Berlin", "America/New_York");
  assert.equal(local.time_zone, "America/New_York");
});

test("numbers follow the number format", () => {
  assert.equal(formatNumber(1234.5, en, 1), "1,234.5");
  assert.equal(nbsp(formatNumber(1234.5, fr, 1)), "1 234,5");
  assert.equal(formatNumber(1234.5, { ...en, number_format: "decimal_comma" }, 1), "1.234,5");
  assert.equal(formatNumber(1234.5, { ...en, number_format: "none" }, 1), "1234.5");
});

test("energy and power units", () => {
  assert.equal(formatEnergy(2.345, en), "2.35 kWh");
  assert.equal(formatEnergy(22.81, en), "22.8 kWh");
  assert.equal(formatPower(850, en), "850 W");
  assert.equal(formatPower(2398, en), "2.40 kW");
  assert.equal(nbsp(formatPower(12345, fr)), "12,3 kW");
});

test("times in the display time zone", () => {
  const ts = Date.UTC(2027, 2, 9, 13, 5) / 1000; // 14:05 in Berlin
  assert.equal(formatTime(ts, en), "14:05");
  assert.equal(formatTime(ts, fr), "14:05");
  assert.match(formatTime(ts, { ...en, time_format: "12" }), /^2:05\sPM$/);
});

test("days are not shifted by the time zone", () => {
  assert.equal(formatDay("2027-03-09", en, "short"), "Mar 9, 2027");
  assert.match(formatDay("2027-03-09", fr), /9 mars 2027/);
});

test("durations", () => {
  assert.equal(formatDuration(45, en), "45 s");
  assert.equal(formatDuration(150, en), "3 min");
  assert.equal(formatDuration(3600, en), "1 h");
  assert.equal(formatDuration(4020, en), "1 h 07");
});

test("previous day in a time zone", () => {
  const ts = Date.UTC(2027, 2, 9, 23, 30) / 1000; // already March 10 in Berlin
  assert.equal(previousDay(ts, "Europe/Berlin"), "2027-03-09");
  assert.equal(previousDay(ts, "UTC"), "2027-03-08");
});

test("translations: language, fallback, parameters", () => {
  assert.equal(translator("fr")("peaks"), "Pics");
  assert.equal(translator("fr-CA")("peaks"), "Pics");
  assert.equal(translator("de")("peaks"), "Peaks");
  assert.equal(translator("en")("of_house", { share: "12 %" }), "12 % of the house");
});
