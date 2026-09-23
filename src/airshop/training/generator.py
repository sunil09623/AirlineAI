"""Generate verifiable training data for AirShoppingRQ/RS tool use.

Produces two artifacts:

1. **Trajectories** — full multi-turn tool-calling episodes in OpenAI chat format
   (system / user / assistant tool_call / tool result / assistant answer). Every
   ``airshopping`` tool call in a trajectory is executed against the real local
   engine, so the tool results and the final answer are ground truth, not
   fabrications.

2. **Paired RQ/RS examples** — (AirShoppingRQ XML -> normalized request) and
   (request -> AirShoppingRS XML) pairs for supervised fine-tuning of XML
   understanding.

Each trajectory carries a ``reward`` computed by the validator, so you can filter
to passing episodes or use the score directly for rejection sampling.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from airshop.agent.local import SYSTEM_PROMPT_SUFFIX
from airshop.data.reference import AIRPORTS, CABINS, CARRIERS
from airshop.ndc import builder, engine, validator
from airshop.ndc.models import (
    AirShoppingRequest,
    OriginDest,
    Preferences,
    Traveler,
)
from airshop.store import TransactionStore
from airshop.tools.ndc import (
    AirShoppingQueryAction,
    AirShoppingTool,
    ValidateAction,
    ValidateAirShoppingTool,
)

_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "ndc_reference",
            "description": "Look up valid airport, carrier, cabin and passenger codes.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "airshopping",
            "description": (
                "Run an NDC AirShopping transaction and return ranked flight offers."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "transaction_id": {"type": "string"},
                    "origin": {"type": "string"},
                    "destination": {"type": "string"},
                    "departure_date": {"type": "string"},
                    "travelers": {"type": "object"},
                    "cabin": {"type": ["string", "null"]},
                    "currency": {"type": "string"},
                    "max_connections": {"type": "integer"},
                    "direct_only": {"type": "boolean"},
                    "carriers": {"type": "array", "items": {"type": "string"}},
                    "max_price": {"type": ["number", "null"]},
                },
                "required": ["transaction_id", "origin", "destination", "departure_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_airshopping",
            "description": "Validate a stored AirShopping transaction.",
            "parameters": {
                "type": "object",
                "properties": {"transaction_id": {"type": "string"}},
                "required": ["transaction_id"],
            },
        },
    },
]

_CABIN_WORDS = {
    "Y": "economy",
    "S": "premium economy",
    "C": "business",
    "J": "business",
    "F": "first",
}


@dataclass
class Scenario:
    """A randomly generated shopping scenario with its natural-language phrasing."""

    request: AirShoppingRequest
    user_prompt: str
    intent: str = "search"


def _random_scenario(rng: random.Random, index: int) -> Scenario:
    codes = list(AIRPORTS)
    origin, destination = rng.sample(codes, 2)
    departure = date(2026, 1, 1) + timedelta(days=rng.randint(30, 300))
    adults = rng.choice([1, 1, 1, 2, 3])
    children = rng.choice([0, 0, 0, 1, 2])
    cabin = rng.choice([None, "Y", "Y", "S", "C", "F"])
    direct_only = rng.random() < 0.25
    max_connections = 0 if direct_only else rng.choice([0, 1, 2])
    carriers = rng.sample(list(CARRIERS), 1) if rng.random() < 0.25 else []
    max_price = rng.choice([None, None, 800.0, 1500.0, 3000.0])

    travelers = [Traveler(ptc="ADT", quantity=adults)]
    if children:
        travelers.append(Traveler(ptc="CHD", quantity=children))

    preferences = Preferences(
        currency=rng.choice(["USD", "EUR", "GBP"]),
        max_connections=max_connections,
        direct_only=direct_only,
        carriers=carriers,
        max_price=max_price,
    )

    request = AirShoppingRequest(
        transaction_id=f"TRX-{index:05d}",
        travelers=travelers,
        origin_dests=[
            OriginDest(
                origin=origin,
                destination=destination,
                departure_date=departure,
                cabin=cabin,
            )
        ],
        preferences=preferences,
    )

    pax_bits = []
    if adults:
        pax_bits.append(f"{adults} adult" + ("s" if adults > 1 else ""))
    if children:
        pax_bits.append(f"{children} child" + ("ren" if children > 1 else ""))
    pax_text = " and ".join(pax_bits) if pax_bits else "1 adult"

    parts = [
        f"Find me flights from {origin} to {destination} on {departure.isoformat()}",
        f"for {pax_text}",
    ]
    if cabin:
        parts.append(f"in {_CABIN_WORDS[cabin]}")
    if direct_only:
        parts.append("non-stop only")
    elif max_connections == 0:
        parts.append("non-stop only")
    if carriers:
        parts.append(f"on {CARRIERS[carriers[0]][0]}")
    if max_price:
        parts.append(f"under {max_price:.0f} {preferences.currency}")
    parts.append(f"and quote prices in {preferences.currency}.")
    user_prompt = " ".join(parts)

    return Scenario(request=request, user_prompt=user_prompt)


def _summarize_offers(obs) -> str:
    lines = [f"{obs.offer_count} offer(s) in {obs.currency}:"]
    for offer in obs.offers:
        stops = "non-stop" if offer["connections"] == 0 else f"{offer['connections']} stop(s)"
        lines.append(
            f"- {offer['itinerary_id']}: {offer['carrier']} cabin {offer['cabin']}, "
            f"{offer['price']:.2f} {obs.currency}, {stops}, "
            f"{offer['duration_minutes']} min"
        )
    return "\n".join(lines)


def _final_answer(scenario: Scenario, obs, validation) -> str:
    request = scenario.request
    od = request.origin_dests[0]
    if not obs.offers:
        reason = request.preferences
        hints = []
        if reason.max_price:
            hints.append(f"the {reason.max_price:.0f} {reason.currency} budget")
        if reason.carriers:
            hints.append(f"the carrier restriction ({', '.join(reason.carriers)})")
        if reason.direct_only:
            hints.append("the non-stop-only requirement")
        culprit = hints[0] if hints else "the filters"
        return (
            f"No offers were found for {od.origin} to {od.destination} on "
            f"{od.departure_date.isoformat()}. The most likely cause is {culprit}. "
            "I can retry with it relaxed."
        )

    cheapest = obs.offers[0]
    stops = "non-stop" if cheapest["connections"] == 0 else f"{cheapest['connections']} stop(s)"
    refund = "refundable" if cheapest["refundable"] else "non-refundable"
    lines = [
        f"Found {obs.offer_count} offer(s) for {od.origin} to {od.destination} on "
        f"{od.departure_date.isoformat()}.",
        f"Cheapest is {cheapest['carrier']} {cheapest['itinerary_id']} at "
        f"{cheapest['price']:.2f} {obs.currency}: {stops}, "
        f"{cheapest['duration_minutes']} minutes, {refund} fare.",
    ]
    if len(obs.offers) > 1:
        lines.append(
            f"Other options, cheapest first: "
            + "; ".join(
                f"{o['carrier']} at {o['price']:.2f} {obs.currency}"
                for o in obs.offers[1:]
            )
            + "."
        )
    lines.append(f"Validation: {validation.score:.2f}. Artifacts at {obs.rs_path}.")
    return " ".join(lines)


def build_trajectory(
    scenario: Scenario,
    store: TransactionStore,
) -> dict:
    """Execute one scenario against the real engine and build a chat trajectory."""
    request = scenario.request
    od = request.origin_dests[0]

    shop_tool = AirShoppingTool.create(store=store)[0]
    validate_tool = ValidateAirShoppingTool.create(store=store)[0]

    shop_action = AirShoppingQueryAction(
        transaction_id=request.transaction_id,
        origin=od.origin,
        destination=od.destination,
        departure_date=od.departure_date.isoformat(),
        travelers=request.pax_type_counts(),
        cabin=od.cabin,
        currency=request.preferences.currency,
        max_connections=request.preferences.max_connections,
        direct_only=request.preferences.direct_only,
        carriers=request.preferences.carriers,
        max_price=request.preferences.max_price,
    )
    shop_obs = shop_tool.executor(shop_action)
    validation = validate_tool.executor(
        ValidateAction(transaction_id=request.transaction_id)
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_SUFFIX.strip()},
        {"role": "user", "content": scenario.user_prompt},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call_{request.transaction_id}_1",
                    "type": "function",
                    "function": {
                        "name": "airshopping",
                        "arguments": json.dumps(shop_action.model_dump(mode="json")),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": f"call_{request.transaction_id}_1",
            "name": "airshopping",
            "content": shop_obs.to_llm_content[0].text,
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call_{request.transaction_id}_2",
                    "type": "function",
                    "function": {
                        "name": "validate_airshopping",
                        "arguments": json.dumps(
                            {"transaction_id": request.transaction_id}
                        ),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": f"call_{request.transaction_id}_2",
            "name": "validate_airshopping",
            "content": validation.to_llm_content[0].text,
        },
        {
            "role": "assistant",
            "content": _final_answer(scenario, shop_obs, validation),
        },
    ]

    return {
        "transaction_id": request.transaction_id,
        "messages": messages,
        "tools": _TOOL_SCHEMAS,
        "reward": validation.score,
        "offer_count": shop_obs.offer_count,
        "metadata": {
            "origin": od.origin,
            "destination": od.destination,
            "departure_date": od.departure_date.isoformat(),
            "cabin": od.cabin,
            "currency": request.preferences.currency,
            "direct_only": request.preferences.direct_only,
            "max_price": request.preferences.max_price,
            "carriers": request.preferences.carriers,
        },
    }


def build_paired_examples(scenario: Scenario, store: TransactionStore) -> list[dict]:
    """RQ XML -> normalized request, and request -> RS XML, as SFT pairs."""
    request = scenario.request
    rq_xml = builder.build_air_shopping_rq(request)
    response = engine.shop(request)
    rs_xml = builder.build_air_shopping_rs(response, request)

    parsed = validator.validate_rq(request)
    xml_report = validator.validate_rs_xml(rs_xml)

    return [
        {
            "kind": "rq_to_json",
            "input": rq_xml,
            "output": json.dumps(request.model_dump(mode="json"), indent=2),
            "reward": parsed.score,
        },
        {
            "kind": "request_to_rs_xml",
            "input": json.dumps(request.model_dump(mode="json"), indent=2),
            "output": rs_xml,
            "reward": xml_report.score,
        },
    ]


def generate(
    count: int = 50,
    out_dir: Path | str = "training_data",
    seed: int = 7,
    min_reward: float = 0.0,
) -> dict:
    """Generate ``count`` trajectories plus paired examples on disk.

    Returns a small manifest with counts and the mean reward.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    store = TransactionStore(out / "transactions")

    rng = random.Random(seed)
    traj_lines: list[str] = []
    pair_items: list[dict] = []
    rewards: list[float] = []

    for i in range(1, count + 1):
        scenario = _random_scenario(rng, i)
        trajectory = build_trajectory(scenario, store)
        if trajectory["reward"] < min_reward:
            continue
        traj_lines.append(json.dumps(trajectory))
        rewards.append(trajectory["reward"])
        pair_items.extend(build_paired_examples(scenario, store))

    traj_path = out / "trajectories.jsonl"
    traj_path.write_text("\n".join(traj_lines) + "\n", encoding="utf-8")

    pairs_path = out / "paired_examples.jsonl"
    pairs_path.write_text(
        "\n".join(json.dumps(p) for p in pair_items) + "\n", encoding="utf-8"
    )

    manifest = {
        "trajectories": len(traj_lines),
        "paired_examples": len(pair_items),
        "mean_reward": round(sum(rewards) / len(rewards), 4) if rewards else 0.0,
        "min_reward_filter": min_reward,
        "seed": seed,
        "paths": {
            "trajectories": str(traj_path),
            "paired_examples": str(pairs_path),
            "transactions": str(store.root),
        },
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


__all__ = ["Scenario", "build_paired_examples", "build_trajectory", "generate"]
