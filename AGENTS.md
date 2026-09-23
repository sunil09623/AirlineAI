# AGENTS.md

Repository-specific guidance for working in this codebase.

## What this project is

An offline local agent for IATA NDC AirShopping messages. Its primary job is to
compare AirShopping responses: given a baseline `AirShoppingRQ` + `AirShoppingRS`
and a newly uploaded second `AirShoppingRS`, report what is **missing** and what is
**extra**.

## Invariants to preserve

1. **Offline is a hard requirement.** `enforce_offline()` must run *before*
   `openhands.sdk` is imported. The SDK configures its telemetry exporter at import
   time, so clearing the environment afterwards is too late and the exporter still
   initialises (you will see `lmnr.opentelemetry_lib` lines in the CLI output). This
   is why `agent/local.py` and `agent/cli.py` call `enforce_offline()` between
   imports with `# noqa: E402`.

2. **The diff needs no LLM.** `ndc/diff.py` and `ndc/catalog.py` are pure functions
   over local files. Never introduce a network or model dependency into the
   deterministic review path — the browser Review tab must work with no model
   server running.

3. **The shopping engine is deterministic.** `ndc/engine.py` seeds its RNG from the
   request content. Do not introduce unseeded randomness; reproducible output is what
   makes generated training data verifiable.

4. **Diff direction matters.** `diff_xml(base, new)` reports `missing_nodes` /
   `entities_removed` relative to `base`. Keep that direction consistent in the
   report, the narrative, the web UI and the tool descriptions — callers rely on
   "missing from the new message" meaning exactly that.

## Layout

```
src/airshop/
  ndc/      models, parser, builder, engine, validator, diff, catalog, xmlutil
  tools/    OpenHands SDK custom tools (registered names in NDC_TOOL_CLASSES)
  agent/    local agent builder + CLI
  web/      FastAPI app (server.py) + single-page UI (static/index.html)
  training/ verifiable trajectory / paired-example generator
  offline.py
```

## Conventions

- NDC XML parsing must stay namespace-agnostic. Use the helpers in
  `ndc/xmlutil.py`; do not assume an `iata:` prefix or a default namespace.
- lxml elements must not be truth-tested (`if element:` is wrong because an
  element with no children is falsy). Use `is not None`, or `xmlutil.first`.
- Tool names are set explicitly via `name: ClassVar[str]` so they match the keys in
  `NDC_TOOL_CLASSES`; do not rely on the class-name derivation.
- `Observation` subclasses must not define a field named `kind` — it collides with
  the base class.

## Commands

```bash
.venv/bin/python -m pytest tests/ -q                 # 40 tests
.venv/bin/python -m airshop.agent.cli --serve --port 12000
.venv/bin/python -m airshop.agent.cli --compare a.xml b.xml
.venv/bin/python scripts/offline_check.py            # proves no egress
```

Local model runtime, if needed (no root required):

```bash
OLLAMA_MODELS=~/ollama/models ~/ollama/bin/ollama serve &
OLLAMA_MODELS=~/ollama/models ~/ollama/bin/ollama pull qwen2.5:3b-instruct
```

## Known issue: GitHub push

Pushing to `sunil09623/AirlineAI` fails with HTTP 403 both over git HTTPS and via
the REST API (`Resource not accessible by integration`), despite the API reporting
`push=True`. The token authenticates as the repo owner but lacks `Contents: write`.
The initial commit `e4fdc28` exists locally on `main`; it needs a token with write
access, or the remote URL updated with one.
