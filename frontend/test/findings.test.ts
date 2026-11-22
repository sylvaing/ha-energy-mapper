import assert from "node:assert/strict";
import { test } from "node:test";

import { findingSentence, findingView } from "../src/findings.ts";
import { localeInfo } from "../src/format.ts";
import { translator } from "../src/i18n.ts";
import type { Finding } from "../src/types.ts";

const en = localeInfo({ language: "en", number_format: "language", time_format: "24", time_zone: "server" }, "en", "UTC");
const fr = localeInfo({ language: "fr", number_format: "language", time_format: "24", time_zone: "server" }, "fr", "UTC");
const tEn = translator("en");
const tFr = translator("fr");
const NBSP = /[  ]/g; // French number grouping and unit spaces

const names: Record<string, string> = {
  "sensor.grid_energy": "Grid meter",
  "sensor.roof_production": "Roof panels",
  "sensor.battery_power": "Home battery",
  "sensor.dryer_energy": "Dryer plug energy",
};
const friendly = (id: string) => names[id];

function finding(kind: string, entity_id: string, params?: Record<string, unknown>, detail = "english detail"): Finding {
  return { kind, entity_id, detail, params };
}

/** One finding of each kind the add-on produces, with its parameters. */
const ALL: Finding[] = [
  finding("grid_ignored", "sensor.grid_energy", {}),
  finding("no_power_sensor", "sensor.grid_energy", { source: "grid" }),
  finding("no_power_sensor", "sensor.roof_production", { source: "solar" }),
  finding("no_power_sensor", "sensor.dryer_energy", { source: "device", name: "Dryer" }),
  finding("battery_sign", "sensor.battery_power", {}),
  finding("unavailable", "sensor.dryer_energy", { name: "Dryer" }),
  finding("unknown_statistic", "energy_mapper:load_switch_old_energy", { name: "Old relay" }),
  finding("no_grid", "", {}),
  finding("tracking_gap", "sensor.dryer_energy", { name: "Dryer", declared_kwh: 0.8, measured_kwh: 6.1 }),
  finding("flat_power_sensor", "sensor.dryer_energy", {
    name: "Dryer",
    power_entity: "sensor.dryer_power",
    power_w: 0,
    energy_kwh: 0.6,
  }),
];

test("findings: every kind gets a title and a sentence in English and French", () => {
  for (const t of [tEn, tFr]) {
    const info = t === tEn ? en : fr;
    for (const f of ALL) {
      const s = findingSentence(f, t, info, friendly);
      assert.ok(s, `${f.kind} has a sentence`);
      assert.ok(!s.includes("{"), `${f.kind}: no placeholder left in "${s}"`);
      assert.ok(!s.includes("english detail"));
      const { title } = findingView(f, t, info, friendly);
      assert.notEqual(title, t("finding_other"), `${f.kind} has its own title`);
    }
  }
});

test("findings: French sentences", () => {
  const s = (f: Finding) => findingSentence(f, tFr, fr, friendly)?.replace(NBSP, " ");
  assert.equal(s(ALL[0]), "Grid meter : compteur réseau qui n'a jamais compté, ignoré.");
  assert.equal(s(ALL[2]), "Roof panels : production solaire sans capteur de puissance, ignorée.");
  assert.equal(s(ALL[3]), "Dryer : pas de capteur de puissance, non soustrait.");
  assert.equal(s(ALL[4]), "Home battery : vérifiez le signe, positif doit signifier décharge.");
  assert.equal(s(ALL[5]), "Dryer : capteur indisponible.");
  assert.equal(s(ALL[8]), "Dryer : 0,80 kWh déclarés, 6,10 kWh mesurés.");
});

test("findings: energies follow the number format of the profile", () => {
  const gap = finding("tracking_gap", "sensor.dryer_energy", { name: "Dryer", declared_kwh: 12.34, measured_kwh: 0 });
  assert.equal(findingSentence(gap, tEn, en, friendly), "Dryer: declared 12.3 kWh, measured 0.00 kWh.");
  const comma = { ...en, number_format: "decimal_comma" };
  assert.equal(findingSentence(gap, tEn, comma, friendly), "Dryer: declared 12,3 kWh, measured 0,00 kWh.");
});

test("findings: the name falls back to the friendly name, then the entity id", () => {
  assert.equal(
    findingSentence(finding("unavailable", "sensor.dryer_energy", {}), tEn, en, friendly),
    "Dryer plug energy: sensor unavailable.",
  );
  assert.equal(
    findingSentence(finding("unavailable", "sensor.unknown_energy", {}), tEn, en, friendly),
    "sensor.unknown_energy: sensor unavailable.",
  );
});

test("findings: the entity id is added when the sentence does not show it", () => {
  assert.deepEqual(findingView(ALL[5], tEn, en, friendly), {
    title: "Sensor unavailable",
    text: "Dryer: sensor unavailable. (sensor.dryer_energy)",
  });
  const bare = finding("unavailable", "sensor.unknown_energy", {});
  assert.equal(findingView(bare, tEn, en, friendly).text, "sensor.unknown_energy: sensor unavailable.");
  assert.equal(findingView(ALL[7], tFr, fr, friendly).text, tFr("finding_text_no_grid"));
});

test("findings: unknown kind or missing parameters fall back to the add-on's detail", () => {
  // a kind from a newer add-on
  const future = finding("brand_new", "sensor.x", { name: "X" }, "X: something new");
  assert.equal(findingSentence(future, tFr, fr, friendly), null);
  assert.deepEqual(findingView(future, tFr, fr, friendly), { title: "Remarque", text: "X: something new (sensor.x)" });
  // an older add-on: no parameters
  const old = finding("tracking_gap", "sensor.dryer_energy", undefined, "Dryer: declared 0.80 kWh, measured 6.10 kWh");
  assert.equal(findingView(old, tFr, fr, friendly).text, "Dryer: declared 0.80 kWh, measured 6.10 kWh (sensor.dryer_energy)");
  const noSource = finding("no_power_sensor", "sensor.grid_energy", undefined, "grid meter without power sensor");
  assert.equal(findingSentence(noSource, tFr, fr, friendly), null);
  // what needs no parameter is translated even from an older add-on
  assert.equal(findingSentence(finding("no_grid", "", undefined), tFr, fr, friendly), tFr("finding_text_no_grid"));
  // a wrong parameter type
  const bad = finding("tracking_gap", "sensor.dryer_energy", { name: "Dryer", declared_kwh: "0.8", measured_kwh: 6 });
  assert.equal(findingSentence(bad, tEn, en, friendly), null);
});
