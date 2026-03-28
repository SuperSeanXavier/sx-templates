"""
Firestore client — single shared instance for all components.

Usage:
    from bluesky.shared.firestore_client import db
    db.collection("seen_events").document(uri).set({"seen_at": ...})
"""
import os
from google.cloud import firestore

_client = None


def _get_client():
    global _client
    if _client is None:
        project = os.environ.get("GOOGLE_CLOUD_PROJECT", "sx-platform")
        database = os.environ.get("FIRESTORE_DATABASE", "sxplatformdatabase")
        _client = firestore.Client(project=project, database=database)
    return _client


# Module-level alias — import this directly
db = _get_client()
