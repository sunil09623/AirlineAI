/*
 * Offline Q&A over a diff report — the chat backend for the browser app.
 *
 * Deliberately rule-based rather than model-backed: the whole point of this
 * version is that it runs with zero install and zero download, so there is no
 * LLM to call. The intent matcher reads the computed report and answers from it,
 * which also means answers can never contradict the diff.
 *
 * Exposes window.NDCChat.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (typeof globalThis !== "undefined") globalThis.NDCChat = api;
  if (root && typeof root === "object") root.NDCChat = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  /** Human-readable label for a passenger type code. */
  const PTC_NAMES = {
    ADT: "Adult",
    CHD: "Child",
    INF: "Infant",
    YTH: "Youth",
    SRC: "Senior",
  };

  function ptcName(code) {
    return PTC_NAMES[code] || code;
  }

  /** Normalise a question for keyword matching. */
  function normalise(text) {
    return String(text || "").toLowerCase().replace(/\s+/g, " ").trim();
  }

  function has(text) {
    for (let i = 1; i < arguments.length; i += 1) {
      if (text.indexOf(arguments[i]) !== -1) return true;
    }
    return false;
  }

  /**
   * Answer a question about a computed review context.
   *
   * @param {string} question
   * @param {object} ctx - { report, narrative, baselineInventory, newInventory,
   *                         tripChange }
   * @returns {string}
   */
  function answer(question, ctx) {
    if (!ctx || !ctx.report) {
      return (
        "Upload a baseline and a new response first, then press Compare. " +
        "Once the diff has run I can answer questions about it."
      );
    }

    const q = normalise(question);
    const r = ctx.report;

    if (!q) return helpText();

    if (has(q, "help", "what can you", "how do i", "instructions")) {
      return helpText();
    }

    if (
      has(
        q,
        "summary",
        "summarise",
        "summarize",
        "overview",
        "what is different",
        "what's different"
      )
    ) {
      return ctx.narrative || "No differences found.";
    }

    if (has(q, "missing", "removed", "deleted", "gone", "absent", "dropped", "no longer")) {
      const removed = r.entities_removed || [];
      const missingPaths = Object.entries(r.missing_nodes || {});
      if (!removed.length && !missingPaths.length) {
        return "Nothing is missing: the new response contains everything the baseline had.";
      }
      const parts = [];
      if (removed.length) {
        // Counts by entity type, then a few examples — never the whole list.
        const counts = {};
        removed.forEach((e) => (counts[e.entity] = (counts[e.entity] || 0) + 1));
        const byType = Object.keys(counts)
          .sort((a, b) => counts[b] - counts[a])
          .map((name) => counts[name] + " " + name)
          .join(", ");
        parts.push("MISSING from the new response: " + byType + ".");
        const examples = removed.slice(0, 5).map((e) => e.entity + " " + e.key);
        parts.push(
          "Examples: " +
            examples.join(", ") +
            (removed.length > examples.length
              ? " … (+" + (removed.length - examples.length) + " more)"
              : "")
        );
      }
      if (missingPaths.length) {
        const top = missingPaths
          .sort((a, b) => b[1] - a[1])
          .slice(0, 8)
          .map(([p, c]) => "  - " + p + " (x" + c + ")");
        parts.push(
          missingPaths.length +
            " node path(s) lost:\n" +
            top.join("\n") +
            (missingPaths.length > 8
              ? "\n  ...and " + (missingPaths.length - 8) + " more."
              : "")
        );
      }
      return parts.join("\n\n");
    }

    if (has(q, "extra", "added", "new node", "newly", "appeared", "additional")) {
      const added = r.entities_added || [];
      const extraPaths = Object.entries(r.extra_nodes || {});
      if (!added.length && !extraPaths.length) {
        return "Nothing is extra: the new response introduces no new entities or nodes.";
      }
      const parts = [];
      if (added.length) {
        const counts = {};
        added.forEach((e) => (counts[e.entity] = (counts[e.entity] || 0) + 1));
        const byType = Object.keys(counts)
          .sort((a, b) => counts[b] - counts[a])
          .map((name) => counts[name] + " " + name)
          .join(", ");
        parts.push("NEW in the new response: " + byType + ".");
        const examples = added.slice(0, 5).map((e) => e.entity + " " + e.key);
        parts.push(
          "Examples: " +
            examples.join(", ") +
            (added.length > examples.length
              ? " … (+" + (added.length - examples.length) + " more)"
              : "")
        );
      }
      if (extraPaths.length) {
        const top = extraPaths
          .sort((a, b) => b[1] - a[1])
          .slice(0, 8)
          .map(([p, c]) => "  + " + p + " (x" + c + ")");
        parts.push(
          extraPaths.length +
            " new node path(s):\n" +
            top.join("\n") +
            (extraPaths.length > 8
              ? "\n  ...and " + (extraPaths.length - 8) + " more."
              : "")
        );
      }
      return parts.join("\n\n");
    }

    if (
      has(
        q,
        "trip",
        "one way",
        "one-way",
        "oneway",
        "round trip",
        "round-trip",
        "multicity",
        "multi-city",
        "multi city",
        "itinerary"
      )
    ) {
      const t = ctx.tripChange;
      if (!t) return "No trip information was captured for this comparison.";
      if (!t.changed) {
        return "The trip is unchanged: " + describeShape(t.new) + ".";
      }
      const lines = [];
      if (t.trip_type_changed) {
        lines.push("Trip type changed: " + t.baseline.trip_type + " -> " + t.new.trip_type + ".");
      }
      (t.legs_removed || []).forEach((leg) => lines.push("Leg no longer requested: " + leg + "."));
      (t.legs_added || []).forEach((leg) => lines.push("Leg newly requested: " + leg + "."));
      lines.push("Baseline: " + describeShape(t.baseline));
      lines.push("New:      " + describeShape(t.new));
      return lines.join("\n");
    }

    if (
      has(
        q,
        "pax",
        "passenger",
        "adult",
        "child",
        "infant",
        "traveller",
        "traveler",
        "how many people"
      )
    ) {
      const t = ctx.tripChange;
      if (!t) return "No passenger information was captured for this comparison.";
      const base = t.baseline || {};
      const now = t.new || {};
      const added = Object.entries(t.pax_added || {});
      const removed = Object.entries(t.pax_removed || {});
      const lines = [];
      if (!added.length && !removed.length) {
        lines.push("Passenger mix unchanged: " + describePax(base.pax) + ".");
      } else {
        removed.forEach(([ptc, n]) =>
          lines.push(n + " " + ptcName(ptc) + " (" + ptc + ") no longer carried.")
        );
        added.forEach(([ptc, n]) =>
          lines.push(n + " " + ptcName(ptc) + " (" + ptc + ") added.")
        );
      }
      lines.push("Baseline: " + describePax(base.pax) + " (" + (base.pax_total || 0) + " total).");
      lines.push("New:      " + describePax(now.pax) + " (" + (now.pax_total || 0) + " total).");
      return lines.join("\n");
    }

    if (has(q, "count", "inventory", "total", "number of")) {
      const base = ctx.baselineInventory || {};
      const now = ctx.newInventory || {};
      const keys = Array.from(new Set(Object.keys(base).concat(Object.keys(now)))).sort();
      const lines = keys.map((k) => {
        const b = base[k] || 0;
        const n = now[k] || 0;
        const arrow = n === b ? "" : n < b ? " (down)" : " (up)";
        return k + ": " + b + " -> " + n + arrow;
      });
      return "Entity counts:\n" + (lines.join("\n") || "none recorded");
    }

    if (has(q, "offer", "segment", "fare", "baggage", "entity", "entities")) {
      const removed = (r.entities_removed || []).map((e) => e.entity + " " + e.key);
      const added = (r.entities_added || []).map((e) => e.entity + " " + e.key);
      const modified = (r.entities_modified || []).map((e) => e.entity + " " + e.key);
      if (!removed.length && !added.length && !modified.length) {
        return "No entity-level changes: every offer, segment and fare matches the baseline.";
      }
      const lines = [];
      if (removed.length) lines.push("Missing: " + removed.join(", "));
      if (added.length) lines.push("Added: " + added.join(", "));
      if (modified.length) lines.push("Changed (same ID): " + modified.join(", "));
      return lines.join("\n");
    }

    if (has(q, "value", "price", "amount", "cost")) {
      const diffs = r.value_diffs || [];
      if (!diffs.length) return "No value changes in the response content.";
      const lines = [];
      diffs.slice(0, 10).forEach((v) => {
        lines.push(v.path);
        (v.missing_values || []).slice(0, 5).forEach((mv) => lines.push("  was: " + mv));
        (v.extra_values || []).slice(0, 5).forEach((xv) => lines.push("  now: " + xv));
      });
      return lines.join("\n");
    }

    if (has(q, "timestamp", "session", "metadata", "trx", "correlation")) {
      const session = r.session_metadata || [];
      if (!session.length) {
        return "No session metadata differences (no timestamps or transaction IDs changed).";
      }
      const lines = session.map((v) => {
        const was = (v.missing_values || []).join(", ");
        const now = (v.extra_values || []).join(", ");
        return v.path + ": was " + was + " -> now " + now;
      });
      return (
        "Session metadata changed (expected on every response, not content):\n" +
        lines.join("\n")
      );
    }

    if (has(q, "identical", "same", "equal")) {
      return r.identical
        ? "Yes - the two responses are identical in content. Nothing is missing and nothing is extra."
        : "No - they differ. Ask about what is missing, what is extra, or for a summary.";
    }

    return (
      "I could not map that to the report. Try asking about: summary, what is " +
      "missing, what is extra, trip type, passengers, counts, offers, or values.\n\n" +
      "The full report is always available in the Review tab."
    );
  }

  function describePax(pax) {
    if (!pax) return "unknown";
    const parts = Object.keys(pax)
      .sort()
      .map((k) => pax[k] + " " + ptcName(k));
    return parts.length ? parts.join(", ") : "none";
  }

  function describeShape(shape) {
    if (!shape) return "unknown";
    const legs = (shape.legs || [])
      .map((l) => l.origin + "->" + l.destination + (l.date ? " on " + l.date : ""))
      .join("; ");
    return shape.trip_type + " (" + (legs || "no legs") + "), " + describePax(shape.pax);
  }

  function helpText() {
    return (
      "I answer questions about the comparison you just ran, entirely offline.\n\n" +
      "Things you can ask:\n" +
      "  - give me a summary\n" +
      "  - what is missing?\n" +
      "  - what is extra or newly added?\n" +
      "  - did the trip type change?\n" +
      "  - did the passenger mix change?\n" +
      "  - how many offers are there now?\n" +
      "  - which entities changed?\n" +
      "  - what values changed?\n" +
      "  - are the two responses identical?\n\n" +
      "Every answer is read directly from the computed diff, so it cannot " +
      "disagree with the Review tab."
    );
  }

  return { answer: answer, helpText: helpText };
});
