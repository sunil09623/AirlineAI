/*
 * Baseline similarity matching — port of src/airshop/ndc/similarity.py.
 *
 * The reviewer keeps a library of baselines and a stream of new releases.
 * Comparing against the wrong baseline produces a meaningless report, so this
 * chooses the nearest-looking baseline and records *why*, letting the UI show
 * the reasoning rather than just asserting a number.
 *
 * Exposes window.NDCSimilarity.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (typeof globalThis !== "undefined") globalThis.NDCSimilarity = api;
  if (root && typeof root === "object") root.NDCSimilarity = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // Relative importance of each signal.
  //
  // Signals describing *what was asked for* (route, dates, airports, trip type,
  // passengers, cabin, currency) are far more diagnostic of "same kind of
  // request" than signals describing *how it was answered* (node-path shape,
  // entity mix, offer count). The latter are near-identical across two responses
  // from the same schema, so weighting them equally compressed every score into a
  // narrow band and let a wrong route win on incidental agreement.
  const WEIGHTS = {
    routes: 6.0,
    dates: 3.0,
    airports: 2.5,
    trip_type: 2.5,
    pax: 2.0,
    carriers: 2.0,
    cabins: 2.0,
    currency: 1.5,
    entities: 1.5,
    hops: 1.0,
    structure: 1.0,
    offer_count: 1.0,
  };

  // Below this, the match is weak enough that the reviewer should be told.
  const WEAK_MATCH_THRESHOLD = 0.45;

  // Signals that are decisive on their own. Two requests sharing no airport at
  // all are for different markets, however similar their schemas are, so when
  // every available decisive signal scores zero the total is capped rather than
  // allowed to accumulate credit from schema-level agreement.
  const DECISIVE_SIGNALS = ["routes", "airports"];
  const MISMATCH_CAP = 0.25;

  // Below this total available weight, too few signals were comparable.
  const LOW_CONFIDENCE_WEIGHT = 6.0;

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

  /** Requested routes, segment hops and referenced airports, kept apart.
   *
   * An OriginDest pair is what was *asked for* and identifies the market. A
   * segment hop is *how it was answered*: the same LHR->JFK request can be served
   * non-stop or via CDG, IST and MAD. Folding hops into the route set made two
   * responses for the same market look unrelated, and inflated the similarity of
   * two responses that merely shared a hub.
   */
  function routesHopsAirports(root) {
    const routes = [];
    const hops = [];
    const airports = [];
    const seenRoute = {};
    const seenHop = {};
    const seenAirport = {};

    for (const od of descendants(root)) {
      if (localname(od.tagName) !== "OriginDest") continue;
      let origin = null;
      let dest = null;
      for (const child of descendants(od)) {
        const name = localname(child.tagName);
        const value = directText(child);
        if (name === "OriginCode" && value) origin = value;
        else if (name === "DestCode" && value) dest = value;
      }
      if (origin && dest) {
        const key = origin + "->" + dest;
        if (!seenRoute[key]) {
          seenRoute[key] = true;
          routes.push([origin, dest]);
        }
        if (!seenAirport[origin]) { seenAirport[origin] = true; airports.push(origin); }
        if (!seenAirport[dest]) { seenAirport[dest] = true; airports.push(dest); }
      }
    }

    for (const segment of descendants(root)) {
      if (localname(segment.tagName) !== "FlightSegment") continue;
      const codes = [];
      for (const child of descendants(segment)) {
        if (localname(child.tagName) !== "AirportCode") continue;
        const value = directText(child);
        if (value) codes.push(value);
      }
      if (codes.length >= 2) {
        const key = codes[0] + "->" + codes[1];
        if (!seenHop[key]) {
          seenHop[key] = true;
          hops.push([codes[0], codes[1]]);
        }
      }
    }

    // Airports include every code seen, so markets still overlap when a response
    // carries no OriginDest list and only segment data.
    for (const element of descendants(root)) {
      if (localname(element.tagName) !== "AirportCode") continue;
      const value = directText(element);
      if (value && !seenAirport[value]) {
        seenAirport[value] = true;
        airports.push(value);
      }
    }

    return { routes, hops, airports };
  }

  /** Departure dates referenced anywhere in the message. */
  function collectDates(root) {
    const found = [];
    const seen = {};
    const add = (value) => {
      if (value && !seen[value]) {
        seen[value] = true;
        found.push(value);
      }
    };
    for (const element of descendants(root)) {
      const name = localname(element.tagName);
      const value = directText(element);
      if (!value) continue;
      if (name === "DepartureDate" || name === "DepartureDateTime") {
        add(value.slice(0, 10));
      } else if (
        name === "Date" &&
        value.length >= 10 &&
        value.charAt(4) === "-" &&
        value.charAt(7) === "-"
      ) {
        add(value.slice(0, 10));
      }
    }
    return found;
  }

  function paxFrom(root) {
    const counts = {};
    for (const pax of descendants(root)) {
      if (localname(pax.tagName) !== "Pax") continue;
      let ptc = null;
      for (const child of descendants(pax)) {
        const name = localname(child.tagName);
        if (name === "PTC" || name === "PaxType") {
          const value = directText(child);
          if (value) { ptc = value.toUpperCase(); break; }
        }
      }
      if (ptc) counts[ptc] = (counts[ptc] || 0) + 1;
    }
    return counts;
  }

  function entityCounts(root, ENTITY_KEYS) {
    const counts = {};
    for (const element of descendants(root)) {
      const tag = localname(element.tagName);
      const spec = ENTITY_KEYS[tag];
      if (!spec) continue;
      let value = null;
      if (spec.charAt(0) === "@") {
        value = element.getAttribute(spec.slice(1));
      } else {
        for (const child of elementChildren(element)) {
          if (localname(child.tagName) === spec) {
            value = directText(child) || null;
            break;
          }
        }
        if (!value) value = element.getAttribute(spec);
      }
      if (value) counts[tag] = (counts[tag] || 0) + 1;
    }
    return counts;
  }

  /** Content-independent signature of an AirShopping message. */
  function fingerprint(root, label, deps) {
    const { ENTITY_KEYS, flatten, tripShapeFromRs } = deps;
    const { routes, hops, airports } = routesHopsAirports(root);

    const cabins = [];
    const carriers = [];
    const seenCabin = {};
    const seenCarrier = {};
    for (const element of descendants(root)) {
      const name = localname(element.tagName);
      const value = directText(element);
      if (!value) continue;
      if (name === "CabinTypeCode" || name === "CabinTypeName") {
        if (!seenCabin[value]) { seenCabin[value] = true; cabins.push(value); }
      } else if (
        name === "OwnerCode" ||
        name === "ValidatingCarrierCode" ||
        name === "MarketingCarrierAirlineID" ||
        name === "OperatingCarrierAirlineID" ||
        name === "AirlineDesigCode"
      ) {
        if (!seenCarrier[value]) { seenCarrier[value] = true; carriers.push(value); }
      }
    }

    let currency = "";
    for (const element of descendants(root)) {
      if (localname(element.tagName) === "CurCode") {
        currency = directText(element) || "";
        if (currency) break;
      }
    }

    const shape = tripShapeFromRs(root);
    const counts = entityCounts(root, ENTITY_KEYS);
    const structs = {};
    for (const record of flatten(root)) structs[record.struct] = true;

    return {
      label: label,
      root: localname(root.tagName),
      trip_type: shape.trip_type,
      routes: routes,
      hops: hops,
      airports: airports,
      pax: shape.pax,
      cabins: cabins,
      carriers: carriers,
      currency: currency,
      dates: collectDates(root),
      structs: structs,
      entity_counts: counts,
      offer_count: counts.Offer || 0,
    };
  }

  // --- signal comparisons: each returns 0..1, or null when not comparable ---

  /** Overlap of two key lists, or null when either side lacks the signal.
   *
   * Returning null rather than 0 when one side is empty is deliberate. A response
   * with no carriers listed, or with no OriginDest list at all, has not expressed
   * an opinion on that signal. Scoring 0 would read as "disagrees about routes"
   * and penalise a legitimate baseline for a schema difference.
   */
  function jaccard(left, right) {
    if (!left.length || !right.length) return null;
    const leftSet = {};
    left.forEach((v) => (leftSet[keyOf(v)] = true));
    let shared = 0;
    right.forEach((v) => { if (leftSet[keyOf(v)]) shared += 1; });
    const union = Object.keys(leftSet).length + right.length - shared;
    if (!union) return null;
    return shared / union;
  }

  function keyOf(value) {
    return Array.isArray(value) ? value.join("->") : String(value);
  }

  function paxSimilarity(left, right) {
    const keys = Object.keys(left).concat(
      Object.keys(right).filter((k) => !(k in left))
    );
    if (!keys.length) return null;
    let total = 0;
    let distance = 0;
    keys.forEach((k) => {
      const a = left[k] || 0;
      const b = right[k] || 0;
      total += Math.max(a, b);
      distance += Math.abs(a - b);
    });
    if (total === 0) return null;
    return 1 - distance / total;
  }

  function countSimilarity(left, right) {
    if (left === 0 && right === 0) return null;
    return 1 - Math.abs(left - right) / Math.max(left, right);
  }

  function entitySimilarity(left, right) {
    const keys = Object.keys(left).concat(
      Object.keys(right).filter((k) => !(k in left))
    );
    if (!keys.length) return null;
    let total = 0;
    let overlap = 0;
    keys.forEach((k) => {
      const a = left[k] || 0;
      const b = right[k] || 0;
      total += Math.max(a, b);
      overlap += Math.min(a, b);
    });
    if (total === 0) return null;
    return overlap / total;
  }

  function explain(signal, score, left, right) {
    if (signal === "routes") {
      const shared = jaccard(left.routes, right.routes);
      return `${Math.round((shared || 0) * left.routes.length)} shared route(s)`;
    }
    if (signal === "trip_type") {
      return score === 1
        ? `both ${left.trip_type}`
        : `${left.trip_type} vs ${right.trip_type}`;
    }
    if (signal === "structure") {
      const leftKeys = Object.keys(left.structs);
      const rightKeys = Object.keys(right.structs);
      const shared = leftKeys.filter((k) => right.structs[k]).length;
      return `${shared} shared node paths of ${leftKeys.length + rightKeys.length - shared}`;
    }
    if (signal === "dates") {
      const shared = left.dates.filter((d) => right.dates.indexOf(d) !== -1).length;
      return `${shared} shared date(s)`;
    }
    if (signal === "pax") {
      return `${formatPax(left.pax)} vs ${formatPax(right.pax)}`;
    }
    if (signal === "carriers") {
      const shared = left.carriers.filter((c) => right.carriers.indexOf(c) !== -1);
      return shared.length ? shared.join(", ") : "none shared";
    }
    if (signal === "entities") {
      return `entity mix overlap ${Math.round(score * 100)}%`;
    }
    if (signal === "airports") {
      const shared = left.airports.filter((a) => right.airports.indexOf(a) !== -1);
      return `${shared.length} shared airport(s)`;
    }
    if (signal === "hops") {
      const shared = left.hops.filter(
        (h) => right.hops.map(keyOf).indexOf(keyOf(h)) !== -1
      );
      return `${shared.length} shared connection(s)`;
    }
    if (signal === "cabins") {
      const shared = left.cabins.filter((c) => right.cabins.indexOf(c) !== -1);
      return shared.length ? shared.join(", ") : "none shared";
    }
    if (signal === "currency") {
      return `${left.currency || "unset"} vs ${right.currency || "unset"}`;
    }
    if (signal === "offer_count") {
      return `${left.offer_count} vs ${right.offer_count} offer(s)`;
    }
    return `score ${score.toFixed(2)}`;
  }

  function formatPax(pax) {
    const keys = Object.keys(pax || {});
    if (!keys.length) return "none";
    return keys.sort().map((k) => `${pax[k]} ${k}`).join(", ");
  }

  /** Score how closely `candidate` resembles `baseline`. */
  function scoreMatch(baseline, candidate) {
    const signals = [];

    signals.push(["routes", jaccard(baseline.routes, candidate.routes)]);

    if (baseline.trip_type !== "unknown" && candidate.trip_type !== "unknown") {
      signals.push([
        "trip_type",
        baseline.trip_type === candidate.trip_type ? 1.0 : 0.0,
      ]);
    } else {
      signals.push(["trip_type", null]);
    }

    signals.push(["structure", jaccard(Object.keys(baseline.structs), Object.keys(candidate.structs))]);
    signals.push(["dates", jaccard(baseline.dates, candidate.dates)]);
    signals.push(["pax", paxSimilarity(baseline.pax, candidate.pax)]);
    signals.push(["carriers", jaccard(baseline.carriers, candidate.carriers)]);
    signals.push(["entities", entitySimilarity(baseline.entity_counts, candidate.entity_counts)]);
    signals.push(["airports", jaccard(baseline.airports, candidate.airports)]);
    signals.push(["hops", jaccard(baseline.hops, candidate.hops)]);
    signals.push(["cabins", jaccard(baseline.cabins, candidate.cabins)]);

    if (baseline.currency && candidate.currency) {
      signals.push([
        "currency",
        baseline.currency === candidate.currency ? 1.0 : 0.0,
      ]);
    } else {
      signals.push(["currency", null]);
    }

    signals.push([
      "offer_count",
      countSimilarity(baseline.offer_count, candidate.offer_count),
    ]);

    const reasons = [];
    let weighted = 0;
    let available = 0;
    let decisiveSeen = 0;
    let decisiveMatched = 0;

    signals.forEach(([signal, score]) => {
      if (score === null) return;
      const weight = WEIGHTS[signal];
      weighted += score * weight;
      available += weight;
      if (DECISIVE_SIGNALS.indexOf(signal) !== -1) {
        decisiveSeen += 1;
        if (score > 0) decisiveMatched += 1;
      }
      reasons.push({
        signal: signal,
        score: score,
        weight: weight,
        explanation: explain(signal, score, baseline, candidate),
      });
    });

    let overall = available ? weighted / available : 0.0;

    // Sharing no route and no airport means a different market. Schema-level
    // agreement must not talk the score back up, so cap it and say why.
    if (decisiveSeen > 0 && decisiveMatched === 0) {
      overall = Math.min(overall, MISMATCH_CAP);
      reasons.push({
        signal: "decisive_mismatch",
        score: 0.0,
        weight: 0.0,
        explanation:
          "no shared route or airport, so this is treated as a different " +
          "market regardless of schema agreement",
      });
    }

    const confidence = !(available >= LOW_CONFIDENCE_WEIGHT)
      ? "low"
      : overall >= 0.75
        ? "high"
        : overall >= WEAK_MATCH_THRESHOLD
          ? "medium"
          : "low";

    return {
      label: candidate.label,
      score: overall,
      confidence: confidence,
      weak: overall < WEAK_MATCH_THRESHOLD,
      low_confidence: available < LOW_CONFIDENCE_WEIGHT,
      available_weight: available,
      reasons: reasons,
      fingerprint: candidate,
    };
  }

  /** Rank every baseline by similarity to the new message, best first. */
  function rankCandidates(newRoot, baselines, newLabel, deps) {
    const target = fingerprint(newRoot, newLabel || "new", deps);
    const results = Object.keys(baselines).map((label) =>
      scoreMatch(target, fingerprint(baselines[label].root, label, deps))
    );
    results.sort((a, b) => b.score - a.score || (a.label < b.label ? -1 : 1));
    return results;
  }

  /** Return the single nearest-looking baseline, or null if there are none. */
  function findNearest(newRoot, baselines, newLabel, deps) {
    const ranked = rankCandidates(newRoot, baselines, newLabel, deps);
    return ranked.length ? ranked[0] : null;
  }

  /** Readable account of why a baseline scored as it did. */
  function explainMatch(result, top) {
    const lines = [
      `${result.label}: score ${result.score.toFixed(2)} (${result.confidence} confidence)`,
    ];
    const ranked = result.reasons
      .slice()
      .sort((a, b) => b.score * b.weight - a.score * a.weight);
    ranked.slice(0, top || 5).forEach((reason) => {
      lines.push(
        `  ${reason.signal}: ${reason.score.toFixed(2)} x${reason.weight} \u2014 ${reason.explanation}`
      );
    });
    if (result.low_confidence) {
      lines.push("  note: few comparable fields, so treat this as a rough guide");
    }
    return lines.join("\n");
  }

  return {
    WEIGHTS: WEIGHTS,
    WEAK_MATCH_THRESHOLD: WEAK_MATCH_THRESHOLD,
    MISMATCH_CAP: MISMATCH_CAP,
    DECISIVE_SIGNALS: DECISIVE_SIGNALS,
    LOW_CONFIDENCE_WEIGHT: LOW_CONFIDENCE_WEIGHT,
    fingerprint: fingerprint,
    scoreMatch: scoreMatch,
    rankCandidates: rankCandidates,
    findNearest: findNearest,
    explainMatch: explainMatch,
  };
});
