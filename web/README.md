# Airline-CMP

An offline tool for comparing IATA NDC **AirShoppingRS** documents across releases,
with **no install at all** — no Python, no pip, no server, no network. Everything
runs in the browser tab and your files never leave the machine.

This exists because the Python version in `src/` needs a Python 3.12–3.14
interpreter, which is not always installable on a locked-down machine.

## Run it

Download these seven files into one folder and open `index.html`:

- `index.html`
- `ndc-diff.js`
- `ndc-report.js`
- `ndc-trip.js`
- `ndc-chat.js`
- `ndc-similarity.js`
- `ndc-store.js`

Double-clicking `index.html` works — the scripts load as classic scripts
(UMD globals rather than ES modules) precisely so `file://` is supported.

## Baseline library

Keep a **library of baselines** from previous releases rather than one at a time.
Add as many responses as you like; they persist in the browser until you remove
them.

When you load a new response, the tool compares it against the **nearest-looking
baseline** automatically. That matters because comparing against the wrong
baseline produces a meaningless report. You can override the choice from a
dropdown, and the panel shows *why* the match was made — route, dates,
passengers, cabin and so on, each with its contribution.

The matcher is honest about uncertainty: a weak match is labelled as such, and a
response sharing no route or airport with any baseline is capped low rather than
being talked up by schema-level agreement. It also distinguishes what was
*requested* (an `OriginDest` pair) from how it was *answered* (a segment hop via
some hub), so the same market reached through different hubs still matches.

Storage uses IndexedDB, because real responses are large enough that
`localStorage`'s ~5MB ceiling is reached after a couple of files.

## What it answers

Upload a baseline `AirShoppingRS` and the new one, plus optionally the
`AirShoppingRQ`, and press **Compare responses**. You get:

- **a plain-English summary** of what is missing and what is extra
- **trip shape** — whether the request moved between one-way, round-trip and
  multi-city, and whether individual legs were added or dropped
- **passenger mix** — whether the adult/child/infant counts changed
- **cabin and currency** changes
- **per-entity changes** — an `Offer`/`Segment`/`Fare` that disappeared, or one
  that kept its ID but changed content
- **node paths** whose count changed
- **value changes** at the same path
- **session metadata** (timestamps, transaction IDs) reported separately, because
  those are regenerated on every response and are not content changes
- a copyable text report and a downloadable JSON report

The **baseline is retained in the browser** until you press *Clear all*, so a new
release response can be dropped in without reloading the reference.

## Chat

The Chat tab answers questions about the comparison you just ran:

```
summary                     what is missing?         what is extra?
did the trip type change?   did pax change?          how many offers now?
which entities changed?     what values changed?     are they identical?
```

It is deliberately **rule-based rather than model-backed**. Since the whole point
of this version is zero install and zero download, there is no LLM to call — and
answering from the computed report means the replies can never contradict the
Review tab.

## Why you can trust it

The JavaScript is a direct port of the Python implementation
(`ndc/diff.py`, `ndc/catalog.py`, `ndc/trip.py`) and is **checked against it**
rather than assumed to match:

```bash
# Node 18+; the DOM shim is only needed by the test harness
npm install --no-save xmldom
node web/verify.mjs          # 55 checks
```

Plus two in-browser checks against the real browser `DOMParser`:

```
web/selftest.html?v=1            # 27 checks — diff, trip/pax, chat
web/selftest-library.html?v=1    # 17 checks — baseline library, nearest match
```

Both need `http://` (browsers block `fetch` and IndexedDB from `file://`).

The Node harness compares against `web/expected/`, which the Python code
generated — including a similarity-fingerprint reference, so the two matchers
cannot drift apart.

Porting and real-world use surfaced five bugs that the checks caught rather than
the eye:

1. Using DOM `textContent` instead of lxml's `.text` attributes an entire
   subtree's text to its parent, producing spurious value diffs.
2. Relying on `.children`, which some DOM implementations do not provide.
3. A different tie-break in the narrative's top-5 path list.
4. **Churning reference IDs.** Carriers regenerate identifiers per response —
   either the trailing token (`Xbga0600210b3be03` -> `Xbga06000a0b3c8d7`) or the
   leading batch UUID (`XA67C09C8-37C4-4844-82B2-1` -> `XAA4633E4-E3C1-43D3-9153-1`).
   Both made every entity look removed-and-added; identifiers are now normalised
   so pure churn reports as *no difference* while a genuine addition stands out.
5. **Hops mistaken for requested routes.** Folding segment hops into the route set
   made two responses for the same market look unrelated when they used different
   hubs, and inflated the similarity of two responses that merely shared one.

## Scoring, and how to read it

Scores run 0–1. Each signal is compared, weighted, and the weights renormalise
over only the signals both sides actually carry — so a response with no
`OriginDest` list is not penalised for it.

Signals describing what was *asked for* (route, dates, airports, trip type,
passengers, cabin, currency) carry more weight than signals describing how it was
*answered* (node-path shape, entity mix, offer count), because the latter are
near-identical across any two responses from the same schema and therefore barely
discriminate.

Two guards keep the result honest:

- **Decisive mismatch.** Sharing no route and no airport means a different market,
  so the score is capped at `0.25` however similar the schemas are.
- **Low confidence.** If too little weight was comparable, the match is labelled
  low-confidence rather than presented as a firm answer.

Rough guide: **above 0.75** high confidence, **0.45–0.75** workable, **below
0.45** treat with suspicion — the library may not cover this request.

## Notes and limits

- **`file://` vs `http://`**: diffing and chat work either way. The baseline
  library needs `http://`, because browsers restrict IndexedDB on `file://`; the
  app says so rather than failing silently. The self-tests also need `http://`.
- **Storage**: baselines are kept in IndexedDB and persist until you remove them
  or press *Clear library*. If the browser blocks storage (private mode), the app
  reports that the library will not persist and carries on working in-memory.
- **Parsing** uses the browser's own XML parser, so malformed XML is reported with
  a readable message rather than silently producing an empty diff. A file that
  fails to parse is rejected at upload time and never enters the library.
- **Entity naming** relies on `ENTITY_KEYS` (`ndc-diff.js`). It covers twenty
  common NDC collections (`Offer`, `OfferItem`, `FlightSegment`, `Fare`,
  `FareGroup`, `BaggageAllowance`, `Pax`, `OriginDest`, `Order`, `PaxJourney`,
  `PriceClass`, `Service`, `Penalty`, and more), accepting the identifier either
  as a child element (`Offer/OfferID`) or an attribute
  (`FareGroup/@ListKey`). If your carrier uses other ID-bearing elements, add them
  there — one line each.
- **No LLM chat**: by design. See above.

## A caveat on generated fixtures

The local Python engine only shops the first origin/destination pair, so an
`AirShoppingRS` *it* generates for a round trip carries one leg. Trip
classification from a response reflects whatever that response actually contains;
against real carrier responses, which include every requested pair, it reflects
the full trip.
