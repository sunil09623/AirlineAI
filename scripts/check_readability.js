// Check that a large realistic dataset renders as a readable summary rather than
// a wall of identifiers. Run with: node /tmp/check_ui.js
const { createRequire } = require("module");
const req = createRequire("/workspace/project/web/verify.mjs");
global.DOMParser = require("/workspace/project/node_modules/xmldom").DOMParser;

const NDC = req("/workspace/project/web/ndc-diff.js");
const NDCReport = req("/workspace/project/web/ndc-report.js");
const NDCChat = req("/workspace/project/web/ndc-chat.js");
const fs = require("fs");

const baseXml = fs.readFileSync("/tmp/real/baseline.xml", "utf8");
const newXml = fs.readFileSync("/tmp/real/new_with_extra.xml", "utf8");

const report = NDC.diffXml(baseXml, newXml, "baseline", "new");
const b = NDC.summarizeMessage(NDC.parseXml(baseXml));
const n = NDC.summarizeMessage(NDC.parseXml(newXml));

console.log("=== NARRATIVE ===");
const narrative = NDCReport.narrative(report, b.entity_counts, n.entity_counts);
console.log(narrative);
console.log();

console.log("=== CHAT: what is missing? ===");
console.log(
  NDCChat.answer("what is missing?", {
    report: report,
    narrative: narrative,
    baselineInventory: b.entity_counts,
    newInventory: n.entity_counts,
  })
);
console.log();

console.log("=== REPORT (first 24 lines) ===");
const out = NDCReport.renderReview(report, b.entity_counts, n.entity_counts);
console.log(out.split("\n").slice(0, 24).join("\n"));
console.log();
console.log("total lines:", out.split("\n").length);
console.log("narrative chars:", narrative.length, "(was ~4000 with a wall of ids)");
