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

  // Report counts per entity type rather than enumerating identifiers: a real
  // release pair can involve hundreds of offers, and a wall of ids is unreadable
  // and buries the one line that matters.
  function summarise(entities) {
    const counts = {};
    for (const e of entities) counts[e.entity] = (counts[e.entity] || 0) + 1;
    return Object.keys(counts)
      .sort((a, b) => counts[b] - counts[a] || (a < b ? -1 : 1))
      .map((name) => `${counts[name]} ${name}`)
      .join(", ");
  }

  const removed = report.entities_removed;
  const added = report.entities_added;
  const modified = report.entities_modified;

  if (removed.length) {
    sentences.push(`MISSING from the new response: ${summarise(removed)}.`);
  }
  if (added.length) {
    sentences.push(`NEW in the new response: ${summarise(added)}.`);
  }
  if (modified.length) {
    sentences.push(`CHANGED under the same identifier: ${summarise(modified)}.`);
  }

  const priceChanged = modified.filter((e) =>
    (e.missing || []).some((m) => m.indexOf("TotalAmount") !== -1)
  );
  if (priceChanged.length) {
    sentences.push(
      `${priceChanged.length} of those changed on price ` +
        `(see the report for the exact amounts).`
    );
  }

  const extraCount = Object.keys(report.extra_nodes || {}).length;
  const missingCount = Object.keys(report.missing_nodes || {}).length;
  if (extraCount && missingCount) {
    sentences.push(
      `${extraCount} new node path(s) appeared; ${missingCount} existing node path(s) disappeared.`
    );
  } else if (extraCount) {
    sentences.push(`${extraCount} new node path(s) appeared.`);
  } else if (missingCount) {
    sentences.push(`${missingCount} node path(s) disappeared.`);
  }

  if ((report.value_diffs || []).length) {
    sentences.push(
      `${report.value_diffs.length} value field(s) changed ` +
        `(expand Value changes for detail).`
    );
  }

  const baseOffers = baselineInventory.Offer || 0;
  const newOffers = newInventory.Offer || 0;
  if (baseOffers || newOffers) {
    sentences.push(`Offer count went from ${baseOffers} to ${newOffers}.`);
  }

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

  // Group by entity type with counts and a few examples. A real release pair can
  // involve hundreds of offers, and one line per identifier buries the signal.
  function bucket(entities, heading) {
    if (!entities.length) return;
    const counts = {};
    const examples = {};
    for (const e of entities) {
      counts[e.entity] = (counts[e.entity] || 0) + 1;
      if (!examples[e.entity]) examples[e.entity] = [];
      if (examples[e.entity].length < 3) examples[e.entity].push(e.key);
    }
    lines.push(heading);
    Object.keys(counts)
      .sort((a, b) => counts[b] - counts[a] || (a < b ? -1 : 1))
      .forEach((name) => {
        const count = counts[name];
        const shown = examples[name].join(", ");
        const more =
          count <= examples[name].length
            ? ""
            : `, \u2026 (+${count - examples[name].length} more)`;
        lines.push(`  ${name}: ${count}  (e.g. ${shown}${more})`);
      });
  }

  bucket(report.entities_removed, "\nMISSING entities (in baseline, absent from new):");
  bucket(report.entities_added, "\nEXTRA entities (new, not in baseline):");

  if (report.entities_modified.length) {
    lines.push(
      `\nMODIFIED entities (same identifier, different content): ` +
        `${report.entities_modified.length} total`
    );
    const fieldCounts = {};
    for (const e of report.entities_modified) {
      for (const field of e.missing || []) {
        const key = field.split(" = ")[0];
        fieldCounts[key] = (fieldCounts[key] || 0) + 1;
      }
    }
    Object.keys(fieldCounts)
      .sort((a, b) => fieldCounts[b] - fieldCounts[a] || (a < b ? -1 : 1))
      .slice(0, maxItems)
      .forEach((field) => {
        lines.push(`  ${field}: changed on ${fieldCounts[field]} entit(ies)`);
      });
    report.entities_modified.slice(0, 3).forEach((e) => {
      lines.push(`  e.g. ${e.entity} ${e.key}:`);
      (e.missing || []).slice(0, 4).forEach((m) => lines.push(`      was: ${m}`));
      (e.extra || []).slice(0, 4).forEach((x) => lines.push(`      now: ${x}`));
    });
  }

  const missingPaths = Object.entries(report.missing_nodes).sort();
  if (missingPaths.length) {
    lines.push(`\nMISSING node paths: ${missingPaths.length} distinct`);
    for (const [p, c] of missingPaths.slice(0, maxItems)) lines.push(`  - ${p} x${c}`);
  }

  const extraPaths = Object.entries(report.extra_nodes).sort();
  if (extraPaths.length) {
    lines.push(`\nEXTRA node paths: ${extraPaths.length} distinct`);
    for (const [p, c] of extraPaths.slice(0, maxItems)) lines.push(`  + ${p} x${c}`);
  }

  if (report.value_diffs.length) {
    lines.push(`\nVALUE changes: ${report.value_diffs.length} field(s)`);
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
