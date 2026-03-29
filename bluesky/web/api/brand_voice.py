"""
Brand voice markdown renderer.
Converts a Firestore _system/brand_voice document dict into a
prompt-ready markdown string matching the brandvoice-template-v1 schema.
"""


def render_brand_voice_md(doc: dict) -> str:
    """
    Render a brand voice Firestore document to markdown.
    Returns a string suitable for use as a Claude system prompt.
    """
    lines = []

    # --- Header ---
    version = doc.get("version", 1)
    pushed_at = doc.get("pushed_at", "")
    lines.append(f"# Brand Voice (v{version})")
    if pushed_at:
        lines.append(f"*Last updated: {pushed_at[:10]}*")
    lines.append("")

    # --- §1 Identity ---
    identity = doc.get("identity", {})
    lines.append("## §1 Identity")
    if identity.get("creator_name"):
        lines.append(f"**Creator:** {identity['creator_name']}")
    if identity.get("handle"):
        lines.append(f"**Handle:** {identity['handle']}")
    if identity.get("platform"):
        lines.append(f"**Platform:** {identity['platform']}")
    if identity.get("persona_summary"):
        lines.append("")
        lines.append(identity["persona_summary"])
    pillars = identity.get("core_pillars", [])
    if pillars:
        lines.append("")
        lines.append(f"**Core Pillars:** {', '.join(pillars)}")
    lines.append("")

    # --- §2 Voice & Register ---
    voice = doc.get("voice", {})
    lines.append("## §2 Voice & Register")
    if voice.get("philosophy"):
        lines.append("")
        lines.append("### Philosophy")
        lines.append(voice["philosophy"])
    if voice.get("point_of_view"):
        lines.append("")
        lines.append("### POV")
        lines.append(voice["point_of_view"])
    lines.append("")

    # --- §3 Lexicon ---
    lexicon = doc.get("lexicon", {})
    lines.append("## §3 Lexicon")
    approved = lexicon.get("approved_vocab", [])
    if approved:
        lines.append("")
        lines.append("**Approved vocabulary:** " + ", ".join(approved))
    banned = lexicon.get("banned_vocab", [])
    if banned:
        lines.append("")
        lines.append("**Banned vocabulary:** " + ", ".join(banned))
    if lexicon.get("punctuation_rules"):
        lines.append("")
        lines.append("**Punctuation:** " + lexicon["punctuation_rules"])
    if lexicon.get("emoji_rules"):
        lines.append("")
        lines.append("**Emoji:** " + lexicon["emoji_rules"])
    lines.append("")

    # --- §4 Structural Rules ---
    structural = doc.get("structural", {})
    lines.append("## §4 Structural Rules")
    reply_lengths = structural.get("reply_lengths", {})
    if reply_lengths:
        lines.append("")
        lines.append("**Reply lengths:**")
        for k, v in reply_lengths.items():
            lines.append(f"- {k}: {v}")
    if structural.get("rhythm"):
        lines.append("")
        lines.append("**Rhythm:** " + structural["rhythm"])
    if structural.get("opening_lines"):
        lines.append("")
        lines.append("**Opening lines:** " + structural["opening_lines"])
    lines.append("")

    # --- §5 Content Rules ---
    content_rules = doc.get("content_rules", {})
    lines.append("## §5 Content Rules")
    always = content_rules.get("always", [])
    if always:
        lines.append("")
        lines.append("**Always:**")
        for rule in always:
            lines.append(f"- {rule}")
    never = content_rules.get("never", [])
    if never:
        lines.append("")
        lines.append("**Never:**")
        for rule in never:
            lines.append(f"- {rule}")
    lines.append("")

    # --- §6 Platform Rules ---
    platform = doc.get("platform_rules", {})
    lines.append("## §6 Platform Rules (Bluesky)")
    if platform.get("bluesky_public_reply_tone"):
        lines.append("")
        lines.append("**Public reply tone:** " + platform["bluesky_public_reply_tone"])
    if platform.get("thread_behaviour"):
        lines.append("")
        lines.append("**Thread behaviour:** " + platform["thread_behaviour"])
    if platform.get("dm_vs_public"):
        lines.append("")
        lines.append("**DM vs public:** " + platform["dm_vs_public"])
    if platform.get("comment_engine_tone"):
        lines.append("")
        lines.append("**Comment engine tone:** " + platform["comment_engine_tone"])
    lines.append("")

    # --- §7 Archetypes ---
    archetypes = doc.get("archetypes", [])
    if archetypes:
        lines.append("## §7 Fan Archetypes")
        for arch in archetypes:
            lines.append("")
            lines.append(f"### {arch.get('name', 'Archetype')}")
            if arch.get("signals"):
                lines.append(f"**Signals:** {arch['signals']}")
            if arch.get("opener_style"):
                lines.append(f"**Opener:** {arch['opener_style']}")
        lines.append("")

    return "\n".join(lines)
