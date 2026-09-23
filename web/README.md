# Browser-only NDC AirShopping Reviewer

A single-page web app that diffs IATA NDC AirShopping messages **with no install
at all** — no Python, no pip, no server, no network. Everything runs in the
browser tab and files never leave your machine.

This exists because the Python version in `src/` needs a Python 3.12–3.14
interpreter, which is not always installable on a locked-down machine.

## Use it

Download these three files into one folder and open `index.html`:

- `index.html`
- `ndc-diff.js`
- `ndc-report.js`

Then either drag your files onto the three slots, or press **Load sample files**
to try it with the bundled examples. Double-clicking `index.html` works — the
scripts load as classic scripts precisely so `file://` is supported.

## What it reports

Same output as the Python implementation:

- a plain-English summary of what is missing and what is extra,
- per-entity changes (an `Offer`/`Segment`/`Fare` that disappeared, or that kept
  its ID but changed content),
- node paths whose count changed,
- value-level changes at the same path,
- a copyable text report and a downloadable JSON report.

## Why you can trust it

The JavaScript is a direct port of `src/airshop/ndc/diff.py`, and it is checked
against the Python implementation rather than assumed to match:

```bash
# Node 18+; needs the DOM shim used only by the test harness
npm install --no-save xmldom
node web/verify.mjs
```

That compares the JS output against `web/expected/diff_baseline_vs_v2.json`,
which the Python code generated. It caught three real bugs during development:
using DOM `textContent` instead of lxml's `.text` (which attributes a whole
subtree's text to its parent), relying on `.children` (absent in some DOM
implementations), and a different tie-break in the narrative's top-5 path list.

There is also an in-browser check that runs against the real browser `DOMParser`
rather than a shim:

```
web/selftest.html      # serve over http:// and open it
```

## Notes and limits

- **`file://` vs `http://`**: diffing works either way. Only the "Load sample
  files" button needs `http://`, because browsers block `fetch` of local files.
- **Parsing uses the browser's own XML parser**, so malformed XML is reported
  with a readable message rather than silently producing an empty diff.
- **Entity naming** relies on the tag list in `ENTITY_KEYS` (`ndc-diff.js`). It
  covers ten common NDC collections (`Offer`, `OfferItem`, `FlightSegment`,
  `Fare`, `Pax`, `OriginDest`, `Order`, `PaxJourney`, `PriceClass`,
  `BaggageAllowance`). If your carrier's responses use other ID-bearing elements,
  add them there — one line each — to get precise entity naming instead of
  path-level reporting only.
- **No chat.** The Python version's optional chat needs a local LLM. This
  browser version deliberately has none, which is what makes it dependency-free.
