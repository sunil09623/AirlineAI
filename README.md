# Airline-CMP — offline local agent

A local, completely offline agent for IATA NDC AirShopping messages. Its main job
is to take a baseline `AirShoppingRQ` + `AirShoppingRS` and a newly uploaded second
`AirShoppingRS`, and tell you exactly **what is missing** and **what is extra**.

Works with no internet. The deterministic comparison path needs no LLM at all; the
optional chat layer runs against a local Ollama model.

> **No Python available?** There is also a browser-only build under `web/` that
> needs no install, no server and no network at all — download five files, open
> `index.html`. See [`web/README.md`](web/README.md).

## What it does

| Capability | Needs a model? | Needs network? |
|---|---|---|
| Upload and diff NDC XML in the browser | no | no |
| `airshop --compare a b` CLI diff | no | no |
| Generate AirShoppingRQ/RS locally | no | no |
| Detect trip-type and passenger changes | no | no |
| Match a new response against a baseline library | no | no |
| Chat explanation of a diff | yes (local Ollama) | no |

A **baseline library** can hold many previous responses. Each new release is
matched against the nearest-looking baseline automatically, with the reasoning
shown, so a review is never silently run against an unrelated baseline.

The diff also distinguishes **session metadata** (timestamps, transaction IDs)
from content: those fields are regenerated on every response, so they are reported
separately rather than as changes. Churning reference IDs are normalised to their
stable prefix, so a response that merely reissued its tokens reports as identical
while a genuine addition still stands out.

## Install

Requires **Python 3.12, 3.13 or 3.14**. Python 3.15 is not supported yet: PyYAML
(pulled in transitively through `openhands-sdk` → `python-frontmatter`) publishes
no cp315 wheels, so `pip install` tries to build it from source and fails inside
Cython. If you are on 3.15, install a 3.13 interpreter alongside it and use that.

Windows: `py -3.13 -m venv .venv` picks a specific version. Check what you have
with `py -0p`.

```bash
git clone https://github.com/sunil09623/AirlineAI.git
cd AirlineAI

# create the environment (use an explicit 3.12-3.14 interpreter on Windows)
python3 -m venv .venv

# activate
source .venv/bin/activate        # Linux / macOS
# .venv\Scripts\Activate.ps1     # Windows PowerShell
# .venv\Scripts\activate.bat     # Windows cmd

pip install -e ".[dev]"
```

Install options:

| Command | What you get |
|---|---|
| `pip install -e ".[dev]"` | Everything: diff engine, CLI, web UI, tests |
| `pip install -e ".[serve]"` | Diff engine, CLI, web UI (no test deps) |
| `pip install -e .` | Diff engine + CLI only (no web UI) |

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# Browser UI: upload files and see the diff
.venv/bin/airshop --serve --port 12000
# then open http://127.0.0.1:12000
```

Upload three files — the optional `AirShoppingRQ`, the baseline `AirShoppingRS`, and
the new `AirShoppingRS` — and press **Compare responses**. You get:

- a plain-English summary (offers missing, prices changed, nodes added/removed),
- per-entity changes (an `Offer`/`Segment`/`Fare` that disappeared or changed),
- node-path counts that went missing or appeared,
- value-level changes at the same path,
- a machine-readable JSON report.

## CLI

```bash
# Diff two messages, no model needed
airshop --upload baseline.xml --as-name baseline_rs
airshop --upload second.xml   --as-name second_rs
airshop --compare baseline_rs second_rs --with-rq baseline_rq

# Exercise the shopping engine with no model
airshop --offline-call --origin LHR --destination JFK --cabin C

# Chat with the local agent
airshop --interactive
```

## How the diff works

Two complementary passes (`src/airshop/ndc/diff.py`):

1. **Structural pass** — each document is flattened into a multiset of
   `(tag-path, value)` records and the multisets are diffed. It is
   order-independent, so a response that merely reorders elements reports no false
   differences.
2. **Entity pass** — NDC's ID-bearing collections (`Offer`/`OfferID`,
   `FlightSegment`/`SegmentID`, `Fare`/`FareCode`, `Pax`/`PaxID`,
   `OriginDest`/`OriginDestID`) are matched across both documents by ID. This is what
   separates "offer IT3 was dropped" from "IT3's price changed", which a flat diff
   blurs.

The result is a `DiffReport` with `missing_nodes`, `extra_nodes`, `value_diffs`,
`entities_removed`, `entities_added` and `entities_modified`.

## Going fully offline with a local model

```bash
# Install Ollama without root, into a local directory
mkdir -p ~/ollama && cd ~/ollama
curl -L -o ollama.tar.zst \
  https://github.com/ollama/ollama/releases/latest/download/ollama-linux-amd64.tar.zst
python -c "import zstandard,tarfile; \
  f=open('ollama.tar.zst','rb'); r=zstandard.ZstdDecompressor().stream_reader(f); \
  tarfile.open(fileobj=r, mode='r|').extractall('.')"

OLLAMA_MODELS=~/ollama/models ./bin/ollama serve &
OLLAMA_MODELS=~/ollama/models ./bin/ollama pull qwen2.5:3b-instruct
```

The agent defaults to offline (`AIRSHOP_OFFLINE=1`). It strips telemetry environment
variables and refuses to run against a non-local model, so it cannot silently reach
the internet. See `src/airshop/offline.py`.

Environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `LLM_MODEL` | `qwen2.5:3b-instruct` | Ollama model name |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama endpoint |
| `LLM_TEMPERATURE` | `0.2` | Sampling temperature |
| `AIRSHOP_OFFLINE` | `1` | Set to `0` to allow non-local models |

## Layout

```
src/airshop/
  ndc/          models, XML parser + builder, shopping engine, validator, diff,
                catalog, trip (trip-shape and passenger comparison)
  tools/        OpenHands SDK custom tools (airshopping, upload, compare, ...)
  agent/        local agent builder + CLI
  web/          FastAPI app + single-page UI  (web/ also holds the browser-only build)
  training/     verifiable training-data generator
  offline.py    no-egress enforcement
tests/          63 tests covering the pipeline, diff, trip/pax and web API
web/            browser-only build: no install, no server, no network
```

## Agent tools

The agent exposes seven tools: `ndc_reference`, `airshopping`,
`parse_airshopping_rq`, `validate_airshopping`, `upload_ndc_message`,
`list_ndc_messages`, `compare_ndc_messages`.

## Training data generation

Generates verifiable tool-use trajectories and NDC request/response pairs, with a
reward per example from the validator:

```bash
python -c "from airshop.training.generator import generate; print(generate(count=200))"
```

Writes `trajectories.jsonl` (OpenAI chat format, every tool result executed against
the real engine) and `paired_examples.jsonl` (RQ XML ↔ normalized request, request ↔
RS XML).

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```
