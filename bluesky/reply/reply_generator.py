import os
import re
import anthropic

_SYSTEM = """You write replies on Bluesky on behalf of the creator described in the brand voice below. \
Follow §6.4 (Bluesky) rules strictly. Reply with ONLY the reply text — no quotes, no labels, nothing else. \
Do not address the person by their handle or username. Never use em dashes (—)."""

_SIMULATE_PROMPT = """You are roleplaying as a fan replying to a creator on Bluesky.

The creator just replied to you with:
{sean_reply}

Write a realistic short fan reply (1-2 sentences, casual, conversational Bluesky style). \
Reply with ONLY the fan's message — no labels, no quotes."""

_CLASSIFY_PEER_INTENT_PROMPT = """Classify this Bluesky reply from a fellow creator into exactly one category:
- compliment: praising, showing appreciation, or hyping the creator
- dm_seeking: expressing interest in connecting, collaborating, or DMing
- general: question, comment, or conversation that doesn't fit the above

Reply: {text}

Reply with only the category word."""

_PEER_HIGH_FOLLOWER_PROMPT = """{brand_voice}

---

You are replying to a fellow creator on Bluesky — peer register, not fan register. Equal footing.

Their reply (@{handle}, ~{follower_count:,} followers):
{reply_text}

---

Respond warmly as a peer. {collab_line}
{emoji_line}
{word_limit_line}"""

_PEER_LOW_COMPLIMENT_PROMPT = """{brand_voice}

---

A smaller creator is giving you a compliment on Bluesky (public post).

Their reply (@{handle}):
{reply_text}

---

Thank them warmly and briefly — genuine, not gushing. No collab mention. No DM invitation.
{emoji_line}
{word_limit_line}"""

_PEER_LOW_DECLINE_PROMPT = """{brand_voice}

---

A smaller creator is expressing interest in DMing or collaborating on Bluesky (this is public).

Their reply (@{handle}):
{reply_text}

---

Write exactly 3 numbered responses that are warm but non-committal — don't encourage, don't offend.
Vague and kind. Think "maybe someday" energy. Keep each under 15 words.
No collab offers. Reply with ONLY the three numbered options, nothing else.
1.
2.
3."""

_PEER_LOW_GENERAL_PROMPT = """{brand_voice}

---

A smaller creator replied to you on Bluesky (public post).

Their reply (@{handle}):
{reply_text}

---

Respond warmly and briefly as a peer. Keep it conversational. No DM invitation. No collab mention.
{emoji_line}
{word_limit_line}"""

_STUDIO_THANKS_PROMPT = """{brand_voice}

---

A porn studio replied to your post on Bluesky.

Your post:
{original_text}

Their reply (@{handle}):
{reply_text}

---

Write a brief, warm thank-you reply. Professional but not stiff. No pitching, no collab offers.
{emoji_line}
Keep it under 15 words."""

_THEMED_REPLY_PROMPT = """{brand_voice}

---

A themed/niche content aggregator account replied to your post on Bluesky.
They curate content for fans of a specific type (muscle, big dicks, etc.).

Your post:
{original_text}

Their reply (@{handle}):
{reply_text}

---

Write a reply that's warm and playful — lean into the fact that your content fits their aesthetic.
No subscription pitch. No calls to action.
{emoji_line}
{word_limit_line}"""

_CLASSIFY_SUBSCRIBER_PROMPT = """Does this Bluesky reply indicate that the person is already a paying subscriber \
or member of the creator's content (e.g. OnlyFans, Fansly, or similar platform)?

Reply: {text}

Reply with only: yes or no"""

_SUBSCRIBER_THANKS_PROMPT = """{brand_voice}

---

A fan replied to your post and mentioned they are already a subscriber/member.

Your post:
{original_text}

Their reply (@{handle}):
{reply_text}

---

Write a warm, genuine thank-you reply. Do NOT mention subscribing, joining, \
or any calls to action — they're already a member. Just appreciate them.
{emoji_line}
{word_limit_line}"""

_CLASSIFY_POST_PROMPT = """Classify this Bluesky post into exactly one category:
- promotional: selling, teasing, or announcing paid content, subscriptions, or products
- content: sharing free content, previews, behind-the-scenes, showcasing work
- personal: personal thoughts, life updates, not content-related
- casual: quick reaction, banter, low-effort post

Post: {text}

Reply with only the category word."""

_CLASSIFY_INTENT_PROMPT = """Classify this Bluesky reply into exactly one category:
- buying_signal: expresses intent or desire to subscribe, pay, or purchase
- curious: asks about content, shows interest without clear purchase intent
- casual: friendly engagement with no clear interest signal
- negative: criticism, spam, or off-topic

Reply: {text}

Reply with only the category word."""

# Matches most common emoji unicode ranges
_EMOJI_RE = re.compile(
    r"[\U00010000-\U0010ffff"
    r"\U00002600-\U000027ff"
    r"\U0000fe00-\U0000fe0f"
    r"\U0001f300-\U0001f9ff]"
)

GATED_POST_TYPES = {"personal", "casual"}
PITCH_INTENT = {"buying_signal", "curious"}


def _has_emoji(text):
    return bool(_EMOJI_RE.search(text))


def _emoji_line(reply_text):
    if _has_emoji(reply_text):
        return "The fan used emojis — match their energy and include emojis naturally."
    return "Do not add emojis."


def _word_limit_line(reply_text):
    fan_words = len(reply_text.split())
    return f"Keep your reply to {12 + fan_words} words or fewer ({fan_words} fan words + 12)."


def _build_reply_prompt(brand_voice, original_text, reply_text, handle, nudge=False):
    if nudge:
        question_instruction = (
            "End with a genuine question that steers toward their content interests or "
            "what they'd love to see more of — warm and curious, never salesy."
        )
    else:
        question_instruction = (
            "Prefer to end with a genuine question that invites them to share more "
            "or keep the conversation going."
        )
    return "\n\n".join([
        brand_voice,
        "---",
        f"The creator's post:\n{original_text}",
        f"Reply from @{handle}:\n{reply_text}",
        "---",
        "\n".join([
            "Write a reply in the creator's voice.",
            question_instruction,
            _emoji_line(reply_text),
            _word_limit_line(reply_text),
        ]),
    ])


def _build_dm_pull_prompt(brand_voice, original_text, reply_text, handle, used_pulls=None, discount=None):
    instructions = [
        f"Write a warm, personal reply in the creator's voice that pulls @{handle} toward continuing this in DMs.",
        _emoji_line(reply_text),
        _word_limit_line(reply_text),
        "End with a clear but natural invitation to DM — vary the phrasing, never formulaic.",
    ]
    if discount:
        instructions.append(f"Offer this as the incentive to DM: {discount}")
    if used_pulls:
        avoids = "\n".join(f"  - {p}" for p in used_pulls)
        instructions.append(
            f"These phrases have already been used in this thread — avoid similar phrasing or openings:\n{avoids}"
        )
    return "\n\n".join([
        brand_voice,
        "---",
        "This is a follow-up exchange — the fan has continued the conversation.",
        f"The creator's original post:\n{original_text}",
        f"Fan's follow-up (@{handle}):\n{reply_text}",
        "---",
        "\n".join(instructions),
    ])


def load_brand_voice():
    """
    Load brand voice text. Two sources (checked in order):
      1. BRANDVOICE_CONTENT env var — set in Cloud Functions via Secret Manager
      2. BRANDVOICE_PATH env var  — absolute path, used in local dev
    """
    content = os.environ.get("BRANDVOICE_CONTENT")
    if content:
        return content
    path = os.environ["BRANDVOICE_PATH"]
    with open(path) as f:
        return f.read()


def _call(prompt, max_tokens=200):
    client = anthropic.Anthropic()
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


def _classify(prompt):
    """Lightweight classification call — no system prompt, minimal tokens."""
    client = anthropic.Anthropic()
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=5,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip().lower()


def classify_post_type(post_text):
    """Returns: 'promotional' | 'content' | 'personal' | 'casual'"""
    return _classify(_CLASSIFY_POST_PROMPT.format(text=post_text))


def classify_fan_intent(reply_text):
    """Returns: 'buying_signal' | 'curious' | 'casual' | 'negative'"""
    return _classify(_CLASSIFY_INTENT_PROMPT.format(text=reply_text))


def classify_peer_intent(reply_text):
    """Returns: 'compliment' | 'dm_seeking' | 'general'"""
    return _classify(_CLASSIFY_PEER_INTENT_PROMPT.format(text=reply_text))


def generate_peer_reply(reply_text, handle, follower_count, brand_voice, collab_threshold=None):
    """
    Generate a peer-register reply for a creator.
    Returns a list of strings:
      - length 1 for most cases
      - length 3 for low-follower DM-seeking (polite decline options for user to pick from)
    """
    from bluesky.reply.creator_classifier import COLLAB_DM_THRESHOLD
    threshold = collab_threshold or COLLAB_DM_THRESHOLD

    peer_intent = classify_peer_intent(reply_text)

    if follower_count >= threshold:
        collab_lines = {
            "dm_seeking": "They're open to connecting — invite them to DMs to chat or explore a collab.",
            "compliment":  "Acknowledge warmly and briefly.",
            "general":     "Keep it conversational. If it feels natural, mention DMs are open.",
        }
        prompt = _PEER_HIGH_FOLLOWER_PROMPT.format(
            brand_voice=brand_voice,
            handle=handle,
            follower_count=follower_count,
            reply_text=reply_text,
            collab_line=collab_lines.get(peer_intent, collab_lines["general"]),
            emoji_line=_emoji_line(reply_text),
            word_limit_line=_word_limit_line(reply_text),
        )
        return [_call(prompt)], peer_intent

    # Low follower
    if peer_intent == "dm_seeking":
        prompt = _PEER_LOW_DECLINE_PROMPT.format(
            brand_voice=brand_voice,
            handle=handle,
            reply_text=reply_text,
        )
        raw = _call(prompt, max_tokens=200)
        options = [
            line.lstrip("123. ").strip()
            for line in raw.splitlines()
            if line.strip() and line.strip()[0].isdigit()
        ]
        return (options or [raw]), peer_intent

    if peer_intent == "compliment":
        prompt = _PEER_LOW_COMPLIMENT_PROMPT.format(
            brand_voice=brand_voice,
            handle=handle,
            reply_text=reply_text,
            emoji_line=_emoji_line(reply_text),
            word_limit_line=_word_limit_line(reply_text),
        )
    else:
        prompt = _PEER_LOW_GENERAL_PROMPT.format(
            brand_voice=brand_voice,
            handle=handle,
            reply_text=reply_text,
            emoji_line=_emoji_line(reply_text),
            word_limit_line=_word_limit_line(reply_text),
        )
    return [_call(prompt)], peer_intent


def generate_reply(original_text, reply_text, handle, brand_voice, nudge=False):
    return _call(_build_reply_prompt(brand_voice, original_text, reply_text, handle, nudge=nudge))


def generate_dm_pull_reply(original_text, reply_text, handle, brand_voice, used_pulls=None, discount=None):
    return _call(_build_dm_pull_prompt(brand_voice, original_text, reply_text, handle, used_pulls, discount))


def generate_studio_thanks(original_text, reply_text, handle, brand_voice):
    """Brief professional thanks to a studio reply — no pitch, no collab offers."""
    prompt = _STUDIO_THANKS_PROMPT.format(
        brand_voice=brand_voice,
        original_text=original_text,
        reply_text=reply_text,
        handle=handle,
        emoji_line=_emoji_line(reply_text),
    )
    return _call(prompt)


def generate_themed_reply(original_text, reply_text, handle, brand_voice):
    """Playful, niche-aware reply to a themed aggregator account."""
    prompt = _THEMED_REPLY_PROMPT.format(
        brand_voice=brand_voice,
        original_text=original_text,
        reply_text=reply_text,
        handle=handle,
        emoji_line=_emoji_line(reply_text),
        word_limit_line=_word_limit_line(reply_text),
    )
    return _call(prompt)


def classify_subscriber_mention(reply_text):
    """Returns True if the reply indicates the person is already a subscriber."""
    result = _classify(_CLASSIFY_SUBSCRIBER_PROMPT.format(text=reply_text))
    return result.startswith("yes")


def generate_subscriber_thanks(original_text, reply_text, handle, brand_voice):
    """Warm thank-you for an existing subscriber — no funnel, no pitch."""
    prompt = _SUBSCRIBER_THANKS_PROMPT.format(
        brand_voice=brand_voice,
        original_text=original_text,
        reply_text=reply_text,
        handle=handle,
        emoji_line=_emoji_line(reply_text),
        word_limit_line=_word_limit_line(reply_text),
    )
    return _call(prompt)


def simulate_fan_reply(sean_reply):
    """For dry-run only — simulates a realistic fan follow-up to Sean's reply."""
    client = anthropic.Anthropic()
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=100,
        messages=[{"role": "user", "content": _SIMULATE_PROMPT.format(sean_reply=sean_reply)}],
    )
    return message.content[0].text.strip()
