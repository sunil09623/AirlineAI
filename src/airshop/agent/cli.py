"""Command-line entry point for the local NDC AirShopping agent.

Modes:
  airshop "compare baseline_rs and second_rs and tell me what changed"
  airshop --interactive
  airshop --upload path/to/rs.xml --as-name baseline_rs
  airshop --compare baseline_rs second_rs [--with-rq baseline_rq]
  airshop --offline-call           # exercise the deterministic layer, no LLM

The ``--compare`` and ``--offline-call`` paths need no LLM at all, so they work on
a machine with no model server and no network.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

# Offline must be enforced before importing the SDK: the SDK configures its
# telemetry exporter at import time, so clearing the environment afterwards is
# too late to stop it from being initialised.
from airshop.offline import enforce_offline

enforce_offline()

from airshop.agent.local import (  # noqa: E402
    AgentConfig,
    build_conversation,
    collect_agent_text,
)
from airshop.ndc.catalog import MessageCatalog, review_update  # noqa: E402
from airshop.store import TransactionStore  # noqa: E402
from airshop.tools.ndc import (  # noqa: E402
    AirShoppingQueryAction,
    AirShoppingTool,
    NdcReferenceTool,
    ReferenceAction,
    ValidateAction,
    ValidateAirShoppingTool,
)


def run_once(prompt: str, config: AgentConfig, stream: bool = True) -> str:
    """Run a single prompt through the agent and return its final text."""
    collected: list[str] = []
    conversation = build_conversation(
        config, callbacks=[collect_agent_text(collected)]
    )
    conversation.send_message(prompt)
    conversation.run()
    return collected[-1] if collected else ""


def run_interactive(config: AgentConfig) -> None:
    """Simple REPL over a persistent conversation."""
    print("Local NDC AirShopping agent. Type 'exit' or Ctrl-D to quit.\n")
    conversation = build_conversation(config)
    while True:
        try:
            prompt = input("airshop> ").strip()
        except EOFError:
            print()
            break
        if prompt.lower() in {"exit", "quit"}:
            break
        if not prompt:
            continue
        conversation.send_message(prompt)
        conversation.run()
        print()


def run_upload(args: argparse.Namespace) -> int:
    """Register an NDC XML file in the local catalog."""
    catalog = MessageCatalog(Path(args.workspace) / args.messages_dir)
    ref = catalog.register(args.as_name or Path(args.upload).stem, args.upload)
    print(f"Registered '{ref.name}' ({ref.kind}, root <{ref.root}>) at {ref.path}")
    print("Catalog: " + ", ".join(m.name for m in catalog.list()))
    return 0


def run_list(args: argparse.Namespace) -> int:
    """List every NDC message in the local catalog."""
    catalog = MessageCatalog(Path(args.workspace) / args.messages_dir)
    messages = catalog.list()
    if not messages:
        print("No NDC messages uploaded yet.")
        return 0
    for m in messages:
        print(f"- {m.name} ({m.kind}, root <{m.root}>) at {m.path}")
    return 0


def run_compare(args: argparse.Namespace) -> int:
    """Diff two NDC messages with no LLM involved."""
    catalog = MessageCatalog(Path(args.workspace) / args.messages_dir)
    baseline_path = catalog.resolve(args.compare[0])
    new_path = catalog.resolve(args.compare[1])
    rq_path = catalog.resolve(args.with_rq) if args.with_rq else None

    review = review_update(baseline_path, new_path, rq_path)
    print(review.render())

    report_path = Path(args.workspace) / "reports" / (
        f"diff_{baseline_path.stem}__vs__{new_path.stem}.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    payload = review.report.to_dict()
    payload["baseline_inventory"] = review.baseline_summary["entity_counts"]
    payload["new_inventory"] = review.new_summary["entity_counts"]
    report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nMachine-readable report: {report_path}")
    return 0


def run_offline(args: argparse.Namespace) -> int:
    """Exercise the deterministic tools with no LLM; used for smoke tests."""
    store = TransactionStore(Path(args.workspace) / args.runs_dir)

    reference = NdcReferenceTool.create()[0].executor(ReferenceAction(query="all"))
    print("[ndc_reference]")
    print(reference.to_llm_content[0].text.splitlines()[0])

    obs = AirShoppingTool.create(store=store)[0].executor(
        AirShoppingQueryAction(
            transaction_id=args.transaction_id,
            origin=args.origin,
            destination=args.destination,
            departure_date=args.departure_date,
            travelers={"ADT": args.adults},
            cabin=args.cabin,
            direct_only=args.direct_only,
        )
    )
    print("\n[airshopping]")
    print(obs.to_llm_content[0].text)

    validation = ValidateAirShoppingTool.create(store=store)[0].executor(
        ValidateAction(transaction_id=args.transaction_id)
    )
    print("\n[validate_airshopping]")
    print(validation.to_llm_content[0].text)

    if args.json:
        print("\n[json]")
        print(json.dumps(obs.model_dump(mode="json"), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="airshop",
        description="Local agent for IATA NDC AirShoppingRQ/RS flight shopping.",
    )
    parser.add_argument("prompt", nargs="?", help="Natural-language request to the agent.")
    parser.add_argument("-i", "--interactive", action="store_true", help="Start a REPL.")
    parser.add_argument("--model", default=None, help="LLM model name (litellm format).")
    parser.add_argument("--api-key", default=None, help="LLM API key for hosted providers.")
    parser.add_argument("--base-url", default=None, help="LLM base URL.")
    parser.add_argument(
        "--ollama-base-url",
        default=None,
        help="Ollama base URL for the local runtime (default http://localhost:11434).",
    )
    parser.add_argument(
        "--workspace", default=None, help="Workspace directory (default: current)."
    )
    parser.add_argument("--runs-dir", default="runs", help="Subdirectory for artifacts.")
    parser.add_argument(
        "--messages-dir",
        default="ndc_messages",
        help="Subdirectory holding uploaded NDC messages.",
    )
    parser.add_argument(
        "--upload", default=None, help="Register an NDC XML file in the catalog."
    )
    parser.add_argument(
        "--as-name", default=None, help="Name to register the uploaded file under."
    )
    parser.add_argument(
        "--list", action="store_true", help="List uploaded NDC messages."
    )
    parser.add_argument(
        "--compare",
        nargs=2,
        metavar=("BASELINE", "NEW"),
        default=None,
        help="Diff two catalog messages (names or paths), no LLM needed.",
    )
    parser.add_argument(
        "--with-rq",
        default=None,
        help="Optional baseline AirShoppingRQ name for comparison context.",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Start the offline browser UI for uploading and comparing NDC files.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host for the UI.")
    parser.add_argument("--port", type=int, default=12000, help="Bind port for the UI.")
    parser.add_argument(
        "--offline-call",
        action="store_true",
        help="Call the NDC tools directly without an LLM.",
    )
    parser.add_argument("--transaction-id", default="TRX-CLI")
    parser.add_argument("--origin", default="LHR")
    parser.add_argument("--destination", default="JFK")
    parser.add_argument("--departure-date", default="2026-12-01")
    parser.add_argument("--adults", type=int, default=1)
    parser.add_argument("--cabin", default=None)
    parser.add_argument("--direct-only", action="store_true")
    parser.add_argument("--json", action="store_true", help="Emit JSON in offline mode.")
    return parser


def _config_from_args(args: argparse.Namespace) -> AgentConfig:
    config = AgentConfig(workspace=args.workspace or str(Path.cwd()), runs_dir=args.runs_dir)
    if args.model:
        config.model = args.model
    if args.api_key:
        config.api_key = args.api_key
    if args.base_url:
        config.base_url = args.base_url
    if args.ollama_base_url:
        config.ollama_base_url = args.ollama_base_url
    return config


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Offline is the default; enforce it before anything can reach out.
    enforce_offline()

    if args.upload:
        return run_upload(args)
    if args.list:
        return run_list(args)
    if args.compare:
        return run_compare(args)
    if args.serve:
        try:
            from airshop.web.server import run as run_web
        except ModuleNotFoundError as exc:
            missing = getattr(exc, "name", "a web dependency")
            print(
                f"The web UI needs extra packages ({missing} not found).\n"
                'Install them with either:\n'
                '  pip install -e ".[serve]"\n'
                '  pip install -e ".[dev]"   # includes serve plus pytest\n'
                "The CLI diff mode (--compare) works without them.",
                file=sys.stderr,
            )
            return 1

        workspace_dir = Path(args.workspace or Path.cwd())
        print(
            f"Offline NDC reviewer at http://{args.host}:{args.port} "
            f"(workspace: {workspace_dir})"
        )
        run_web(host=args.host, port=args.port, workspace_dir=str(workspace_dir))
        return 0
    if args.offline_call:
        return run_offline(args)

    config = _config_from_args(args)

    if args.interactive:
        run_interactive(config)
        return 0

    if not args.prompt:
        build_parser().print_help()
        return 2

    answer = run_once(args.prompt, config)
    print(answer or "(the agent produced no text output; check logs above)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
