"""
Anthropic API cost calculation and api_cost_events Firestore writes.
Import and call write_cost_event() after every anthropic.messages.create() call.
"""
from google.cloud import firestore as _firestore

ANTHROPIC_PRICING = {
    "claude-sonnet-4-6":         {"input": 3.00,  "output": 15.00},
    "claude-haiku-4-5-20251001": {"input": 0.80,  "output": 4.00},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},
}


def calculate_anthropic_cost(model: str, usage) -> float:
    pricing = ANTHROPIC_PRICING.get(model, ANTHROPIC_PRICING["claude-sonnet-4-6"])
    return round(
        (usage.input_tokens / 1_000_000) * pricing["input"]
        + (usage.output_tokens / 1_000_000) * pricing["output"],
        6,
    )


def write_cost_event(db, model: str, usage, call_type: str) -> None:
    """
    Write one api_cost_events doc. Silently no-ops on any error so cost
    tracking never breaks production paths.

    Args:
        db:        Firestore client (from bluesky.shared.firestore_client)
        model:     model string from response.model
        usage:     response.usage object (has .input_tokens, .output_tokens)
        call_type: one of reply_generation | intent_classification |
                   dm_generation | comment_generation | query_bar |
                   insights | brand_voice_preview | classifier_session
    """
    try:
        db.collection("api_cost_events").add({
            "provider":      "anthropic",
            "model":         model,
            "input_tokens":  usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cost_usd":      calculate_anthropic_cost(model, usage),
            "call_type":     call_type,
            "created_at":    _firestore.SERVER_TIMESTAMP,
        })
    except Exception:
        pass
