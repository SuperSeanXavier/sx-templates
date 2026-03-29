"""
Human handoff detection for active DM conversations.

check_handoff_triggers(message_text, exchange_count, ai_confidence)
    → (True, reason) if the conversation should be handed off to a human
    → (False, None) if automated replies can continue

flag_handoff(handle, reason)
    → sets human_handoff=True on the Firestore conversations document

Trigger conditions:
  1. Fan asks if they're speaking to a real person
  2. Mentions pricing, rates, or custom content requests
  3. Expresses distress or sends abusive content
  4. Conversation has reached >= 8 exchanges
  5. AI confidence flag is low
"""
import re

import anthropic

from bluesky.shared.firestore_client import db
from bluesky.shared.cost_calculator import write_cost_event

# ---------------------------------------------------------------------------
# Keyword patterns for fast local detection (no API call)
# ---------------------------------------------------------------------------

_REAL_PERSON_PATTERNS = [
    r"\breal person\b",
    r"\bactual(ly)? (you|him|her|them)\b",
    r"\bare you (a )?bot\b",
    r"\bai\b",
    r"\bautomated\b",
    r"\btalking to (a )?(human|person|real)\b",
    r"\bis (this|it) (really |actually )?(you|him|her)\b",
    r"\bdo you (actually |really )?exist\b",
    r"\bwho am i (talking|speaking) to\b",
]

_PRICING_PATTERNS = [
    r"\brates?\b",
    r"\bhow much\b",
    r"\bpric(e|ing|es)\b",
    r"\bcustom (content|video|pic|photo|clip)\b",
    r"\bpersonali[sz]ed\b",
    r"\bcommission\b",
    r"\bpay(ment)?\b",
    r"\bvenmo\b",
    r"\bcashapp\b",
    r"\bpaypal\b",
]

_DISTRESS_PATTERNS = [
    r"\bkill (my)?self\b",
    r"\bsuicid(e|al)\b",
    r"\bwant to die\b",
    r"\bhurt(ing)? (my)?self\b",
    r"\bfuck you\b",
    r"\bscam\b",
    r"\breport(ing)? you\b",
    r"\blawsuit\b",
]


def _matches(text, patterns):
    t = text.lower()
    return any(re.search(p, t) for p in patterns)


def _semantic_real_person_check(message_text):
    """
    Ask Claude if the message is asking whether they're talking to a real person.
    Called only when keyword patterns miss but we want a second opinion.
    Returns True if semantic check confirms the trigger.
    """
    client = anthropic.Anthropic()
    result = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        messages=[{
            "role": "user",
            "content": (
                "Does this DM message ask whether the sender is a real person, "
                "or express doubt that they're talking to a human (vs. a bot or AI)?\n\n"
                f"Message: {message_text}\n\n"
                "Reply with only YES or NO."
            ),
        }],
    )
    write_cost_event(db, result.model, result.usage, "intent_classification")
    answer = result.content[0].text.strip().upper()
    return answer.startswith("YES")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_handoff_triggers(message_text, exchange_count, ai_confidence=None):
    """
    Evaluate whether a conversation should be handed off to a human.

    Args:
        message_text:   The fan's latest message.
        exchange_count: Total back-and-forth exchanges in this conversation.
        ai_confidence:  Optional bool — False means the generator flagged low confidence.

    Returns:
        (True, reason_str) or (False, None)
    """
    # Trigger 5: low AI confidence
    if ai_confidence is not None and not ai_confidence:
        return True, "low_ai_confidence"

    # Trigger 4: conversation too long
    if exchange_count >= 10:
        return True, "max_exchanges"

    # Trigger 3: distress or abusive content
    if _matches(message_text, _DISTRESS_PATTERNS):
        return True, "distress_or_abuse"

    # Trigger 2: pricing / custom content
    if _matches(message_text, _PRICING_PATTERNS):
        return True, "pricing_or_custom"

    # Trigger 1: real person question — fast keyword check first, then semantic
    if _matches(message_text, _REAL_PERSON_PATTERNS):
        return True, "real_person_question"

    # Semantic fallback for ambiguous phrasing (e.g. "is this really Sean?")
    try:
        if _semantic_real_person_check(message_text):
            return True, "real_person_question"
    except Exception:
        pass  # semantic check is best-effort

    return False, None


def flag_handoff(handle, reason):
    """
    Mark a conversation as requiring human attention.
    Silences all further automated replies for this handle.
    """
    db.collection("conversations").document(handle).update({
        "human_handoff": True,
        "handoff_reason": reason,
    })
    print(f"  [HANDOFF] @{handle} flagged — reason: {reason}. Automated replies silenced.")
