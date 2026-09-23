/*
 * Trip-shape and passenger comparison — port of src/airshop/ndc/trip.py.
 *
 * Two AirShoppingRS documents from different releases often describe different
 * journeys, not just different prices: a request may have moved from one-way to
 * round-trip or multi-city, or the passenger mix may have changed. Those are the
 * differences a release reviewer cares about, and a pure node-by-node diff
 * cannot see them.
 *
 * Exposes window.NDCTrip.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (typeof globalThis !== "undefined") globalThis.NDCTrip = api;
  if (root && typeof root === "object") root.NDCTrip = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const PTC_NAMES = {
    ADT: "Adult",
    CHD: "Child",
    INF: "Infant",
    YTH: "Youth",
    SRC: "Senior",
    INS: "Infant seated",
    UNN: "Unaccompanied minor",
    STU: "Student",
    MIL: "Military",
  };

  function ptcName(code) {
    return PTC_NAMES[code] || code;
  }

  function localname(tag) {
    if (!tag) return "";
    let name = tag;
    const brace = name.indexOf("}");
    if (name.charAt(0) === "{" && brace !== -1) name = name.slice(brace + 1);
    const colon = name.indexOf(":");
    if (colon !== -1) name = name.slice(colon + 1);
    return name;
  }

  function elementChildren(element) {
    const out = [];
    const nodes = element.childNodes || [];
    for (let i = 0; i < nodes.length; i += 1) {
      if (nodes[i].nodeType === 1) out.push(nodes[i]);
    }
    return out;
  }

  function directText(element) {
    let text = "";
    const nodes = element.childNodes || [];
    for (let i = 0; i < nodes.length; i += 1) {
      const n = nodes[i];
      if (n.nodeType === 3 || n.nodeType === 4) text += n.nodeValue || n.data || "";
    }
    return text.trim();
  }

  function descendants(root) {
    const out = [];
    const walk = (el) => {
      for (const child of elementChildren(el)) {
        out.push(child);
        walk(child);
      }
    };
    walk(root);
    return out;
  }

  /** First descendant (excluding root) with the given local name. */
  function find(element, name) {
    for (const child of descendants(element)) {
      if (localname(child.tagName) === name) return child;
    }
    return null;
  }

  function findAll(element, name) {
    return descendants(element).filter((c) => localname(c.tagName) === name);
  }

  function text(element) {
    if (!element) return null;
    const value = directText(element);
    return value || null;
  }

  // --- shape extraction -----------------------------------------------------

  function legsFromRq(root) {
    const legs = [];
    for (const od of findAll(root, "OriginDest")) {
      const origin = text(find(od, "OriginCode"));
      const destination = text(find(od, "DestCode"));
      const date =
        text(find(od, "DepartureDate")) || text(find(od, "DepartureDateTime")) || "";
      if (origin && destination) {
        legs.push({ origin, destination, date: date.slice(0, 10) });
      }
    }
    return legs;
  }

  function legsFromRs(root) {
    const legs = [];
    for (const od of findAll(root, "OriginDest")) {
      const origin = text(find(od, "OriginCode"));
      const destination = text(find(od, "DestCode"));
      if (origin && destination) legs.push({ origin, destination, date: "" });
    }
    if (legs.length) return legs;
    // No OriginDestList: fall back to distinct segment routes.
    const seen = {};
    for (const seg of findAll(root, "FlightSegment")) {
      const dep = find(seg, "Departure");
      const arr = find(seg, "Arrival");
      const origin = dep ? text(find(dep, "AirportCode")) : null;
      const destination = arr ? text(find(arr, "AirportCode")) : null;
      if (origin && destination) {
        const key = origin + "->" + destination;
        if (!seen[key]) {
          seen[key] = true;
          legs.push({ origin, destination, date: "" });
        }
      }
    }
    return legs;
  }

  function paxFrom(root) {
    const counts = {};
    for (const pax of findAll(root, "Pax")) {
      const ptc = text(find(pax, "PTC")) || text(find(pax, "PaxType"));
      if (!ptc) continue;
      const code = ptc.toUpperCase();
      counts[code] = (counts[code] || 0) + 1;
    }
    return counts;
  }

  /** Classify a leg list as one-way, round-trip or multi-city. */
  function classify(legs) {
    if (!legs.length) return "unknown";
    if (legs.length === 1) return "one-way";
    if (legs.length === 2) {
      const a = legs[0];
      const b = legs[1];
      if (a.origin === b.destination && a.destination === b.origin) return "round-trip";
      return "multi-city";
    }
    return "multi-city";
  }

  function summarisePax(pax) {
    const keys = Object.keys(pax).sort();
    if (!keys.length) return "unknown";
    return keys.map((k) => pax[k] + " " + ptcName(k)).join(", ");
  }

  function paxTotal(pax) {
    return Object.keys(pax).reduce((sum, k) => sum + pax[k], 0);
  }

  function describeShape(shape) {
    const legs = (shape.legs || [])
      .map((l) => l.origin + "->" + l.destination + (l.date ? " on " + l.date : ""))
      .join("; ");
    const cabin = shape.cabin || "no cabin preference";
    return (
      shape.trip_type +
      " (" + (legs || "no legs") + "), " +
      summarisePax(shape.pax) +
      ", " +
      cabin +
      (shape.currency ? ", " + shape.currency : "")
    );
  }

  function makeShape(tripType, legs, pax, cabin, currency, source) {
    return {
      trip_type: tripType,
      legs: legs,
      pax: pax,
      pax_total: paxTotal(pax),
      pax_summary: summarisePax(pax),
      cabin: cabin,
      currency: currency,
      source: source,
    };
  }

  /** Extract trip shape and passengers from an AirShoppingRQ (authoritative). */
  function tripShapeFromRq(root) {
    const legs = legsFromRq(root);
    const cabinEl = find(root, "CabinTypeName") || find(root, "CabinTypeCode");
    return makeShape(
      classify(legs),
      legs,
      paxFrom(root),
      cabinEl ? text(cabinEl) : null,
      text(find(root, "CurCode")) || "",
      "request"
    );
  }

  /** Extract trip shape from an AirShoppingRS (what was answered). */
  function tripShapeFromRs(root) {
    const legs = legsFromRs(root);
    return makeShape(
      classify(legs),
      legs,
      paxFrom(root),
      null,
      text(find(root, "CurCode")) || "",
      "response"
    );
  }

  /** Diff two trip shapes into a reviewable change set. */
  function compareTripShape(baseline, newShape) {
    const baseKeys = {};
    baseline.legs.forEach((l) => (baseKeys[l.origin + "->" + l.destination] = true));
    const newKeys = {};
    newShape.legs.forEach((l) => (newKeys[l.origin + "->" + l.destination] = true));

    const describeLeg = (l) =>
      l.origin + "->" + l.destination + (l.date ? " on " + l.date : "");

    const legsAdded = newShape.legs
      .filter((l) => !baseKeys[l.origin + "->" + l.destination])
      .map(describeLeg);
    const legsRemoved = baseline.legs
      .filter((l) => !newKeys[l.origin + "->" + l.destination])
      .map(describeLeg);

    const paxAdded = {};
    const paxRemoved = {};
    const allPtc = Array.from(
      new Set(Object.keys(baseline.pax).concat(Object.keys(newShape.pax)))
    ).sort();
    allPtc.forEach((ptc) => {
      const delta = (newShape.pax[ptc] || 0) - (baseline.pax[ptc] || 0);
      if (delta > 0) paxAdded[ptc] = delta;
      else if (delta < 0) paxRemoved[ptc] = -delta;
    });

    const cabinChanged =
      baseline.cabin !== newShape.cabin
        ? [baseline.cabin, newShape.cabin]
        : null;
    const currencyChanged =
      baseline.currency && newShape.currency && baseline.currency !== newShape.currency
        ? [baseline.currency, newShape.currency]
        : null;

    const changed = Boolean(
      baseline.trip_type !== newShape.trip_type ||
        legsAdded.length ||
        legsRemoved.length ||
        Object.keys(paxAdded).length ||
        Object.keys(paxRemoved).length ||
        cabinChanged ||
        currencyChanged
    );

    return {
      changed: changed,
      baseline: baseline,
      new: newShape,
      trip_type_changed: baseline.trip_type !== newShape.trip_type,
      legs_added: legsAdded,
      legs_removed: legsRemoved,
      pax_added: paxAdded,
      pax_removed: paxRemoved,
      cabin_changed: cabinChanged,
      currency_changed: currencyChanged,
    };
  }

  function renderShapeChange(change) {
    if (!change.changed) return "Trip shape unchanged: " + describeShape(change.new);
    const lines = ["TRIP / PASSENGER changes:"];
    if (change.trip_type_changed) {
      lines.push("  - trip type: " + change.baseline.trip_type + " -> " + change.new.trip_type);
    }
    change.legs_removed.forEach((l) => lines.push("  - leg no longer requested: " + l));
    change.legs_added.forEach((l) => lines.push("  + leg newly requested: " + l));
    Object.keys(change.pax_removed).sort().forEach((ptc) => {
      lines.push(
        "  - " + change.pax_removed[ptc] + " " + ptcName(ptc) + " (" + ptc + ") no longer carried"
      );
    });
    Object.keys(change.pax_added).sort().forEach((ptc) => {
      lines.push("  + " + change.pax_added[ptc] + " " + ptcName(ptc) + " (" + ptc + ") added");
    });
    if (change.cabin_changed) {
      lines.push(
        "  - cabin: " +
          (change.cabin_changed[0] || "none") +
          " -> " +
          (change.cabin_changed[1] || "none")
      );
    }
    if (change.currency_changed) {
      lines.push("  - currency: " + change.currency_changed[0] + " -> " + change.currency_changed[1]);
    }
    lines.push("  baseline: " + describeShape(change.baseline));
    lines.push("  new:      " + describeShape(change.new));
    return lines.join("\n");
  }

  return {
    PTC_NAMES: PTC_NAMES,
    ptcName: ptcName,
    tripShapeFromRq: tripShapeFromRq,
    tripShapeFromRs: tripShapeFromRs,
    compareTripShape: compareTripShape,
    renderShapeChange: renderShapeChange,
    describeShape: describeShape,
  };
});
