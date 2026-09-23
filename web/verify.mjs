/*
 * Verify the JavaScript diff engine against the Python reference output.
 *
 * Usage:
 *   node web/verify.mjs
 *
 * Requires Node 18+. Compares web/expected/diff_baseline_vs_v2.json (produced by
 * the Python implementation) against the same inputs run through the JS engine,
 * so the two implementations cannot silently drift apart.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

// DOMParser lives in the browser; install a spec-compatible shim before the
// engine module is exercised. Imported dynamically so the browser bundle never
// pulls in a Node-only dependency.
if (typeof globalThis.DOMParser === "undefined") {
  const { DOMParser } = await import("xmldom");
  globalThis.DOMParser = DOMParser;
}

// Load the UMD modules. In Node, `module.exports` is populated by the factory;
// createRequire keeps the harness working as an ESM script while the modules
// themselves stay classic scripts so the browser can load them from file://.
const { createRequire } = await import("node:module");
const require = createRequire(import.meta.url);
const NDC = require("./ndc-diff.js");
const NDCReport = require("./ndc-report.js");
const { diffXml, summarizeMessage, parseXml } = NDC;
const { narrative, renderReport, renderReview } = NDCReport;

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");

const baseline = readFileSync(
  join(root, "samples", "airshopping_rs_baseline.xml"),
  "utf8"
);
const updated = readFileSync(
  join(root, "samples", "airshopping_rs_v2.xml"),
  "utf8"
);
const expected = JSON.parse(
  readFileSync(join(here, "expected", "diff_baseline_vs_v2.json"), "utf8")
);

const report = diffXml(
  baseline,
  updated,
  "airshopping_rs_baseline.xml",
  "airshopping_rs_v2.xml"
);

const baselineSummary = summarizeMessage(parseXml(baseline), "airshopping_rs_baseline.xml");
const newSummary = summarizeMessage(parseXml(updated), "airshopping_rs_v2.xml");

let failures = 0;

function check(name, actual, want) {
  const a = stable(actual);
  const w = stable(want);
  if (a === w) {
    console.log(`  PASS  ${name}`);
  } else {
    failures += 1;
    console.log(`  FAIL  ${name}`);
    console.log(`        expected: ${w}`);
    console.log(`        actual:   ${a}`);
  }
}

/**
 * Canonical JSON with object keys sorted, so a comparison is not defeated by
 * key insertion order, which carries no meaning here.
 */
function stable(value) {
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  if (value && typeof value === "object") {
    const keys = Object.keys(value).sort();
    return `{${keys.map((k) => `${JSON.stringify(k)}:${stable(value[k])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

console.log("Comparing JS engine output against the Python reference:\n");

check("identical flag", report.identical, expected.identical);
check(
  "entities_removed",
  report.entities_removed.map((e) => [e.entity, e.key]),
  expected.entities_removed.map((e) => [e.entity, e.key])
);
check(
  "entities_added",
  report.entities_added.map((e) => [e.entity, e.key]),
  expected.entities_added.map((e) => [e.entity, e.key])
);
check(
  "entities_modified keys",
  report.entities_modified.map((e) => [e.entity, e.key]).sort(),
  expected.entities_modified.map((e) => [e.entity, e.key]).sort()
);

// Compare modified-entity field details, normalised for order.
function normaliseModified(list) {
  return list
    .map((e) => ({ entity: e.entity, key: e.key, missing: [...e.missing].sort(), extra: [...e.extra].sort() }))
    .sort((a, b) => (a.key < b.key ? -1 : 1));
}
check("entities_modified detail", normaliseModified(report.entities_modified), normaliseModified(expected.entities_modified));

check("missing_nodes", report.missing_nodes, expected.missing_nodes);
check("extra_nodes", report.extra_nodes, expected.extra_nodes);
check(
  "value_diffs",
  [...report.value_diffs].sort((a, b) => (a.struct < b.struct ? -1 : 1)),
  [...expected.value_diffs].sort((a, b) => (a.struct < b.struct ? -1 : 1))
);

check("baseline inventory", baselineSummary.entity_counts, expected.baseline_inventory);
check("new inventory", newSummary.entity_counts, expected.new_inventory);

// Narrative must match the Python wording exactly.
const jsNarrative = narrative(report, baselineSummary.entity_counts, newSummary.entity_counts);
check("narrative", jsNarrative, expected.narrative);

const jsRender = renderReport(report);
check("render", jsRender, expected.render);

const jsReviewRender = renderReview(
  report,
  baselineSummary.entity_counts,
  newSummary.entity_counts
);
check("review_render", jsReviewRender, expected.review_render);

// An identical pair must report no differences.
const same = diffXml(baseline, baseline, "a", "b");
check("identical documents report no differences", same.identical, true);
check(
  "identical documents produce the no-diff message",
  renderReport(same),
  "No differences: b is structurally identical to a."
);

// A reordered document must not produce false differences, since the structural
// pass is order-independent.
const reordered = baseline.replace(
  /(<Offers>)([\s\S]*?)(<\/Offers>)/,
  (_m, open, inner, close) => {
    const offers = inner.match(/<Offer>[\s\S]*?<\/Offer>/g) || [];
    return open + offers.reverse().join("") + close;
  }
);
const reorderReport = diffXml(baseline, reordered, "a", "b");
check("reordering offers is not a difference", reorderReport.identical, true);

console.log(
  failures === 0
    ? "\nAll checks passed: the JS engine matches the Python reference.\n"
    : `\n${failures} check(s) FAILED: the JS engine diverges from Python.\n`
);
process.exit(failures === 0 ? 0 : 1);
