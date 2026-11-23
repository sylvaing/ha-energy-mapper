import assert from "node:assert/strict";
import { test } from "node:test";

import { exampleLines, type ExampleStatusItem, type ExamplesStatus } from "../src/examples.ts";
import { localeInfo } from "../src/format.ts";
import { translator } from "../src/i18n.ts";

// synthetic household (no real data)
const info = localeInfo(undefined, "en", "UTC");
const en = translator("en");
const fr = translator("fr");
// values never wrap inside: compared with plain spaces
const plain = (lines: string[]) => lines.map((s) => s.replace(/ /g, " "));

function item(power_w: number, reason: string | null, extra: Partial<ExampleStatusItem> = {}): ExampleStatusItem {
  return { t_on: 1_800_000_000, t_off: 1_800_000_120, power_w, status: "pending", reason, ...extra };
}

function status(...examples: ExampleStatusItem[]): ExamplesStatus {
  const count = (s: string) => examples.filter((e) => e.status === s).length;
  return { key: "appliance:fan", total: examples.length, found: count("found"), other: count("other"), pending: count("pending"), examples };
}

test("a labelled example under the threshold that the curve shows", () => {
  const seen = status(item(70, "small_untracked"));
  assert.deepEqual(plain(exampleLines(seen, en, info)), [
    "Labelled example at 70 W: the whole-house curve shows it, but appliances this small are not tracked yet.",
  ]);
  assert.deepEqual(plain(exampleLines(seen, fr, localeInfo(undefined, "fr", "UTC"))), [
    "Exemple étiqueté à 70 W : la courbe de la maison le montre, mais les appareils aussi petits ne sont pas encore suivis.",
  ]);
  // matched to a small use of the day: the same words
  const matched = status(item(70, "small_untracked", { small_use: { day: "2027-01-15", start: 1_800_000_004, end: 1_800_000_118, power_w: 68 } }));
  assert.deepEqual(exampleLines(matched, en, info), exampleLines(seen, en, info));
});

test("too low for the curve is told apart, each with its usual power", () => {
  const low = status(item(20, "too_small"));
  assert.deepEqual(plain(exampleLines(low, en, info)), [
    "Labelled example at 20 W: too low for the whole-house curve, it cannot be tracked from it.",
  ]);
  const both = status(
    item(64, "small_untracked"),
    item(70, "small_untracked"),
    item(90, "small_untracked"),
    item(18, "too_small"),
    item(22, "too_small"),
    item(2000, "no_activation"),
    item(2000, null, { status: "found" }),
  );
  assert.deepEqual(plain(exampleLines(both, en, info)), [
    "Labelled examples at 70 W: the whole-house curve shows them, but appliances this small are not tracked yet.",
    "Labelled examples at 20 W: too low for the whole-house curve, they cannot be tracked from it.",
  ]);
  for (const t of [en, fr]) for (const l of exampleLines(both, t, info)) assert.doesNotMatch(l, /\{|examples_/);
});

test("nothing to say of the other examples", () => {
  assert.deepEqual(exampleLines(null, en, info), []);
  assert.deepEqual(exampleLines(undefined, en, info), []);
  assert.deepEqual(exampleLines(status(), en, info), []);
  const others = status(item(2000, null, { status: "found" }), item(2000, "day_not_analysed"), item(1000, "not_in_registry"), item(150, "measured"));
  assert.deepEqual(exampleLines(others, en, info), []);
});
