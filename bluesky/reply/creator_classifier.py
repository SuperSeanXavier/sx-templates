"""
User type classification for Bluesky profiles.

Four user types, checked in priority order:
  studio  — porn studios / production companies
  themed  — niche content aggregators (muscle, big dicks, etc.)
  creator — individual adult content creators
  fan     — everyone else

Detection for studios and themed accounts:
  1. Manual handle list (env: STUDIO_HANDLES / THEMED_HANDLES, comma-separated, no @)
  2. Bio keyword matching

Creator detection uses three independently-togglable signals:
  CREATOR_DETECTION_MUTUAL_FOLLOW  — Sean follows them
  CREATOR_DETECTION_BIO            — bio contains known creator platform/role keywords
  CREATOR_DETECTION_FOLLOWER_COUNT — follower count exceeds CREATOR_FOLLOWER_THRESHOLD

For Sean's account: set only CREATOR_DETECTION_MUTUAL_FOLLOW=true.
"""
import os

# --- Thresholds ---
CREATOR_FOLLOWER_THRESHOLD = int(os.environ.get("CREATOR_FOLLOWER_THRESHOLD", "500"))
COLLAB_DM_THRESHOLD = int(os.environ.get("COLLAB_DM_THRESHOLD", "20000"))

# --- Creator detection flags ---
_FLAG_MUTUAL_FOLLOW = os.environ.get("CREATOR_DETECTION_MUTUAL_FOLLOW", "false").lower() == "true"
_FLAG_BIO = os.environ.get("CREATOR_DETECTION_BIO", "false").lower() == "true"
_FLAG_FOLLOWER_COUNT = os.environ.get("CREATOR_DETECTION_FOLLOWER_COUNT", "false").lower() == "true"

# --- Manual handle lists (no @, comma-separated) ---
def _parse_handle_list(env_key):
    raw = os.environ.get(env_key, "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}

_STUDIO_HANDLES = _parse_handle_list("STUDIO_HANDLES")
_THEMED_HANDLES = _parse_handle_list("THEMED_HANDLES")

# --- Bio keyword lists ---
_CREATOR_BIO_KEYWORDS = [
    "onlyfans", "fansly", "manyvids", "loyalfans", "clips4sale",
    "content creator", "creator", "model", "18+", "nsfw", "adult content",
    "subscribe", "fans.ly",
]

_STUDIO_BIO_KEYWORDS = [
    "productions", "studios", "studio", "films", "entertainment",
    "official", "porn network", "adult network", "media group",
    "gay porn", "adult films", "adult studio",
]

_THEMED_BIO_KEYWORDS = [
    "muscle worship", "muscle appreciation", "big dick appreciation",
    "hung men", "celebrating", "dedicated to", "daily muscle",
    "dick pics", "big cocks", "huge cocks", "massive cocks",
    "muscle men", "muscle boys", "beefy", "bear appreciation",
    "jock worship", "daddy appreciation", "twink appreciation",
]


class UserClassification:
    """Result of classifying a Bluesky profile."""

    def __init__(self, user_type, follower_count, signal="none"):
        self.user_type = user_type        # "fan" | "creator" | "themed" | "studio"
        self.follower_count = follower_count
        self.signal = signal

    @property
    def is_creator(self):
        return self.user_type == "creator"

    def __repr__(self):
        return (
            f"UserClassification(type={self.user_type!r}, "
            f"followers={self.follower_count}, signal={self.signal!r})"
        )


# Keep old name as alias so existing imports don't break
CreatorStatus = UserClassification


def classify_user(profile, flags=None):
    """
    Classify a Bluesky profile into one of four user types.

    Priority: studio → themed → creator → fan

    profile: ProfileViewDetailed from client.get_profile()
    flags: optional dict override for creator detection flags
    Returns UserClassification.
    """
    handle = (getattr(profile, "handle", "") or "").lower()
    bio = (getattr(profile, "description", "") or "").lower()
    follower_count = getattr(profile, "followers_count", 0) or 0

    # --- Studio: manual list first ---
    if handle in _STUDIO_HANDLES:
        return UserClassification("studio", follower_count, "manual_list")

    # --- Themed: manual list ---
    if handle in _THEMED_HANDLES:
        return UserClassification("themed", follower_count, "manual_list")

    # --- Studio: bio keywords ---
    for kw in _STUDIO_BIO_KEYWORDS:
        if kw in bio:
            return UserClassification("studio", follower_count, f"bio:{kw}")

    # --- Themed: bio keywords ---
    for kw in _THEMED_BIO_KEYWORDS:
        if kw in bio:
            return UserClassification("themed", follower_count, f"bio:{kw}")

    # --- Creator: existing three-signal system ---
    if flags is None:
        flags = {
            "mutual_follow": _FLAG_MUTUAL_FOLLOW,
            "bio": _FLAG_BIO,
            "follower_count": _FLAG_FOLLOWER_COUNT,
        }

    signals_hit = []

    if flags.get("mutual_follow") and getattr(getattr(profile, "viewer", None), "following", None):
        signals_hit.append("mutual_follow")

    if flags.get("bio"):
        for kw in _CREATOR_BIO_KEYWORDS:
            if kw in bio:
                signals_hit.append(f"bio:{kw}")
                break

    if flags.get("follower_count") and follower_count >= CREATOR_FOLLOWER_THRESHOLD:
        signals_hit.append("follower_count")

    active_flags_count = sum(flags.values())
    if active_flags_count == 1:
        is_creator = len(signals_hit) >= 1
    else:
        is_creator = "mutual_follow" in signals_hit or len(signals_hit) >= 2

    if is_creator:
        return UserClassification("creator", follower_count, ", ".join(signals_hit))

    return UserClassification("fan", follower_count, "none")


# Backward-compat alias used in existing code
def classify_replier(profile, flags=None):
    return classify_user(profile, flags)
