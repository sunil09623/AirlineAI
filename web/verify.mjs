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

// --- churning reference-id coverage -------------------------------------- //
// Real carriers regenerate the tail of each identifier per response. That churn
// must not be reported as content change, while a genuine addition still is.
const { churnSuffix, churnPrefix, identifierNormalisation } = NDC;

check(
  "churnSuffix finds the per-response token",
  churnSuffix(["Xbga0600210b3be03", "Xbga1900210b3be03"]),
  "00210b3be03"
);
check("churnSuffix ignores ordinary keys", churnSuffix(["SEG1", "SEG2_1"]), "");
check(
  "normalisation keeps distinct entities distinct",
  Object.keys(
    Object.fromEntries(
      Object.entries(
        identifierNormalisation({
          "BaggageAllowance\u0000@BaggageAllowanceID": [
            "Xbga0600210b3be03",
            "Xbga1900210b3be03",
          ],
        })["BaggageAllowance\u0000@BaggageAllowanceID"]
      ).map(([, v]) => [v, true])
    )
  ).length,
  2
);

// Prefix churn: real carrier offer ids reissue a UUID prefix per response while
// the trailing ordinal stays stable. This must not read as everything changing.
check(
  "churnPrefix finds the regenerated batch id",
  churnPrefix(["XA67C09C8-37C4-4844-82B2-1", "XA67C09C8-37C4-4844-82B2-2"]),
  "XA67C09C8-37C4-4844-82B2-"
);
check(
  "churnPrefix ignores plain numeric keys",
  churnPrefix(["SEG1", "SEG2"]),
  ""
);
check(
  "prefix normalisation strips to the stable ordinal",
  identifierNormalisation({
    "Offer\u0000@OfferID": [
      "XA67C09C8-37C4-4844-82B2-1",
      "XA67C09C8-37C4-4844-82B2-2",
    ],
  })["Offer\u0000@OfferID"]["XA67C09C8-37C4-4844-82B2-1"],
  "1"
);

function churnDoc(suffix, extra) {
  const bgas = [
    `<BaggageAllowance BaggageAllowanceID="Xbga0600${suffix}"><Type>Checked</Type></BaggageAllowance>`,
    `<BaggageAllowance BaggageAllowanceID="Xbga1900${suffix}"><Type>Checked</Type></BaggageAllowance>`,
  ];
  if (extra) {
    bgas.push(
      `<BaggageAllowance BaggageAllowanceID="Xbga9900${suffix}"><Type>CarryOn</Type></BaggageAllowance>`
    );
  }
  const fares = ["09", "0e", "11"]
    .map(
      (k) =>
        `<FareGroup ListKey="Xfbc${k}00${suffix}"><FareBasisCode>${k.toUpperCase()}</FareBasisCode></FareGroup>`
    )
    .join("");
  return (
    `<AirShoppingRS><DataLists>` +
    `<BaggageAllowanceList>${bgas.join("")}</BaggageAllowanceList>` +
    `<FareList>${fares}</FareList>` +
    `</DataLists></AirShoppingRS>`
  );
}

const churnA = churnDoc("210b3be03", false);
const churnB = churnDoc("0a0b3c8d7", false);
const churnC = churnDoc("0a0b3c8d7", true);
const pureChurn = diffXml(churnA, churnB, "old", "new");
const churnPlusAdd = diffXml(churnA, churnC, "old", "new");

check("pure id churn is not a difference", pureChurn.identical, true);
check("pure id churn yields no value diffs", pureChurn.value_diffs, []);

// End-to-end prefix churn: two responses whose only difference is a reissued
// UUID prefix and matching ordinals must read as identical.
function prefixChurnDoc(prefix) {
  const offers = [];
  for (let i = 1; i <= 40; i += 1) {
    offers.push(
      `<Offer OfferID="${prefix}-${i}">` +
        `<OfferItem OfferItemID="${prefix}-${i}-OI1">` +
        `<Price><TotalAmount CurCode="USD">${400 + i}.00</TotalAmount></Price>` +
        `</OfferItem></Offer>`
    );
  }
  return `<AirShoppingRS><Response><Offers>${offers.join("")}</Offers></Response></AirShoppingRS>`;
}
const prefixA = prefixChurnDoc("XA67C09C8-37C4-4844-82B2");
const prefixB = prefixChurnDoc("XAA4633E4-E3C1-43D3-9153");
const prefixReport = diffXml(prefixA, prefixB, "old", "new");
check("prefix-only churn is not a difference", prefixReport.identical, true);
check("prefix-only churn yields no entity changes", prefixReport.entities_removed.length, 0);
check("prefix-only churn yields no value diffs", prefixReport.value_diffs.length, 0);

// A genuinely added offer must still be visible through the prefix change.
const prefixC = prefixChurnDoc("XAA4633E4-E3C1-43D3-9153").replace(
  "</Offers>",
  `<Offer OfferID="XAA4633E4-E3C1-43D3-9153-999"><OfferItem OfferItemID="XAA4633E4-E3C1-43D3-9153-999-OI1"><Price><TotalAmount CurCode="USD">9999.00</TotalAmount></Price></OfferItem></Offer></Offers>`
);
const prefixAddReport = diffXml(prefixA, prefixC, "old", "new");
check(
  "genuine addition survives prefix churn",
  prefixAddReport.entities_added.map((e) => e.key).sort(),
  ["999", "999-OI1"]
);

check(
  "genuine addition still surfaces after churn is stripped",
  churnPlusAdd.entities_added.map((e) => e.key),
  ["Xbga99"]
);
check("genuine addition is not identical", churnPlusAdd.identical, false);

// Volatile session fields are reported separately, not as content churn.
const volA =
  '<AirShoppingRS><PayloadAttributes><Timestamp>2026-01-01T00:00:00Z</Timestamp></PayloadAttributes></AirShoppingRS>';
const volB =
  '<AirShoppingRS><PayloadAttributes><Timestamp>2026-01-02T00:00:00Z</Timestamp></PayloadAttributes></AirShoppingRS>';
const volReport = diffXml(volA, volB, "a", "b");
check("timestamp-only change is identical", volReport.identical, true);
check("timestamp is classed as session metadata", volReport.session_metadata.length, 1);

// --- trip / passenger parity with the Python implementation ---------------- //
const NDCTrip = require("./ndc-trip.js");
const NDCChat = require("./ndc-chat.js");

function rqXml(pax, legs, cabin) {
  const travelers = pax
    .map((p, i) => `<Pax><PaxID>${p}${i + 1}</PaxID><PTC>${p}</PTC></Pax>`)
    .join("");
  const ods = legs
    .map(
      (l) =>
        `<OriginDest><OriginCode>${l[0]}</OriginCode><DestCode>${l[1]}</DestCode>` +
        `<DepartureDate>${l[2]}</DepartureDate><CabinTypeName>${cabin}</CabinTypeName></OriginDest>`
    )
    .join("");
  return (
    `<AirShoppingRQ><PayloadAttributes><TrxID>T1</TrxID></PayloadAttributes>` +
    `<Request><FlightRequest>${ods}</FlightRequest><PaxList>${travelers}</PaxList>` +
    `<ShoppingCriteria><CurParameter><CurCode>USD</CurCode></CurParameter></ShoppingCriteria>` +
    `</Request></AirShoppingRQ>`
  );
}

const rqOneWay = rqXml(["ADT"], [["LHR", "JFK", "2027-03-15"]], "Economy");
const rqRound = rqXml(
  ["ADT", "ADT", "CHD"],
  [["LHR", "JFK", "2027-03-15"], ["JFK", "LHR", "2027-03-22"]],
  "Business"
);
const rqMulti = rqXml(
  ["ADT"],
  [["LHR", "JFK", "2027-03-15"], ["JFK", "CDG", "2027-03-20"], ["CDG", "LHR", "2027-03-25"]],
  "Economy"
);

const shapeOneWay = NDCTrip.tripShapeFromRq(NDC.parseXml(rqOneWay));
const shapeRound = NDCTrip.tripShapeFromRq(NDC.parseXml(rqRound));
const shapeMulti = NDCTrip.tripShapeFromRq(NDC.parseXml(rqMulti));

check("one-way classified", shapeOneWay.trip_type, "one-way");
check("round-trip classified", shapeRound.trip_type, "round-trip");
check("multi-city classified", shapeMulti.trip_type, "multi-city");
check("pax mix read", shapeRound.pax, { ADT: 2, CHD: 1 });
check("pax total read", shapeRound.pax_total, 3);
check("cabin read", shapeRound.cabin, "Business");
check("currency read", shapeRound.currency, "USD");

const tripChange = NDCTrip.compareTripShape(shapeOneWay, shapeRound);
check("trip type change detected", tripChange.trip_type_changed, true);
check("added leg detected", tripChange.legs_added, ["JFK->LHR on 2027-03-22"]);
check("pax additions detected", tripChange.pax_added, { ADT: 1, CHD: 1 });
check("cabin change detected", tripChange.cabin_changed, ["Economy", "Business"]);
check("unchanged trip reports unchanged", NDCTrip.compareTripShape(shapeOneWay, NDCTrip.tripShapeFromRq(NDC.parseXml(rqOneWay))).changed, false);

// --- chat answers are grounded in the report ------------------------------ //
const chatCtx = {
  report: report,
  narrative: narrative(report, baselineSummary.entity_counts, newSummary.entity_counts),
  baselineInventory: baselineSummary.entity_counts,
  newInventory: newSummary.entity_counts,
  tripChange: tripChange,
};

check(
  "chat summary matches narrative",
  NDCChat.answer("give me a summary", chatCtx),
  chatCtx.narrative
);
check(
  "chat reports missing offers",
  NDCChat.answer("what is missing?", chatCtx).indexOf("IT4") !== -1,
  true
);
check(
  "chat rejects unmapped questions helpfully",
  NDCChat.answer("what is the weather", chatCtx).indexOf("could not map") !== -1,
  true
);
check(
  "chat handles no-report state",
  NDCChat.answer("summary", null).indexOf("Upload") !== -1,
  true
);

console.log(
  failures === 0
    ? "\nAll checks passed: the JS engine matches the Python reference.\n"
    : `\n${failures} check(s) FAILED: the JS engine diverges from Python.\n`
);
process.exit(failures === 0 ? 0 : 1);
