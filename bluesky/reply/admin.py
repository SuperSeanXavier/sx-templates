"""
Admin CLI for the Bluesky reply bot.

Usage:
    python bluesky/reply/admin.py status
    python bluesky/reply/admin.py pause-all
    python bluesky/reply/admin.py resume
    python bluesky/reply/admin.py pause-user @handle
    python bluesky/reply/admin.py block-user @handle
    python bluesky/reply/admin.py unblock-user @handle
    python bluesky/reply/admin.py clear-handoff @handle
"""
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from bluesky.reply.state_manager import StateManager
from bluesky.shared.firestore_client import db


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    state = StateManager()
    cmd = sys.argv[1]

    if cmd == "status":
        s = state.summary()
        print(f"Status:        {s['bot_status']}")
        print(f"Replied to:    {s['replied_count']} post(s)")
        print(f"Blocked users: {s['blocked_users'] or 'none'}")
        print(f"Paused users:  {s['paused_users'] or 'none'}")

    elif cmd == "pause-all":
        state.set_status("paused")
        print("Bot paused.")

    elif cmd == "resume":
        state.set_status("running")
        print("Bot resumed.")

    elif cmd == "pause-user":
        handle = sys.argv[2].lstrip("@")
        state.pause_user(handle)
        print(f"Paused replies to @{handle}.")

    elif cmd == "block-user":
        handle = sys.argv[2].lstrip("@")
        state.block_user(handle)
        print(f"Blocked @{handle}.")

    elif cmd == "unblock-user":
        handle = sys.argv[2].lstrip("@")
        state.unblock_user(handle)
        print(f"Unblocked @{handle}.")

    elif cmd == "clear-handoff":
        handle = sys.argv[2].lstrip("@")
        doc_ref = db.collection("conversations").document(handle)
        if not doc_ref.get().exists:
            print(f"No conversation record found for @{handle}.")
            sys.exit(1)
        doc_ref.update({"human_handoff": False, "handoff_reason": None})
        print(f"Handoff cleared for @{handle}. Automated replies resumed.")

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
