/*
 * Plain-language summary and text report for a diff.
 * Ports _narrative() and DiffReport.render() from the Python implementation.
 *
 * UMD-style global/CommonJS export, so index.html works from file:// as well as
 * from a served origin. Exposes window.NDCReport.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (typeof globalThis !== "undefined") globalThis.NDCReport = api;
  if (root && typeof root === "object") root.NDCReport = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

/** Trim the common AirShoppingRS/Response/ prefix for readability. */
function shortPath(path) {
  return path.replace("AirShoppingRS/Response/", "");
}

/**
 * Plain-language summary of the diff — ported from _narrative() in
 * src/airshop/web/server.py. Requires no model.
 */
function narrative(report, baselineInventory, newInventory) {
  if (report.identical) {
    return (
      "The new response is identical to the baseline. Nothing is missing and " +
      "nothing has been added."
    );
  }

  const sentences = [];

  const removed = report.entities_removed.filter((e) => e.entity === "Offer");
  const added = report.entities_added.filter((e) => e.entity === "Offer");

  if (removed.length) {
    const ids = removed.map((e) => e.key).join(", ");
    sentences.push(
      `${removed.length} offer(s) present in the baseline are MISSING from the ` +
        `new response: ${ids}.`
    );
  }
  if (added.length) {
    const ids = added.map((e) => e.key).join(", ");
    sentences.push(`${added.length} new offer(s) appeared: ${ids}.`);
  }

  const modifiedOffers = report.entities_modified.filter((e) => e.entity === "Offer");
  const priceChanges = new Set();
  for (const e of modifiedOffers) {
    for (const miss of e.missing || []) {
      if (miss.includes("TotalAmount")) priceChanges.add(e.key);
    }
  }
  if (modifiedOffers.length) {
    sentences.push(
      `${modifiedOffers.length} offer(s) kept their ID but changed content.`
    );
  }
  if (priceChanges.size) {
    sentences.push(`Prices changed on: ${Array.from(priceChanges).sort().join(", ")}.`);
  }

  const extraEntries = topEntries(report.extra_nodes);
  if (extraEntries.length) {
    sentences.push(
      "New nodes appeared under: " +
        extraEntries.map(([p]) => shortPath(p)).join(", ") +
        "."
    );
  }
  const missingEntries = topEntries(report.missing_nodes);
  if (missingEntries.length) {
    sentences.push(
      "Nodes no longer present under: " +
        missingEntries.map(([p]) => shortPath(p)).join(", ") +
        "."
    );
  }

  const baseOffers = baselineInventory.Offer || 0;
  const newOffers = newInventory.Offer || 0;
  sentences.push(`Offer count went from ${baseOffers} to ${newOffers}.`);

  return sentences.join(" ");
}

/**
 * Highest-count entries first, at most five.
 *
 * Python's sorted(key=-count) is stable, so ties keep the order paths were first
 * seen. Array.prototype.sort is stable too, so sorting only on count reproduces
 * exactly the same selection.
 */
function topEntries(counts) {
  return Object.entries(counts)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 5);
}

/**
 * Full text report — ported from DiffReport.render() in src/airshop/ndc/diff.py.
 */
function renderReport(report, maxItems = 40) {
  if (report.identical) {
    return (
      `No differences: ${report.new} is structurally identical to ${report.base}.`
    );
  }

  const lines = [`Comparing ${report.base} (baseline) vs ${report.new} (new):`];

  if (report.entities_removed.length) {
    lines.push("\nMISSING entities (in baseline, absent from new):");
    for (const e of report.entities_removed.slice(0, maxItems)) {
      lines.push(`  - ${e.entity} ${e.key} was removed`);
    }
  }
  if (report.entities_added.length) {
    lines.push("\nEXTRA entities (new, not in baseline):");
    for (const e of report.entities_added.slice(0, maxItems)) {
      lines.push(`  - ${e.entity} ${e.key} was added`);
    }
  }
  if (report.entities_modified.length) {
    lines.push("\nMODIFIED entities (same ID, different content):");
    for (const e of report.entities_modified.slice(0, maxItems)) {
      lines.push(`  - ${e.entity} ${e.key}:`);
      for (const m of (e.missing || []).slice(0, maxItems)) lines.push(`      missing: ${m}`);
      for (const x of (e.extra || []).slice(0, maxItems)) lines.push(`      extra:   ${x}`);
    }
  }

  const missingPaths = Object.entries(report.missing_nodes).sort();
  if (missingPaths.length) {
    lines.push("\nMISSING node paths (tag -> count):");
    for (const [p, c] of missingPaths.slice(0, maxItems)) lines.push(`  - ${p} x${c}`);
  }

  const extraPaths = Object.entries(report.extra_nodes).sort();
  if (extraPaths.length) {
    lines.push("\nEXTRA node paths (tag -> count):");
    for (const [p, c] of extraPaths.slice(0, maxItems)) lines.push(`  + ${p} x${c}`);
  }

  if (report.value_diffs.length) {
    lines.push("\nVALUE changes (same path, different values):");
    for (const v of report.value_diffs.slice(0, maxItems)) {
      for (const mv of (v.missing_values || []).slice(0, 10)) {
        lines.push(`  - ${v.path}: '${mv}' no longer present`);
      }
      for (const xv of (v.extra_values || []).slice(0, 10)) {
        lines.push(`  + ${v.path}: '${xv}' newly present`);
      }
    }
  }

  const session = report.session_metadata || [];
  if (session.length) {
    lines.push(
      "\nSession metadata (regenerated per response - not content changes):"
    );
    for (const v of session.slice(0, maxItems)) {
      for (const mv of (v.missing_values || []).slice(0, 5)) {
        lines.push(`    ${v.path}: was '${mv}'`);
      }
      for (const xv of (v.extra_values || []).slice(0, 5)) {
        lines.push(`    ${v.path}: now '${xv}'`);
      }
    }
  }

  return lines.join("\n");
}

/**
 * Full review - the diff report plus the baseline and new entity inventories.
 * Mirrors UpdateReview.render() in src/airshop/ndc/catalog.py, which wraps
 * DiffReport.render() and appends the counts.
 */
function renderReview(report, baselineInventory, newInventory) {
  const parts = [renderReport(report)];
  parts.push("\nBaseline inventory:");
  for (const [key, value] of Object.entries(baselineInventory)) {
    parts.push(`  ${key}: ${value}`);
  }
  parts.push("New inventory:");
  for (const [key, value] of Object.entries(newInventory)) {
    parts.push(`  ${key}: ${value}`);
  }
  return parts.join("\n");
}

return {
  narrative: narrative,
  renderReport: renderReport,
  renderReview: renderReview,
};
});
