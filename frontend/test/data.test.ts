import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  assignColors,
  dayEntries,
  dayPresets,
  type DayStats,
  hourProfile,
  neighbourDay,
  peakWindow,
  seriesValue,
  signatureNames,
  stackSeries,
  statsDays,
  summary,
  unexplainedShare,
} from "../src/data.ts";
import type { Peak, Registry, Report } from "../src/types.ts";

function report(): Report {
  return {
    day: "2027-03-09",
    start: 1000,
    end: 1000 + 86400,
    energy_kwh: {
      house: 10,
      grid_import: 8,
      grid_export: 1,
      pv: 3,
      submetered: 2,
      base: 4,
      other: 0.5,
      base_w: 170,
      known: { "Water heater": 2.4 },
      signatures: { "short-2000w": 0.6, "short-2050w": 0.2, "cyc-1500w": 0.3 },
    },
    max_house_w: 4200,
    peak_threshold_w: 3500,
    curve: [
      {
        t: 1000,
        house: 4000,
        grid: 4000,
        pv: 0,
        submetered: 500,
        base: 170,
        other: 30,
        known: { "Water heater": 2400 },
        signatures: { "short-2000w": 600, "short-2050w": 300 },
      },
    ],
    peaks: [],
    signatures: [
      { id: "short-2000w", kind: "steady", band: "short", power_w: 2000, count: 2, duration_s: 120, energy_kwh: 0.6, registry_id: "short-2000w" },
      // two daily clusters matched to the same registry signature
      { id: "short-2050w", kind: "steady", band: "short", power_w: 2050, count: 1, duration_s: 120, energy_kwh: 0.2, registry_id: "short-2000w" },
      { id: "cyc-1500w", kind: "cycling", band: "session", power_w: 1500, count: 1, duration_s: 900, energy_kwh: 0.3, registry_id: null },
    ],
    known_loads: [],
    config: { pv_entity: "sensor.solar_power" },
  };
}

const registry: Registry = {
  signatures: [
    {
      id: "short-2000w", kind: "steady", band: "short", label: "Kettle", power_w: 2000, duration_s: 120,
      on_s: null, period_s: null, duty: null, weight: 3, count: 3, days_seen: 3, per_week: 3,
      first_seen: "2027-03-01", last_seen: "2027-03-09", hours: [], energy_kwh: 1,
    },
  ],
  switched_loads: [],
  peak: null,
};

test("stack order: base, sub-meters, switched loads, signatures by energy, other", () => {
  const stack = stackSeries(report(), null);
  assert.deepEqual(
    stack.map((s) => s.id),
    ["base", "submetered", "known:Water heater", "sig:short-2000w", "sig:cyc-1500w", "other"],
  );
  const kettle = stack.find((s) => s.id === "sig:short-2000w")!;
  assert.deepEqual(kettle.keys, ["short-2000w", "short-2050w"]);
  assert.ok(Math.abs(kettle.energy_kwh - 0.8) < 1e-9);
});

test("series values sum the daily clusters of a registry signature", () => {
  const r = report();
  const stack = stackSeries(r, null);
  const values = Object.fromEntries(stack.map((s) => [s.id, seriesValue(r.curve[0], s)]));
  assert.deepEqual(values, {
    base: 170,
    submetered: 500,
    "known:Water heater": 2400,
    "sig:short-2000w": 900,
    "sig:cyc-1500w": 0,
    other: 30,
  });
});

test("no sub-meter series without sub-metered energy", () => {
  const r = report();
  r.energy_kwh.submetered = 0;
  r.curve[0].submetered = 0;
  assert.ok(!stackSeries(r, null).some((s) => s.id === "submetered"));
});

test("estimated devices are a series of their own, apart from the measured ones", () => {
  const r = report();
  r.energy_kwh.estimated = 0.5;
  r.curve[0].estimated = 200;
  const stack = stackSeries(r, null);
  const measured = stack.find((s) => s.id === "submetered");
  const estimated = stack.find((s) => s.id === "estimated");
  assert.ok(measured && estimated);
  assert.equal(measured.energy_kwh, r.energy_kwh.submetered - 0.5);
  assert.equal(estimated.energy_kwh, 0.5);
  assert.equal(seriesValue(r.curve[0], measured) + seriesValue(r.curve[0], estimated), r.curve[0].submetered);
  const s = summary(r);
  assert.equal(s.submetered + s.estimated, r.energy_kwh.submetered);
});

test("older reports without estimated energy: everything measured", () => {
  const stack = stackSeries(report(), null);
  assert.ok(!stack.some((s) => s.id === "estimated"));
  assert.equal(summary(report()).estimated, 0);
});

test("signature names: registry label, else registry id, else daily id", () => {
  const names = signatureNames(report(), registry);
  assert.equal(names.get("short-2050w"), "Kettle");
  assert.equal(names.get("cyc-1500w"), "cyc-1500w");
  assert.equal(signatureNames(report(), null).get("short-2050w"), "short-2000w");
  assert.equal(stackSeries(report(), registry).find((s) => s.id === "sig:short-2000w")!.name, "Kettle");
});

test("summary totals", () => {
  const s = summary(report());
  assert.equal(s.known, 2.4);
  assert.ok(Math.abs(s.signatures - 1.1) < 1e-9);
  assert.equal(s.base_w, 170);
  assert.equal(s.has_pv, true);
  const r = report();
  r.config = {};
  r.energy_kwh.pv = 0;
  r.energy_kwh.grid_export = 0;
  assert.equal(summary(r).has_pv, false);
});

test("colours are stable per id and distinct within a chart", () => {
  const candidates = [1, 2, 3, 4, 5];
  const a = assignColors(["sig:a", "sig:b", "known:c"], candidates);
  const b = assignColors(["known:c", "sig:b", "sig:a"], candidates);
  assert.deepEqual([...a.entries()].sort(), [...b.entries()].sort());
  assert.equal(new Set(a.values()).size, 3);
  for (const v of a.values()) assert.ok(candidates.includes(v));
  // more series than colours: every id still gets one
  const many = assignColors(["a", "b", "c", "d", "e", "f", "g"], [7, 8]);
  assert.equal(many.size, 7);
  assert.equal(assignColors(["a"], []).size, 0);
});

test("peak window stays inside the day", () => {
  const peak: Peak = { start: 1100, end: 1200, max_ts: 1150, house_w: 4000, grid_w: 4000, pv_w: 0, parts: [], unexplained_w: 1000 };
  assert.deepEqual(peakWindow(peak, 1000, 90000, 600), [1000 * 1000, 1800 * 1000]);
  assert.equal(unexplainedShare(peak), 0.25);
  assert.equal(unexplainedShare({ ...peak, unexplained_w: -50 }), 0);
  assert.equal(unexplainedShare({ ...peak, house_w: 0 }), 0);
});

test("days: newest first, summaries flagged, neighbours", () => {
  const entries = dayEntries({ days: ["2027-03-08", "2027-03-09"], summary_days: ["2027-01-02"] });
  assert.deepEqual(entries, [
    { day: "2027-03-09", summary: false },
    { day: "2027-03-08", summary: false },
    { day: "2027-01-02", summary: true },
  ]);
  assert.equal(neighbourDay(entries, "2027-03-08", -1), "2027-01-02");
  assert.equal(neighbourDay(entries, "2027-03-08", 1), "2027-03-09");
  assert.equal(neighbourDay(entries, "2027-03-09", 1), null);
  assert.equal(neighbourDay(entries, "2027-01-02", -1), null);
  assert.deepEqual(dayEntries(null), []);
});

test("hour profile is scaled to the busiest hour", () => {
  const p = hourProfile([0, 2, 4]);
  assert.equal(p.length, 24);
  assert.deepEqual(p.slice(0, 4), [0, 0.5, 1, 0]);
  assert.deepEqual(hourProfile(undefined), Array(24).fill(0));
});

test("the synthetic sample report is consistent with the stack", () => {
  const url = new URL("../dev/sample/report-2027-03-09.json", import.meta.url);
  const r = JSON.parse(readFileSync(url, "utf8")) as Report;
  const stack = stackSeries(r, null);
  for (const p of r.curve) {
    const total = stack.reduce((a, s) => a + seriesValue(p, s), 0);
    // the stack explains the house load (sub-meters can exceed it by noise)
    assert.ok(Math.abs(total - Math.max(p.house, p.submetered)) <= stack.length, `${p.t}: ${total} vs ${p.house}`);
  }
});

test("stats days: the available days of the last 30, before today", () => {
  const entries = dayEntries({ days: ["2027-03-09", "2027-03-10"], summary_days: ["2027-01-02", "2027-02-10"] });
  assert.deepEqual(statsDays(entries, "2027-03-10"), ["2027-03-09", "2027-02-10"]);
});

test("day picker presets", () => {
  const s = (house_kwh: number, notable_peaks = 0): DayStats => ({
    house_kwh,
    max_house_w: null,
    notable_peaks,
    last_notable: null,
  });
  const stats = {
    "2027-01-01": s(99, 3), // older than 30 days
    "2027-03-01": s(30, 1),
    "2027-03-05": s(25, 2),
    "2027-03-08": s(12),
    "2027-03-10": s(80, 1), // today: not over
  };
  assert.deepEqual(dayPresets("2027-03-10", stats), [
    { key: "yesterday", day: "2027-03-09" },
    { key: "day_before_yesterday", day: "2027-03-08" },
    { key: "highest_consumption", day: "2027-03-01" },
    { key: "last_notable_peak", day: "2027-03-05" },
  ]);
  assert.deepEqual(
    dayPresets("2027-03-10", {}).map((p) => p.key),
    ["yesterday", "day_before_yesterday"],
  );
});
