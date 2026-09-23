"""Local NDC AirShopping agent.

Wires the custom NDC tools into an OpenHands SDK agent. Nothing in this module
requires network access if the LLM points at a local runtime (e.g. Ollama via
``ollama_base_url``), which is what makes it a genuine local agent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Import-time ordering matters: the SDK configures its telemetry exporter when
# openhands.sdk is first imported, so the environment must be sanitised before
# that happens, not later.
from airshop.offline import assert_local_model, enforce_offline

enforce_offline()

from openhands.sdk import LLM, Agent, Conversation, Tool  # noqa: E402
from openhands.sdk.context.agent_context import AgentContext  # noqa: E402
from pydantic import SecretStr  # noqa: E402

from airshop.store import TransactionStore  # noqa: E402
from airshop.tools.ndc import NDC_TOOL_CLASSES, build_ndc_tools  # noqa: E402,F401

SYSTEM_PROMPT_SUFFIX = """
You are an airline distribution agent for IATA NDC flight shopping, running fully
offline against local files. You never need the internet.

Your NDC tools:

- ndc_reference: look up valid airport, carrier, cabin and passenger codes.
- airshopping: run a full AirShopping transaction; returns ranked offers plus the
  paths of the generated AirShoppingRQ/RS XML and a JSON summary.
- parse_airshopping_rq: parse an AirShoppingRQ XML document into a normalized request.
- validate_airshopping: verify a stored transaction and report a pass/fail score.
- upload_ndc_message: import an NDC XML file the user supplies, under a short name.
- list_ndc_messages: list the messages already uploaded.
- compare_ndc_messages: diff two messages and report what is missing / extra.

Comparison workflow (the most important job):
When the user gives you a baseline AirShoppingRQ and AirShoppingRS, then a second
AirShoppingRS, do this:
1. upload_ndc_message each of the three files under clear names
   (e.g. baseline_rq, baseline_rs, second_rs).
2. compare_ndc_messages with baseline=baseline_rs, new=second_rs, and
   baseline_rq=baseline_rq for context.
3. Report, in plain language:
   - what is MISSING from the second response (entities or node paths present in
     the baseline but absent now), and
   - what is EXTRA in the second response (new offers, new nodes, new values).
   Also mention any entity that kept its ID but changed content, and what changed.
4. If the second RS lost offers that the baseline had, say explicitly which
   OfferIDs disappeared. If prices changed, name the offer and both amounts.

Working rules:
1. Never invent airport or carrier codes. Only use codes returned by ndc_reference.
2. Do not set the `carriers` filter unless the user names a specific airline.
3. Use a distinct transaction_id per search so artifacts do not collide.
4. After any airshopping call, call validate_airshopping on that transaction.
5. Report offers cheapest first, with carrier, cabin, total price, stops, duration,
   and whether the fare is refundable.
6. Give the user artifact paths when they may want the raw NDC XML.
7. When comparing, be precise about direction: state clearly whether something is
   missing from the NEW message or newly added in it. Do not claim a difference you
   did not observe in the tool output.
"""


@dataclass
class AgentConfig:
    """Configuration for building the local agent."""

    model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "qwen2.5:7b-instruct"))
    api_key: str | None = field(default_factory=lambda: os.getenv("LLM_API_KEY"))
    base_url: str | None = field(default_factory=lambda: os.getenv("LLM_BASE_URL"))
    ollama_base_url: str | None = field(
        default_factory=lambda: os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    )
    workspace: str = field(default_factory=lambda: os.getcwd())
    runs_dir: str = "runs"
    temperature: float = field(
        default_factory=lambda: float(os.getenv("LLM_TEMPERATURE", "0.2"))
    )
    reasoning_effort: str | None = field(
        default_factory=lambda: os.getenv("LLM_REASONING_EFFORT")
    )
    extended_thinking_budget: int | None = field(
        default_factory=lambda: (
            int(v) if (v := os.getenv("LLM_EXTENDED_THINKING_BUDGET")) else None
        )
    )
    require_offline: bool = field(
        default_factory=lambda: os.getenv("AIRSHOP_OFFLINE", "1").lower()
        not in {"0", "false", "no"}
    )
    include_terminal: bool = False

    @property
    def store(self) -> TransactionStore:
        return TransactionStore(Path(self.workspace) / self.runs_dir)


def _build_llm(config: AgentConfig) -> LLM:
    """Create the LLM handle.

    A local Ollama runtime is the default: with no ``LLM_API_KEY`` and a
    model name like ``qwen2.5:7b-instruct``, litellm routes to Ollama.
    """
    is_local = not config.api_key or config.model.startswith(("ollama/", "ollama_chat/"))
    model = config.model
    if is_local and "/" not in model:
        model = f"ollama_chat/{model}"

    if config.require_offline:
        assert_local_model(model, config.base_url, config.ollama_base_url)

    kwargs: dict = {
        "model": model,
        "temperature": config.temperature,
        "usage_id": "airshop",
        # Local models such as qwen2.5 reject Ollama's `think` parameter, which
        # litellm derives from reasoning_effort. Disable it for local runtimes.
        "reasoning_effort": None if is_local else config.reasoning_effort,
        "extended_thinking_budget": None if is_local else config.extended_thinking_budget,
    }
    if config.api_key:
        kwargs["api_key"] = SecretStr(config.api_key)
    if config.base_url:
        kwargs["base_url"] = config.base_url
    if is_local and config.ollama_base_url:
        kwargs["ollama_base_url"] = config.ollama_base_url
    return LLM(**kwargs)


def build_agent(config: AgentConfig | None = None) -> Agent:
    """Build the local AirShopping agent (LLM + NDC tools + system prompt)."""
    config = config or AgentConfig()
    if config.require_offline:
        enforce_offline()
    llm = _build_llm(config)

    tools = [Tool(name=name) for name in NDC_TOOL_CLASSES]

    context = AgentContext(
        system_message_suffix=SYSTEM_PROMPT_SUFFIX,
        load_memory=False,
        load_user_skills=False,
        load_public_skills=False,
        load_project_skills=False,
    )

    return Agent(
        llm=llm,
        tools=tools,
        agent_context=context,
        include_default_tools=["FinishTool", "ThinkTool"],
    )


def build_conversation(
    config: AgentConfig | None = None,
    callbacks=None,
) -> Conversation:
    """Build an agent plus a conversation bound to the configured workspace."""
    config = config or AgentConfig()
    workspace = Path(config.workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    config.store.root.mkdir(parents=True, exist_ok=True)

    agent = build_agent(config)
    return Conversation(
        agent=agent,
        workspace=str(workspace),
        callbacks=callbacks or [],
    )


__all__ = [
    "AgentConfig",
    "SYSTEM_PROMPT_SUFFIX",
    "build_agent",
    "build_conversation",
    "build_ndc_tools",
]
