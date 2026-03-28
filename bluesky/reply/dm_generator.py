"""
DM generation for proactive like/repost thank-you messages.

Fan DMs:
  generate_like_dm        — fan liked a post
  generate_repost_dm      — fan reposted (higher energy)

Creator DMs:
  generate_creator_repost_dm — peer reposted (peer register, no fan language)

Themed account DMs:
  generate_themed_repost_dm  — niche aggregator reposted (playful, niche-aware)

Studio DMs:
  generate_studio_repost_dm  — studio reposted (simple professional thanks)

Fan/creator DMs accept an optional continuation_message: if the person already
replied in the DM thread, the message thanks them AND picks up where they left off.
"""
import json
import os
import anthropic

_SYSTEM = """You write direct messages on Bluesky on behalf of the creator described \
in the brand voice below. Write ONLY the DM text — no quotes, no labels, nothing else. \
Do not address the recipient by their handle or username. Never use em dashes (—). \
Use "Thanks" rather than "Appreciate" when expressing gratitude."""

_LIKE_DM_PROMPT = """{brand_voice}

---

Someone liked your post. Write a short, warm DM thanking them. \
Explicitly acknowledge that they liked the post — say they liked it, hit the like, \
or similar. Don't be vague about what they did.

Their handle: @{handle}
Your post they liked:
{post_text}

---

{continuation_section}Keep it personal and genuine — not copy-paste energy. \
No hard sell. No subscription pitch.
{emoji_instruction}
2 sentences maximum."""

_REPOST_DM_PROMPT = """{brand_voice}

---

Someone reposted your content — a bigger deal than a like. Write a warm, slightly \
more enthusiastic DM thanking them for sharing it. Explicitly acknowledge the repost \
— say they reposted it, shared it, or put it on their feed. Don't be vague about what they did.

Their handle: @{handle}
Your post they reposted:
{post_text}

---

{continuation_section}Genuine appreciation — they helped your content reach more people. \
No hard sell. No subscription pitch.
{emoji_instruction}
2 sentences maximum."""

_CONTINUATION_SECTION = """They previously replied to you in DMs. Pick up that conversation \
naturally — thank them for the {interaction} AND respond to their last message.

Their last DM to you:
{last_message}

"""

_DISCOUNT_WARM_SECTION = """This is a warm lead — they've shown genuine interest. Naturally steer \
toward a first-time offer: use code {code} for 50% off at SeanXavier.com. \
Include this link: {url}
Make it feel like a personal gift, not a sales pitch. Weave it in naturally at the end."""

_DISCOUNT_DIRECT_SECTION = """Make a warm but direct offer — they've liked/reposted before but \
haven't replied. Lead with the thank-you, then offer: code {code} for 50% off their first month \
at SeanXavier.com. Include this link: {url}
Keep it genuine. One clear CTA at the end, nothing pushy."""


def _discount_section(discount):
    """Build the discount instruction block from a discount dict, or return empty string."""
    if not discount:
        return ""
    if discount.get("warm"):
        return "\n" + _DISCOUNT_WARM_SECTION.format(
            code=discount["code"], url=discount.get("url", "")
        )
    return "\n" + _DISCOUNT_DIRECT_SECTION.format(
        code=discount["code"], url=discount.get("url", "")
    )


def _call(system, prompt):
    client = anthropic.Anthropic()
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=150,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


def generate_like_dm(handle, post_text, brand_voice, continuation_message=None, discount=None):
    continuation = (
        _CONTINUATION_SECTION.format(interaction="like", last_message=continuation_message)
        if continuation_message else ""
    )
    prompt = _LIKE_DM_PROMPT.format(
        brand_voice=brand_voice,
        handle=handle,
        post_text=post_text[:300],
        continuation_section=continuation,
        emoji_instruction="Do not add emojis unless they feel completely natural.",
    ) + _discount_section(discount)
    return _call(_SYSTEM, prompt)


def generate_repost_dm(handle, post_text, brand_voice, continuation_message=None, discount=None):
    continuation = (
        _CONTINUATION_SECTION.format(interaction="repost", last_message=continuation_message)
        if continuation_message else ""
    )
    prompt = _REPOST_DM_PROMPT.format(
        brand_voice=brand_voice,
        handle=handle,
        post_text=post_text[:300],
        continuation_section=continuation,
        emoji_instruction="Do not add emojis unless they feel completely natural.",
    ) + _discount_section(discount)
    return _call(_SYSTEM, prompt)


_CREATOR_REPOST_DM_PROMPT = """{brand_voice}

---

A fellow creator reposted your content. Write a short DM thanking them — peer register,
equal footing. Not fan energy, not industry-networking energy. Just two creators being real.

Their handle: @{handle}
Your post they reposted:
{post_text}

---

{continuation_section}Warm and genuine. No collab pitch. No subscription language.
Do not add emojis unless they feel completely natural.
2 sentences maximum."""

_THEMED_REPOST_DM_PROMPT = """{brand_voice}

---

A themed/niche content aggregator account reposted your post — they curate content for
fans of a specific type (muscle, big dicks, etc.) and sharing your post means your content
fits their aesthetic.

Their handle: @{handle}
Your post they reposted:
{post_text}

---

{continuation_section}Write a short DM that's warm and slightly playful — acknowledge that
you fit what they're into without being crass. It's a compliment that they chose your content.
Do not pitch, sell, or mention subscribing.
Do not add emojis unless they feel completely natural.
2 sentences maximum."""

_STUDIO_REPOST_DM_PROMPT = """{brand_voice}

---

A porn studio reposted your content. Write a brief, professional thank-you DM.

Their handle: @{handle}
Your post they reposted:
{post_text}

---

Simple and genuine. No flattery. No pitching. Just a clean thank-you.
Do not add emojis.
2 sentences maximum."""


def generate_creator_repost_dm(handle, post_text, brand_voice, continuation_message=None):
    continuation = (
        _CONTINUATION_SECTION.format(interaction="repost", last_message=continuation_message)
        if continuation_message else ""
    )
    prompt = _CREATOR_REPOST_DM_PROMPT.format(
        brand_voice=brand_voice,
        handle=handle,
        post_text=post_text[:300],
        continuation_section=continuation,
    )
    return _call(_SYSTEM, prompt)


def generate_themed_repost_dm(handle, post_text, brand_voice, continuation_message=None):
    continuation = (
        _CONTINUATION_SECTION.format(interaction="repost", last_message=continuation_message)
        if continuation_message else ""
    )
    prompt = _THEMED_REPOST_DM_PROMPT.format(
        brand_voice=brand_voice,
        handle=handle,
        post_text=post_text[:300],
        continuation_section=continuation,
    )
    return _call(_SYSTEM, prompt)


def generate_studio_repost_dm(handle, post_text, brand_voice):
    prompt = _STUDIO_REPOST_DM_PROMPT.format(
        brand_voice=brand_voice,
        handle=handle,
        post_text=post_text[:300],
    )
    return _call(_SYSTEM, prompt)


_CLASSIFY_SIGNAL_PROMPT = """Score the following fan messages from a single conversation thread \
across five dimensions (0-2 each):

- volume: 0 = under 10 words total, 1 = 10-30 words total, 2 = 30+ words total
- specificity: 0 = generic reactions only, 1 = references a context or moment, \
2 = references specific acts or observable detail
- register: 0 = no explicit commitment, 1 = suggestive, 2 = explicitly committed to a sexual register
- disclosure: 0 = pure reaction, 1 = implied preference, 2 = shares personal experience or psychology
- complexity: 0 = only single fragments, 1 = some multi-sentence messages, \
2 = sustained multi-sentence structure with varied rhythm

Fan messages:
{messages}

Reply with JSON only: {{"volume": n, "specificity": n, "register": n, "disclosure": n, "complexity": n}}"""

_ADAPTIVE_INSTRUCTIONS = {
    "low": (
        "The fan hasn't revealed much yet. Write in Sean's default voice. "
        "End with a question or observation that draws them out — invite them toward something more specific: "
        "what got to them, what they're into, what they would want. Pull them toward more without asking directly."
    ),
    "medium": (
        "The fan has shown some signal. Match their energy level and temperature — "
        "meet them where they are emotionally without fully adopting their vocabulary."
    ),
    "high": (
        "The fan has shown strong signal across the conversation. "
        "Adapt fully to mirror their register, vocabulary, pace, and emotional temperature. "
        "The voice is always Sean's; the register meets theirs completely."
    ),
}


def _score_thread_signal(fan_messages: list) -> tuple:
    """
    Score all fan messages in the thread for adaptation signal.
    Returns (total_score 0-10, tier 'low'|'medium'|'high').
    Falls back to (0, 'low') on any error.
    """
    if not fan_messages:
        return 0, "low"
    combined = "\n".join(fan_messages)
    client = anthropic.Anthropic()
    try:
        result = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=60,
            messages=[{"role": "user", "content": _CLASSIFY_SIGNAL_PROMPT.format(messages=combined)}],
        )
        raw = result.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        scores = json.loads(raw.strip())
        total = sum(scores.values())
    except Exception:
        return 0, "low"
    if total <= 3:
        tier = "low"
    elif total <= 6:
        tier = "medium"
    else:
        tier = "high"
    return total, tier


_CONVERSATION_REPLY_PROMPT = """{brand_voice}

---

You are continuing an ongoing DM conversation with a fan on Bluesky.

Their handle: @{handle}

Conversation so far:
{history}

Their latest message:
{their_message}

---

{adaptive_instruction}

{cta_instruction}Do not add emojis unless they feel completely natural.
2 sentences maximum."""

_CTA_INSTRUCTION = """This fan has been engaged across multiple exchanges. \
Weave in a natural, low-pressure mention of where they can see more — \
frame it as a personal invitation, not a pitch. Include this link: {url}{discount_line}
"""

_CTA_DISCOUNT_LINE = " Offer this discount if it feels right: {code}"


def _cta_instruction(exchange_count: int) -> str:
    """Return a CTA instruction block when exchange_count >= 2 and env vars are set, else empty string."""
    if exchange_count < 2:
        return ""
    url = os.environ.get("FAN_DISCOUNT_URL_REPLY") or os.environ.get("FAN_DISCOUNT_URL_DM", "")
    if not url:
        return ""
    code = os.environ.get("FAN_DISCOUNT_CODE", "")
    discount_line = _CTA_DISCOUNT_LINE.format(code=code) if code else ""
    return _CTA_INSTRUCTION.format(url=url, discount_line=discount_line)


def generate_conversation_reply(handle, their_message, history, brand_voice, exchange_count=0):
    """Generate a reply to an inbound DM in an ongoing conversation."""
    fan_messages = [
        h["content"] for h in history[-10:] if h["role"] != "assistant"
    ] + [their_message]
    _, tier = _score_thread_signal(fan_messages)

    history_text = "\n".join(
        f"{'You' if h['role'] == 'assistant' else 'Fan'}: {h['content']}"
        for h in history[-10:]
    )
    prompt = _CONVERSATION_REPLY_PROMPT.format(
        brand_voice=brand_voice,
        handle=handle,
        history=history_text or "(no prior messages)",
        their_message=their_message,
        adaptive_instruction=_ADAPTIVE_INSTRUCTIONS[tier],
        cta_instruction=_cta_instruction(exchange_count),
    )
    return _call(_SYSTEM, prompt)
