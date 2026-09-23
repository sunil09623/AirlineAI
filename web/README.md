# Airline-CMP

An offline tool for comparing IATA NDC **AirShoppingRS** documents across releases,
with **no install at all** — no Python, no pip, no server, no network. Everything
runs in the browser tab and your files never leave the machine.

This exists because the Python version in `src/` needs a Python 3.12–3.14
interpreter, which is not always installable on a locked-down machine.

## Run it

Download these five files into one folder and open `index.html`:

- `index.html`
- `ndc-diff.js`
- `ndc-report.js`
- `ndc-trip.js`
- `ndc-chat.js`

Double-clicking `index.html` works — the scripts load as classic scripts
(UMD globals rather than ES modules) precisely so `file://` is supported.

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
node web/verify.mjs          # 42 checks
```

Plus an in-browser check against the real browser `DOMParser`:

```
web/selftest.html?v=1        # serve over http:// and open it — 27 checks
```

The Node harness compares against `web/expected/diff_baseline_vs_v2.json`, which
the Python code generated.

Porting and real-world use surfaced four bugs that the checks caught rather than
the eye:

1. Using DOM `textContent` instead of lxml's `.text` attributes an entire
   subtree's text to its parent, producing spurious value diffs.
2. Relying on `.children`, which some DOM implementations do not provide.
3. A different tie-break in the narrative's top-5 path list.
4. **Churning reference IDs.** Carriers regenerate the trailing token of every
   identifier on each response (e.g. `Xbga0600210b3be03` -> `Xbga06000a0b3c8d7`),
   which made every entity look removed-and-added. IDs are now normalised to
   their stable prefix, so pure churn reports as *no difference* while a genuine
   addition still stands out.

## Notes and limits

- **`file://` vs `http://`**: diffing, chat and persistence work either way. Only
  the self-test needs `http://`, since browsers block `fetch` of local files.
- **Storage**: the baseline is kept in `localStorage`. If the browser blocks it
  (private mode, some `file://` configurations) the app says so and continues
  without retention.
- **Parsing** uses the browser's own XML parser, so malformed XML is reported with
  a readable message rather than silently producing an empty diff.
- **Entity naming** relies on `ENTITY_KEYS` (`ndc-diff.js`). It covers twenty
  common NDC collections (`Offer`, `OfferItem`, `FlightSegment`, `Fare`,
  `FareGroup`, `BaggageAllowance`, `Pax`, `OriginDest`, `Order`, `PaxJourney`,
  `PriceClass`, `Service`, `Penalty`, and more), accepting the identifier either
  as a child element (`Offer/OfferID`) or an attribute
  (`FareGroup/@ListKey`). If your carrier uses other ID-bearing elements, add them
  there — one line each.
- **No LLM chat**: by design. See above.
