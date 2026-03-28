import os
from datetime import datetime, timezone
from atproto import Client as AtprotoClient, models

_GRAPHEME_LIMIT = 295  # Bluesky hard cap is 300; 295 leaves margin for edge cases


def _truncate(text, limit=_GRAPHEME_LIMIT):
    """Truncate to at most `limit` characters, appending … if cut. Safe for posts and DMs."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "\u2026"


_SESSION_DOC_PATH = ("_system", "bluesky_session")


class BlueskyClient:
    def __init__(self):
        self.handle = os.environ["BLUESKY_HANDLE"]
        self._password = os.environ["BLUESKY_APP_PASSWORD"]
        self._client = AtprotoClient()
        self._my_did = None

    def login(self):
        """
        Log in, reusing a stored session when available.

        Session string is persisted in Firestore _system/bluesky_session so
        Cloud Function invocations share a single session instead of each
        calling createSession (limit: 300/day). The atproto SDK transparently
        refreshes the access token when it expires (~2 hrs) using the stored
        refresh token (~90 day lifetime) — that refresh does not count against
        the createSession quota.

        A full createSession call only happens when:
          - No stored session exists (first ever run), or
          - The refresh token has expired (roughly every 90 days).
        """
        from bluesky.shared.firestore_client import db

        session_doc = db.collection(_SESSION_DOC_PATH[0]).document(_SESSION_DOC_PATH[1])
        restored = False

        doc = session_doc.get()
        if doc.exists:
            session_str = (doc.to_dict() or {}).get("session_string", "")
            if session_str:
                try:
                    self._client.import_session_string(session_str)
                    self._my_did = self._client.me.did
                    restored = True
                    print("[auth] session restored from Firestore")
                except Exception as e:
                    print(f"[auth] session restore failed ({e}), falling back to full login")

        if not restored:
            self._client.login(self.handle, self._password)
            self._my_did = self._client.me.did
            print("[auth] full login (createSession)")

        # Persist current session string (may include refreshed tokens)
        session_doc.set({
            "session_string": self._client.export_session_string(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

        self._chat = self._client.with_bsky_chat_proxy()
        return self

    def get_reply_notifications(self, max_results=200):
        """Return reply notifications, paginated up to max_results."""
        notifs = []
        cursor = None
        while len(notifs) < max_results:
            params = {"limit": min(100, max_results - len(notifs))}
            if cursor:
                params["cursor"] = cursor
            response = self._client.app.bsky.notification.list_notifications(params=params)
            page = [n for n in (response.notifications or []) if n.reason == "reply"]
            notifs.extend(page)
            cursor = getattr(response, "cursor", None)
            if not cursor or not response.notifications:
                break
        return notifs

    def get_engagement_notifications(self, since=None, max_results=200):
        """
        Return like, repost, and follow notifications, paginated up to max_results.

        since: ISO timestamp string — only return notifications newer than this.
               Pagination stops as soon as a page contains no qualifying notifications
               (notifications are newest-first, so once we pass `since` we can stop).
        """
        notifs = []
        cursor = None
        while len(notifs) < max_results:
            params = {"limit": min(100, max_results - len(notifs))}
            if cursor:
                params["cursor"] = cursor
            response = self._client.app.bsky.notification.list_notifications(params=params)
            page = [n for n in (response.notifications or []) if n.reason in ("like", "repost", "follow")]
            if since:
                page = [n for n in page if n.indexed_at > since]
                # Once we've filtered out all results on a page, we've passed the watermark
                if not page and response.notifications:
                    break
            notifs.extend(page)
            cursor = getattr(response, "cursor", None)
            if not cursor or not response.notifications:
                break
        return notifs

    def get_post(self, uri):
        """Fetch a post by URI. Returns the PostView."""
        response = self._client.app.bsky.feed.get_post_thread(params={"uri": uri})
        return response.thread.post

    def get_profile(self, handle):
        """Fetch a full profile (includes viewer.following for mutual-follow check)."""
        return self._client.app.bsky.actor.get_profile(params={"actor": handle})

    def post_reply(self, text, parent_uri, parent_cid, root_uri, root_cid):
        """Post a reply to a given parent, anchored to the thread root."""
        reply_ref = models.AppBskyFeedPost.ReplyRef(
            root=models.ComAtprotoRepoStrongRef.Main(uri=root_uri, cid=root_cid),
            parent=models.ComAtprotoRepoStrongRef.Main(uri=parent_uri, cid=parent_cid),
        )
        return self._client.send_post(text=_truncate(text), reply_to=reply_ref)

    # --- DM (chat) methods ---

    def get_dm_convo_status(self, handle):
        """
        Get DM conversation status with a user.

        Returns a dict:
          convo_id          — str, the conversation ID
          last_sender       — "me" | "them" | None  (None = no messages yet)
          consecutive_mine  — int, consecutive messages from me at end of thread
          last_their_message — str | None, most recent message text from them

        Note: Bluesky DMs route through chat.bsky.app via self._chat (proxied client).
        """
        profile = self.get_profile(handle)
        their_did = profile.did

        # Get or create the DM conversation
        convo_response = self._chat.chat.bsky.convo.get_convo_for_members(
            {"members": [self._my_did, their_did]}
        )
        convo_id = convo_response.convo.id

        # Fetch recent messages (returns newest first)
        msgs_response = self._chat.chat.bsky.convo.get_messages(
            {"convo_id": convo_id, "limit": 20}
        )
        messages = getattr(msgs_response, "messages", []) or []

        if not messages:
            return {
                "convo_id": convo_id,
                "last_sender": None,
                "consecutive_mine": 0,
                "last_their_message": None,
            }

        # Walk messages newest-first: count consecutive mine, find their last message
        consecutive_mine = 0
        counting = True
        last_their_message = None

        for msg in messages:
            sender_did = getattr(getattr(msg, "sender", None), "did", None)
            is_mine = sender_did == self._my_did

            if counting:
                if is_mine:
                    consecutive_mine += 1
                else:
                    counting = False

            if not is_mine and last_their_message is None:
                last_their_message = getattr(msg, "text", None)

        last_sender = "me" if getattr(getattr(messages[0], "sender", None), "did", None) == self._my_did else "them"

        return {
            "convo_id": convo_id,
            "last_sender": last_sender,
            "consecutive_mine": consecutive_mine,
            "last_their_message": last_their_message,
        }

    def send_dm(self, convo_id, text):
        """Send a DM in an existing conversation."""
        return self._chat.chat.bsky.convo.send_message({
            "convo_id": convo_id,
            "message": {"$type": "chat.bsky.convo.defs#messageInput", "text": _truncate(text)},
        })

    def list_convos(self, limit=100, cursor=None):
        """
        List DM conversations sorted by most recent activity.

        Each ConvoView includes:
          .id            — conversation ID (use directly with send_dm)
          .unread_count  — messages from the other party not yet read
          .last_message  — most recent MessageView (.text, .sender.did, .sent_at)
          .members       — list of ProfileViewBasic (.did, .handle)

        Paginate by passing the returned .cursor back in.
        """
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._chat.chat.bsky.convo.list_convos(params=params)

    # --- Discovery methods ---

    def get_author_feed(self, actor, limit=10):
        """Fetch recent posts by actor. Returns raw response with .feed (list of FeedViewPost)."""
        return self._client.app.bsky.feed.get_author_feed(
            params={"actor": actor, "limit": limit, "filter": "posts_no_replies"}
        )

    def get_followers_page(self, actor, limit=100, cursor=None):
        """
        Fetch one page of an account's followers.
        Returns raw response with .followers (list of ProfileView) and .cursor.
        """
        params = {"actor": actor, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._client.app.bsky.graph.get_followers(params=params)

    def get_follows_page(self, actor, limit=100, cursor=None):
        """
        Fetch one page of accounts that `actor` follows.
        Returns raw response with .follows (list of ProfileView) and .cursor.
        """
        params = {"actor": actor, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._client.app.bsky.graph.get_follows(params=params)

    def search_starter_packs(self, query, limit=25):
        """
        Search for starter packs by keyword.
        Returns raw response with .starter_packs (list of StarterPackViewBasic).
        """
        return self._client.app.bsky.graph.search_starter_packs(
            params={"q": query, "limit": limit}
        )

    def get_starter_pack(self, uri):
        """
        Fetch a starter pack by URI.
        Returns raw response with .starter_pack (StarterPackView) which has .list.uri.
        """
        return self._client.app.bsky.graph.get_starter_pack(
            params={"starterPack": uri}
        )

    def get_list_members_page(self, list_uri, limit=100, cursor=None):
        """
        Fetch one page of members from a Bluesky list.
        Returns raw response with .items (list of ListItemView, .subject = ProfileView) and .cursor.
        """
        params = {"list": list_uri, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._client.app.bsky.graph.get_list(params=params)
