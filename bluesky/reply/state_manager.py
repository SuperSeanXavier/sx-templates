import json
import os
from datetime import date
from pathlib import Path

_DEFAULT_PATH = Path(__file__).parent / "state.json"

_EMPTY = {
    "bot_status": "running",
    "replied_posts": [],
    "my_reply_uris": [],
    "dm_pulls_by_root": {},
    "conversation_depth": {},
    "daily_discounts": {},   # {date_str: count}
    "blocked_users": [],
    "paused_users": [],
}

MAX_CONVERSATION_DEPTH = int(os.environ.get("MAX_CONVERSATION_DEPTH", 3))
MAX_DISCOUNTS_PER_DAY = int(os.environ.get("MAX_DISCOUNTS_PER_DAY", 5))


class StateManager:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get("STATE_PATH", _DEFAULT_PATH))
        self._state = self._load()

    def _load(self):
        if self.path.exists():
            with open(self.path) as f:
                return json.load(f)
        return dict(_EMPTY)

    def _save(self):
        try:
            with open(self.path, "w") as f:
                json.dump(self._state, f, indent=2)
        except OSError as e:
            print(f"[state] warning: could not save to {self.path}: {e}")

    # --- dedup ---

    def has_replied(self, post_uri):
        return post_uri in self._state["replied_posts"]

    def mark_replied(self, post_uri):
        if post_uri not in self._state["replied_posts"]:
            self._state["replied_posts"].append(post_uri)
            self._save()

    # --- my reply URI tracking (for follow-up detection) ---

    def is_my_reply(self, uri):
        return uri in self._state.get("my_reply_uris", [])

    def add_my_reply(self, uri):
        if uri not in self._state.setdefault("my_reply_uris", []):
            self._state["my_reply_uris"].append(uri)
            self._save()

    # --- DM pull phrase tracking (per root post, prevents repetition) ---

    def get_dm_pulls(self, root_uri):
        return self._state.setdefault("dm_pulls_by_root", {}).get(root_uri, [])

    def add_dm_pull(self, root_uri, text):
        pulls = self._state.setdefault("dm_pulls_by_root", {}).setdefault(root_uri, [])
        pulls.append(text)
        self._save()

    # --- conversation depth (per root post) ---

    def get_depth(self, root_uri):
        return self._state.setdefault("conversation_depth", {}).get(root_uri, 0)

    def increment_depth(self, root_uri):
        depths = self._state.setdefault("conversation_depth", {})
        depths[root_uri] = depths.get(root_uri, 0) + 1
        self._save()

    def at_max_depth(self, root_uri):
        return self.get_depth(root_uri) >= MAX_CONVERSATION_DEPTH

    # --- daily discount cap ---

    def discount_allowed(self):
        today = str(date.today())
        used = self._state.setdefault("daily_discounts", {}).get(today, 0)
        return used < MAX_DISCOUNTS_PER_DAY

    def record_discount(self):
        today = str(date.today())
        counts = self._state.setdefault("daily_discounts", {})
        counts[today] = counts.get(today, 0) + 1
        self._save()

    # --- blocklist ---

    def is_blocked(self, handle):
        return handle in self._state["blocked_users"]

    def block_user(self, handle):
        if handle not in self._state["blocked_users"]:
            self._state["blocked_users"].append(handle)
            self._save()

    def unblock_user(self, handle):
        self._state["blocked_users"] = [
            h for h in self._state["blocked_users"] if h != handle
        ]
        self._save()

    # --- per-user pause ---

    def is_paused_user(self, handle):
        return handle in self._state["paused_users"]

    def pause_user(self, handle):
        if handle not in self._state["paused_users"]:
            self._state["paused_users"].append(handle)
            self._save()

    def resume_user(self, handle):
        self._state["paused_users"] = [
            h for h in self._state["paused_users"] if h != handle
        ]
        self._save()

    # --- global bot status ---

    def get_status(self):
        return self._state.get("bot_status", "running")

    def set_status(self, status):
        self._state["bot_status"] = status
        self._save()

    # --- info ---

    def summary(self):
        return {
            "bot_status": self.get_status(),
            "replied_count": len(self._state["replied_posts"]),
            "blocked_users": self._state["blocked_users"],
            "paused_users": self._state["paused_users"],
        }
