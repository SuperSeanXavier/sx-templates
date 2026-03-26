---
template_version: 1.0.0
---

# BrandVoice Template v1.0.0

This file defines the structure for a creator's brand voice instance. Each section contains instructions for what to fill in — no creator-specific values live here.

**To create an instance:** copy this file, fill in each section with your creator's values, and save it as `brandvoice.md` in your project config. Set `BRANDVOICE_PATH` to its absolute path.

**To version-upgrade an instance:** diff `brandvoice-template-v1.md` against the new version, update your `brandvoice.md` to conform, and bump the `template_version` field in its frontmatter.

---

## 1. Identity

*Who is this creator? Define the persona, platforms, and fan-facing positioning.*

- **Name / Handle:** [Creator name and primary social handle]
- **Platforms:** [List all active platforms, e.g. OnlyFans, Twitter/X, Instagram, Bluesky, personal site]
- **Persona:** [1–3 sentence description of the persona — who they are, what they project, how fans relate to them]
- **Core Pillars:** [3 adjectives or short phrases that are non-negotiable traits of the persona]

---

## 2. Voice & Register

*How does this creator sound? Define tone, mode, and the philosophy behind the content.*

### Philosophy
[What is the creator's approach to their content? What makes it authentic or distinct? What is NOT performed? What IS shown? Keep this to the guiding principle, not rules — rules go in §4 and §5.]

### Modes
[Does the creator operate in different modes depending on platform or audience? Define each mode with its platform context, target audience, and tonal qualities. If single-mode, define one row.]

| Mode | Platform / Context | Target Audience | Tone |
|---|---|---|---|
| [Mode name] | [Platform or situation] | [Audience type] | [Tone description] |

### POV
[Describe the grammatical and relational perspective: first person / third person / direct address. How does the creator speak to the fan — singular or plural? Intimate or broadcast?]

---

## 3. Lexicon

*Words to use and words to avoid. This section is loaded directly by copy generation tools.*

### Approved Vocabulary
- **Verbs:** [Comma-separated list — include conjugation note if non-standard]
- **Nouns:** [Comma-separated list]
- **Notes:** [Any usage rules: tense, person, frequency guidelines]

### Banned Vocabulary
[Group bans by category for clarity. Include a brief rationale for each category.]

- **[Category name]:** [Words or phrases] — [Why banned]
- **[Category name]:** [Words or phrases] — [Why banned]

### Punctuation Rules
[Any punctuation rules specific to this creator's voice. Note exceptions explicitly.]

---

## 4. Structural Rules

*How content is physically constructed — fragment patterns, rhythm, chaining, length. These rules apply across all long-form formats unless a Platform Extension overrides them.*

### Fragment Architecture
[How long should fragments be? How do short and long fragments alternate to create rhythm? What is the target fragment count for a standard post?]

### Sentence Chaining
[How are multiple actions chained within a single sentence? Which connectors are used inside a sentence? Which connectors are banned at the start of a new fragment?]

### Verb Rules
[Any verb variety, rotation, or frequency rules.]

### Qualifier Rules
[Rules for adjectives and descriptors: which body / whose body, stacking limits, frequency guidelines.]

### Personality / Voice Lines
[Rules for lines that reveal character rather than describe action. How often? What do they sound like? What are they explicitly NOT (to prevent drift)?]

### Length Targets
[Word count targets by format, if applicable.]

---

## 5. Content Rules

*Global rules that apply across all platforms and formats. What to always do and never do.*

- [Rule]
- [Rule]
- [Add as many as needed — keep each rule to one line]

---

## 6. Platform Extensions

*Platform-specific rules for voice, format, links, and scheduling. Each subsection adds to or overrides §4 for that platform.*

### 6.1 Twitter / X
- **Format:** [Hook style, character target, structure rules]
- **Hook types:** [Named hook types with 1-line description or example each]
- **Slashtag pattern:** [Pattern for link slashtags on this platform]
- **UTM params:** [utm_source, utm_medium, utm_campaign values]
- **Rebrandly tags:** [Which tags to apply at link creation]
- **Other notes:** [Emoji rules, threading behavior, fan-request acknowledgment, etc.]

### 6.2 Instagram
- **Format:** [Caption style and length]
- **Slashtag pattern:** [Pattern for link slashtags on this platform]
- **Rebrandly tags:** [Which tags to apply]
- **Other notes:** [Any IG-specific rules]

### 6.3 [Primary subscription / content platform]
[Name this section for the creator's main platform, e.g. SeanXavier.com, OnlyFans, etc.]

- **Wall post format:** [Structure rules for the primary post format on this platform]
- **Long-form / vault format:** [Structure for extended narrative content, if used]
- **Promo / mass message format:** [Structure for outbound promotional messages]
- **Slashtag pattern:** [Pattern for mass message links]
- **Other notes:** [Anything platform-specific]

### 6.4 Bluesky
- **Register:** [How does this creator sound on Bluesky vs. broadcast platforms? More conversational? Same voice?]
- **Reply tone:** [How should replies sound — direct, warm, brief?]
- **Reply length:** [Character target for replies, e.g. ≤300 chars]
- **Engagement triggers:** [What types of posts or replies should be responded to?]
- **When to ignore:** [What to skip — spam, negativity, off-topic, etc.]
- **Thread behavior:** [Rules for multi-post threads on Bluesky]
- **Scheduling / repurpose notes:** [Any rules for reposting or repurposing Twitter content to Bluesky]

### 6.5 [Additional platforms]
[Add sections as needed for other active platforms.]

---

## 7. Archetypes

*Optional. Define audience segments and engagement strategies for each. Used by copy generation tools to target opening hooks.*

1. **[Archetype name]:** [Who this fan is, what they need, how to open for them.] *Key word: [X]*
2. [Repeat for each archetype]

**Fan request rule:** [Global rule for acknowledging direct fan requests in copy, if applicable]
