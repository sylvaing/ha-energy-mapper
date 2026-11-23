import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  assignColors,
  categorySeries,
  maxPowerTime,
  splitParts,
  dayEntries,
  dayPresets,
  type DayStats,
  hourProfile,
  neighbourDay,
  peakPartName,
  peakWindow,
  registryName,
  seriesValue,
  signatureNames,
  stackSeries,
  statsDays,
  summary,
  unexplainedShare,
  seriesPeriods,
  timeSpan,
  usageDetail,
  usageRows,
  type UsageRow,
} from "../src/data.ts";
import { localeInfo } from "../src/format.ts";
import { translator } from "../src/i18n.ts";
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

test("signature names with a namer: provisional name of the unnamed ones", () => {
  const namer = (s: { power_w?: number | null; kind?: string | null; hours?: number[] | null }) =>
    `${s.kind}:${s.power_w}:${s.hours ? "h" : "-"}`;
  const r = report();
  r.signatures.push({ id: "?", kind: "steady", band: "", power_w: 0, count: 1, duration_s: 0, energy_kwh: 0, registry_id: null });
  const reg: Registry = {
    ...registry,
    signatures: [...registry.signatures, { ...registry.signatures[0], id: "cyc-1500w", kind: "cycling", label: null, power_w: 1500, hours: [1] }],
  };
  const names = signatureNames(r, reg, namer);
  assert.equal(names.get("short-2050w"), "Kettle");
  assert.equal(names.get("?"), "?");
  // no registry match: built from the daily signature (no hours)
  assert.equal(signatureNames(r, registry, namer).get("cyc-1500w"), "cycling:1500:-");
  const r2 = report();
  r2.signatures[2].registry_id = "cyc-1500w";
  assert.equal(signatureNames(r2, reg, namer).get("cyc-1500w"), "cycling:1500:h");
  const stack = stackSeries(r2, reg, namer);
  assert.equal(stack.find((s) => s.id === "sig:cyc-1500w")!.ref, "cyc-1500w");
  assert.equal(stack.find((s) => s.id === "sig:short-2000w")!.ref, undefined);
  assert.equal(registryName(reg.signatures[1], namer), "cycling:1500:h");
  assert.equal(registryName(reg.signatures[1]), "cyc-1500w");
  assert.equal(registryName(reg.signatures[0], namer), "Kettle");
});

test("peak part names", () => {
  const namer = (s: { power_w?: number | null }) => `p${s.power_w}`;
  const r = report();
  const part = (name: string, registry_id: string | null) => ({ kind: "signature", name, power_w: 1, registry_id });
  assert.equal(peakPartName(part("short-2050w", "short-2000w"), r, registry, namer), "Kettle");
  assert.equal(peakPartName(part("cyc-1500w", null), r, registry, namer), "p1500");
  assert.equal(peakPartName(part("cyc-1500w", null), r, registry), "cyc-1500w");
  assert.equal(peakPartName(part("?", null), r, registry, namer), "?");
  assert.equal(peakPartName(part("gone", "gone-1"), r, null, namer), "gone-1");
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

const near = (a: number, b: number) => assert.ok(Math.abs(a - b) < 1e-9, `${a} != ${b}`);

test("split bar: switched loads and labelled signatures are named, the others to name", () => {
  const parts = splitParts(report(), registry);
  assert.deepEqual(parts.map((p) => p.key), ["base", "measured", "named", "to_name", "other"]);
  const kwh = Object.fromEntries(parts.map((p) => [p.key, p.energy_kwh]));
  near(kwh.base, 4);
  near(kwh.measured, 2);
  near(kwh.named, 2.4 + 0.8); // water heater + kettle (two daily clusters)
  near(kwh.to_name, 0.3);
  near(kwh.other, 0.5);
  near(parts.reduce((a, p) => a + p.share, 0), 1);
  near(parts.find((p) => p.key === "base")!.share, 4 / 10);
});

test("split bar: without registry every signature is still to name", () => {
  const kwh = Object.fromEntries(splitParts(report(), null).map((p) => [p.key, p.energy_kwh]));
  near(kwh.named, 2.4);
  near(kwh.to_name, 1.1);
});

test("split bar: a signature merged into an appliance is named", () => {
  const reg: Registry = {
    ...registry,
    signatures: [{ ...registry.signatures[0], id: "cyc-1500w", label: null, appliance: "dishwasher" }],
  };
  const kwh = Object.fromEntries(splitParts(report(), reg).map((p) => [p.key, p.energy_kwh]));
  near(kwh.named, 2.4 + 0.3);
});

test("split bar: estimated apart, older reports all measured", () => {
  const r = report();
  r.energy_kwh.estimated = 0.5;
  const kwh = Object.fromEntries(splitParts(r, registry).map((p) => [p.key, p.energy_kwh]));
  near(kwh.measured, 1.5);
  near(kwh.estimated, 0.5);
  assert.ok(!splitParts(report(), registry).some((p) => p.key === "estimated"));
});

test("split bar: an empty day has no part", () => {
  const r = report();
  r.energy_kwh = { ...r.energy_kwh, base: 0, submetered: 0, other: 0, known: {}, signatures: {} };
  r.curve = [];
  r.signatures = [];
  assert.deepEqual(splitParts(r, null), []);
});

test("series of a category, to highlight them in the chart", () => {
  const stack = stackSeries(report(), registry);
  assert.deepEqual(categorySeries(stack, "named", registry), ["known:Water heater", "sig:short-2000w"]);
  assert.deepEqual(categorySeries(stack, "to_name", registry), ["sig:cyc-1500w"]);
  assert.deepEqual(categorySeries(stack, "measured", registry), ["submetered"]);
});

test("time of the highest power: curve maximum, else the highest peak", () => {
  const r = report();
  r.curve.push({ ...r.curve[0], t: 1060, house: 4200 });
  assert.equal(maxPowerTime(r), 1060);
  r.curve = [];
  r.peaks = [
    { start: 2000, end: 2100, max_ts: 2050, house_w: 3600, grid_w: 0, pv_w: 0, parts: [], unexplained_w: 0 },
    { start: 5000, end: 5200, max_ts: 5100, house_w: 4200, grid_w: 0, pv_w: 0, parts: [], unexplained_w: 0 },
  ];
  assert.equal(maxPowerTime(r), 5100);
  r.peaks = [];
  assert.equal(maxPowerTime(r), null);
});

// -- "What used energy" (#50) -------------------------------------------------

const en = localeInfo({ language: "en", number_format: "language", time_format: "24", time_zone: "server" }, "en", "UTC");
const fr = localeInfo({ language: "fr", number_format: "language", time_format: "24", time_zone: "server" }, "fr", "UTC");
const tEn = translator("en");
const tFr = translator("fr");

/** A day of 6 curve points one minute apart, from 08:00 UTC. */
function usageReport(): Report {
  const r = report();
  const t0 = Date.UTC(2027, 2, 9, 8, 0) / 1000;
  const heater = [2400, 2400, 0, 0, 2400, 0];
  const kettle = [0, 900, 900, 0, 0, 0];
  r.curve = heater.map((w, i) => ({
    t: t0 + i * 60,
    house: 3000,
    grid: 3000,
    pv: 0,
    submetered: i < 3 ? 500 : 0,
    base: 170,
    other: 30,
    known: { "Water heater": w },
    signatures: { "short-2000w": kettle[i] },
  }));
  r.known_loads = [
    { name: "Water heater", entity_id: "switch.heater", power_w: 2400, transitions: 4, matched: 4, inverted: false, on_s: 180 },
  ];
  r.activations = [
    { start: t0 + 60, end: t0 + 120, power_w: 2000, cycling: false, signature: "short-2000w", registry_id: "short-2000w" },
    { start: t0 + 150, end: t0 + 180, power_w: 2050, cycling: false, signature: "short-2050w", registry_id: "short-2000w" },
    { start: t0 + 3600, end: t0 + 7200, power_w: 1500, cycling: true, signature: "cyc-1500w", registry_id: null },
  ];
  return r;
}

test("usage rows: by energy, the unexplained rest last, with the chart series ids", () => {
  const rows = usageRows(usageReport(), registry);
  assert.deepEqual(
    rows.map((r) => r.id),
    ["base", "known:Water heater", "submetered", "sig:short-2000w", "sig:cyc-1500w", "other"],
  );
  assert.equal(rows[0].ratio, 1);
  assert.equal(rows[1].ratio, 2.4 / 4);
  const base = rows[0];
  assert.equal(base.power_w, 170);
  assert.equal(base.span, null);
});

test("usage rows: empty series are left out", () => {
  const r = usageReport();
  r.energy_kwh.other = 0;
  r.energy_kwh.known = { "Water heater": 0 };
  const ids = usageRows(r, registry).map((x) => x.id);
  assert.ok(!ids.includes("other"));
  assert.ok(!ids.includes("known:Water heater"));
});

test("usage rows: signatures count their activations and span them", () => {
  const r = usageReport();
  const rows = usageRows(r, registry);
  const t0 = r.curve[0].t;
  const kettle = rows.find((x) => x.id === "sig:short-2000w")!;
  // two daily clusters matched to the same registry signature
  assert.equal(kettle.runs, 2);
  assert.deepEqual(kettle.span, [t0 + 60, t0 + 180]);
  assert.equal(kettle.unnamed, false);
  assert.equal(kettle.registry_id, "short-2000w");
  const cyc = rows.find((x) => x.id === "sig:cyc-1500w")!;
  assert.equal(cyc.cycling, true);
  assert.equal(cyc.unnamed, true);
  // no registry match: nothing to review
  assert.equal(cyc.registry_id, null);
});

test("usage rows: without activations, the daily counts and the curve", () => {
  const r = usageReport();
  delete r.activations;
  const rows = usageRows(r, registry);
  const t0 = r.curve[0].t;
  const kettle = rows.find((x) => x.id === "sig:short-2000w")!;
  assert.equal(kettle.runs, 3);
  assert.deepEqual(kettle.span, [t0 + 60, t0 + 180]);
  assert.equal(rows.find((x) => x.id === "sig:cyc-1500w")!.cycling, true);
});

test("usage rows: a merged signature is named", () => {
  const r = usageReport();
  const merged: Registry = {
    ...registry,
    signatures: [...registry.signatures, { ...registry.signatures[0], id: "cyc-1500w", label: null, appliance: "heater" }],
  };
  r.signatures[2].registry_id = "cyc-1500w";
  assert.equal(usageRows(r, merged).find((x) => x.id === "sig:cyc-1500w")!.unnamed, false);
});

test("usage rows: switched loads count their on periods in the curve", () => {
  const r = usageReport();
  const heater = usageRows(r, registry).find((x) => x.id === "known:Water heater")!;
  assert.equal(heater.runs, 2);
  assert.equal(heater.on_s, 180);
  const t0 = r.curve[0].t;
  assert.deepEqual(heater.span, [t0, t0 + 300]);
});

test("series periods: a gap in the curve ends a period", () => {
  const r = usageReport();
  r.curve.splice(1, 1); // the minute 08:01 is missing
  r.curve.forEach((p) => (p.known["Water heater"] = 2400));
  const series = stackSeries(r, null).find((s) => s.id === "known:Water heater")!;
  const t0 = r.curve[0].t;
  assert.deepEqual(seriesPeriods(r.curve, series), [
    [t0, t0 + 60],
    [t0 + 120, t0 + 360],
  ]);
});

const row = (over: Partial<UsageRow>): UsageRow => ({
  id: "x",
  kind: "signature",
  name: "x",
  energy_kwh: 1,
  ratio: 1,
  unnamed: false,
  registry_id: null,
  runs: null,
  cycling: false,
  estimated: null,
  span: null,
  on_s: null,
  power_w: null,
  ...over,
});

test("usage detail texts", () => {
  const span: [number, number] = [Date.UTC(2027, 2, 9, 19, 2) / 1000, Date.UTC(2027, 2, 9, 19, 53) / 1000];
  assert.equal(usageDetail(row({ runs: 1, span }), tEn, en), "1 run · 19:02 – 19:53");
  assert.equal(usageDetail(row({ runs: 4, span }), tFr, fr), "4 fois · 19:02 – 19:53");
  assert.equal(
    usageDetail(row({ runs: 2, span, unnamed: true, cycling: true }), tEn, en),
    "to name · in cycles · 19:02 – 19:53",
  );
  assert.equal(usageDetail(row({ kind: "known", runs: 2, on_s: 8700, span }), tEn, en), "2 runs · on for 2 h 25");
  assert.equal(usageDetail(row({ kind: "known", runs: 1, span }), tFr, fr), "1 fois · 19:02 – 19:53");
  assert.equal(usageDetail(row({ kind: "submetered", span }), tFr, fr), "mesuré · 19:02 – 19:53");
  assert.equal(usageDetail(row({ kind: "estimated" }), tEn, en), "estimated");
  assert.equal(usageDetail(row({ kind: "base", power_w: 221 }), tFr, fr), "221 W en permanence");
  assert.equal(usageDetail(row({ kind: "base" }), tEn, en), "");
  assert.equal(usageDetail(row({ kind: "other" }), tEn, en), "");
  // a cold appliance (#89): the part of its energy that is an estimate is said
  assert.equal(
    usageDetail(row({ runs: 34, cycling: true, unnamed: true, estimated: 0.224 }), tEn, en),
    "to name · in cycles · 22 % estimated",
  );
  assert.equal(usageDetail(row({ runs: 34, cycling: true, estimated: 0.224 }), tFr, fr), "en cycles · 22 % estimé");
  assert.equal(usageDetail(row({ runs: 34, cycling: true, estimated: 0.001 }), tEn, en), "in cycles");
});

test("usage rows: a cold appliance runs in cycles, a part of its energy estimated", () => {
  const r = usageReport();
  for (const p of r.curve) p.signatures = { ...p.signatures, "cold-80w": 17 };
  r.energy_kwh.signatures = { ...r.energy_kwh.signatures, "cold-80w": 0.4 };
  r.signatures = [
    ...r.signatures,
    { id: "cold-80w", kind: "cold", band: "cold", power_w: 79, count: 34, duration_s: 540, energy_kwh: 0.4, estimated_kwh: 0.1, registry_id: "cold-80w" },
  ];
  const cold = usageRows(r, registry).find((x) => x.id === "sig:cold-80w")!;
  assert.equal(cold.cycling, true);
  assert.equal(cold.runs, 34);
  assert.equal(cold.estimated, 0.25);
  assert.equal(cold.unnamed, true);
  assert.equal(cold.registry_id, "cold-80w");
  // the other rows have no estimated part
  assert.deepEqual([...new Set(usageRows(r, registry).filter((x) => x.id !== "sig:cold-80w").map((x) => x.estimated))], [null]);
});

test("time span: one time for the same minute", () => {
  const t = Date.UTC(2027, 2, 9, 7, 10) / 1000;
  assert.equal(timeSpan([t, t + 20], en), "07:10");
  assert.equal(timeSpan([t, t + 600], en), "07:10 – 07:20");
});
