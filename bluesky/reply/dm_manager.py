"""
DM conversation state manager.

Tracks per-handle user-type classification (cached to avoid repeat API calls)
and the engagement watermark (last_checked_at) used to filter notifications.

Stored in dm_state.json (gitignored, same directory as state.json).

NOTE: Conversation anti-spam state (consecutive_mine, last_sender, convo_id)
is stored in Firestore `conversations` collection (fan_pipeline.py).
Interaction dedup is handled via Firestore `seen_events` (poller.py).
"""
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

CLASSIFICATION_TTL_DAYS = int(os.environ.get("CLASSIFICATION_TTL_DAYS", "30"))

_DEFAULT_PATH = Path(__file__).parent / "dm_state.json"

_EMPTY = {
    "conversations": {},   # {handle: {user_type, follower_count, classified_at}}
    "last_checked_at": None,
}


class DMManager:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get("DM_STATE_PATH", _DEFAULT_PATH))
        self._state = self._load()

    def _load(self):
        if self.path.exists():
            with open(self.path) as f:
                data = json.load(f)
            # Drop legacy fields from old schema
            data.pop("processed_interactions", None)
            for conv in data.get("conversations", {}).values():
                for k in ("convo_id", "consecutive_mine", "last_sender", "total_sent"):
                    conv.pop(k, None)
            return data
        return dict(_EMPTY)

    def _save(self):
        try:
            with open(self.path, "w") as f:
                json.dump(self._state, f, indent=2)
        except OSError as e:
            print(f"[dm_state] warning: could not save to {self.path}: {e}")

    # --- user type classification cache ---

    def _convo(self, handle):
        return self._state["conversations"].setdefault(handle, {})

    def get_cached_user_type(self, handle):
        """
        Returns (user_type, follower_count) if a fresh cached classification exists,
        otherwise None. Fresh = classified within CLASSIFICATION_TTL_DAYS days.
        """
        conv = self._state["conversations"].get(handle)
        if not conv:
            return None
        user_type = conv.get("user_type")
        classified_at = conv.get("classified_at")
        if not user_type or not classified_at:
            return None
        age = datetime.now(timezone.utc) - datetime.fromisoformat(classified_at)
        if age > timedelta(days=CLASSIFICATION_TTL_DAYS):
            return None
        return user_type, conv.get("follower_count", 0)

    def cache_user_type(self, handle, user_type, follower_count):
        """Store user classification result with a timestamp."""
        conv = self._convo(handle)
        conv["user_type"] = user_type
        conv["follower_count"] = follower_count
        conv["classified_at"] = datetime.now(timezone.utc).isoformat()
        self._save()

    # --- engagement timestamp watermark ---

    def get_last_checked_at(self):
        """Returns the ISO timestamp of the last engagement check, or None on first run."""
        return self._state.get("last_checked_at")

    def update_last_checked_at(self):
        """Call at the end of each engagement cycle to advance the watermark."""
        self._state["last_checked_at"] = datetime.now(timezone.utc).isoformat()
        self._save()
