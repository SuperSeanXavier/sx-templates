"""
Audience discovery — ranked target account lists stored in Firestore.

discover_starter_packs(client, domain_keywords, domains)
    Search Bluesky starter packs by keyword, score members, write to
    Firestore `target_accounts`. Run weekly.

prefetch_fan_profiles(client, creator_handle, cap)
    Phase A of follower graph analysis. Fetch all fan (follower) profiles,
    compute mean + std dev of their followers_count, filter to within 1 std
    dev of the mean (removes bots and mega-influencers), sort by
    followers_count descending, store ordered DID list to
    Firestore _system/follower_graph_state. Run weekly.

analyze_follower_graph_slot(client, creator_handle, slot, slot_size, followee_cap, top_pct)
    Phase B of follower graph analysis. Reads the pre-filtered fan DID list
    from _system/follower_graph_state, takes a 2000-fan slice for this slot,
    fetches each fan's followees, sorts followees by their own followers_count
    descending, takes the top top_pct fraction, and frequency-counts those
    followees. Upserts results into `target_accounts`. Cross-slot counts
    accumulate via follower_graph_count + follower_graph_checked fields.
    Run nightly across 5 staggered slots (40-min spacing).

analyze_follower_graph(client, creator_handle, follower_cap, followee_cap)
    Legacy single-pass analysis. Retained for manual runs and fallback.
    Replaced in production by prefetch + slot pipeline.

score_and_tier()
    Pure Firestore pass: assign tiers based on discovery_sources.
      Both sources  → Tier 1
      follower_graph only → Tier 2
      starter_pack only   → Tier 3
    Updates `tier` and `score` fields. Deduplicate by DID.
    Run after all nightly slots complete (5:30am).

Scoring scales (0–100):
    starter_pack : log10(followers) * 15 (max 60) + bio keyword matches * 10 (max 40)
    follower_graph: appearance_count / followers_checked * 100
"""
import math
import statistics
import time
from datetime import datetime, timezone

from google.cloud.firestore_v1.base_query import FieldFilter as _filter
from bluesky.shared.firestore_client import db
from bluesky.shared.rate_limiter import check_read, RateLimitError

_TARGET = db.collection("target_accounts")
_GRAPH_STATE = db.collection("_system").document("follower_graph_state")

# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _pack_score(profile, domain_keywords):
    """
    Score a profile found via starter pack discovery (0–100).
    follower_count contributes up to 60; bio keyword matches up to 40.
    """
    followers = getattr(profile, "followers_count", 0) or 0
    score = min(60.0, math.log10(max(followers, 1)) * 15)

    bio = (getattr(profile, "description", "") or "").lower()
    kw_hits = sum(1 for kw in domain_keywords if kw.lower() in bio)
    score += min(40.0, kw_hits * 10)

    return round(score, 1)


def _graph_score(appearance_count, total_checked):
    """Score a profile found via follower graph (0–100, frequency-based)."""
    if total_checked == 0:
        return 0.0
    return round(appearance_count / total_checked * 100, 1)


def _profile_doc(profile):
    """Extract stable fields from a Bluesky ProfileView."""
    return {
        "handle": getattr(profile, "handle", ""),
        "display_name": getattr(profile, "display_name", "") or "",
        "follower_count": getattr(profile, "followers_count", 0) or 0,
        "bio": (getattr(profile, "description", "") or "")[:500],
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# 7a — Starter pack discovery
# ---------------------------------------------------------------------------

def _fetch_list_members(client, list_uri, cap=500):
    """Paginate through a Bluesky list; return up to `cap` ProfileViews."""
    members = []
    cursor = None
    while len(members) < cap:
        try:
            check_read()
        except RateLimitError as e:
            print(f"  [rate] {e} — pausing 60s")
            time.sleep(60)
            continue

        try:
            resp = client.get_list_members_page(list_uri, limit=100, cursor=cursor)
        except Exception as e:
            print(f"  [warn] list fetch failed: {e}")
            break

        items = getattr(resp, "items", []) or []
        for item in items:
            subject = getattr(item, "subject", None)
            if subject:
                members.append(subject)

        cursor = getattr(resp, "cursor", None)
        if not cursor or not items:
            break

    return members[:cap]


def discover_starter_packs(client, domain_keywords, domains=None, pack_limit=10, member_cap=500):
    """
    Search Bluesky starter packs using each keyword in `domain_keywords`,
    score the members, and upsert them into Firestore `target_accounts`.

    Args:
        client:          BlueskyClient (logged in)
        domain_keywords: list of search query strings (e.g. ["gay fitness", "muscle"])
        domains:         list of domain tags to attach (e.g. ["fitness", "muscle"])
        pack_limit:      max starter packs to process per keyword
        member_cap:      max members to fetch per pack
    """
    domains = domains or []
    seen_pack_uris = set()
    members_evaluated = 0
    docs_created = 0
    docs_updated = 0

    print(f"[discovery] starter pack search — keywords: {domain_keywords}")

    for keyword in domain_keywords:
        print(f"  [search] '{keyword}'")
        try:
            check_read()
            resp = client.search_starter_packs(keyword, limit=pack_limit)
        except RateLimitError as e:
            print(f"  [rate] {e}")
            time.sleep(60)
            continue
        except Exception as e:
            print(f"  [warn] search failed for '{keyword}': {e}")
            continue

        packs = getattr(resp, "starter_packs", []) or []
        print(f"    {len(packs)} pack(s) found")

        for pack_stub in packs:
            pack_uri = getattr(pack_stub, "uri", None)
            if not pack_uri or pack_uri in seen_pack_uris:
                continue
            seen_pack_uris.add(pack_uri)

            # Resolve pack to get its member list URI
            try:
                check_read()
                pack_detail = client.get_starter_pack(pack_uri)
            except RateLimitError as e:
                print(f"  [rate] {e}")
                time.sleep(60)
                continue
            except Exception as e:
                print(f"  [warn] pack fetch failed ({pack_uri}): {e}")
                continue

            sp = getattr(pack_detail, "starter_pack", None)
            list_uri = getattr(getattr(sp, "list", None), "uri", None)
            if not list_uri:
                continue

            pack_name = getattr(sp, "record", None)
            pack_name = getattr(pack_name, "name", pack_uri) if pack_name else pack_uri
            print(f"    [pack] {pack_name} — fetching members...")

            members = _fetch_list_members(client, list_uri, cap=member_cap)
            print(f"      {len(members)} member(s)")

            for profile in members:
                did = getattr(profile, "did", None)
                if not did:
                    continue

                members_evaluated += 1
                score = _pack_score(profile, domain_keywords)
                doc_ref = _TARGET.document(did)
                existing = doc_ref.get()

                if existing.exists:
                    data = existing.to_dict()
                    sources = set(data.get("discovery_sources", []))
                    sources.add("starter_pack")
                    existing_domains = set(data.get("domains", []))
                    existing_domains.update(domains)
                    # Keep the higher pack score if already discovered via starter pack
                    new_score = max(data.get("starter_pack_score", 0), score)
                    doc_ref.update({
                        "discovery_sources": list(sources),
                        "domains": list(existing_domains),
                        "starter_pack_score": new_score,
                        "last_updated": datetime.now(timezone.utc).isoformat(),
                        **_profile_doc(profile),
                    })
                    docs_updated += 1
                else:
                    doc_ref.set({
                        **_profile_doc(profile),
                        "did": did,
                        "discovery_sources": ["starter_pack"],
                        "domains": domains,
                        "starter_pack_score": score,
                        "follower_graph_score": 0.0,
                        "score": score,
                        "tier": 3,
                    })
                    docs_created += 1

    print(f"[discovery] starter pack discovery complete — {len(seen_pack_uris)} pack(s) processed")
    return {
        "packs_processed": len(seen_pack_uris),
        "members_evaluated": members_evaluated,
        "docs_created": docs_created,
        "docs_updated": docs_updated,
    }


# ---------------------------------------------------------------------------
# 7b — Follower graph analysis
# ---------------------------------------------------------------------------

def _fetch_all_followers(client, actor, cap):
    """Paginate creator's followers up to `cap`. Returns list of ProfileView."""
    followers = []
    cursor = None

    while len(followers) < cap:
        try:
            check_read()
        except RateLimitError as e:
            print(f"  [rate] {e} — pausing 60s")
            time.sleep(60)
            continue

        try:
            resp = client.get_followers_page(actor, limit=100, cursor=cursor)
        except Exception as e:
            print(f"  [warn] get_followers failed: {e}")
            break

        batch = getattr(resp, "followers", []) or []
        followers.extend(batch)
        cursor = getattr(resp, "cursor", None)
        if not cursor or not batch:
            break

    return followers[:cap]


def _fetch_followee_dids(client, actor_did, cap):
    """Fetch the DIDs of accounts that `actor_did` follows, up to `cap`."""
    dids = []
    cursor = None

    while len(dids) < cap:
        try:
            check_read()
        except RateLimitError as e:
            time.sleep(60)
            continue

        try:
            resp = client.get_follows_page(actor_did, limit=100, cursor=cursor)
        except Exception:
            break

        batch = getattr(resp, "follows", []) or []
        for f in batch:
            did = getattr(f, "did", None)
            if did:
                dids.append(did)

        cursor = getattr(resp, "cursor", None)
        if not cursor or not batch:
            break

    return dids[:cap]


# ---------------------------------------------------------------------------
# Phase A — prefetch and filter fan profiles
# ---------------------------------------------------------------------------

def prefetch_fan_profiles(client, creator_handle, cap=10000):
    """
    Fetch all fan (follower) profiles, compute mean + std dev of their
    followers_count, filter to accounts within 1 std dev of the mean
    (removes bots with near-zero followers and mega-influencers whose
    followee lists are too broad to be useful signals), sort by
    followers_count descending, and persist the ordered DID list to
    Firestore _system/follower_graph_state.

    Run weekly — follower composition shifts slowly.
    """
    print(f"[discovery] prefetch fan profiles — @{creator_handle} (cap: {cap})")

    all_followers = _fetch_all_followers(client, creator_handle, cap)
    print(f"  fetched {len(all_followers)} follower(s)")

    if not all_followers:
        print("  [warn] no followers found — aborting prefetch")
        return

    # Collect DID + followers_count pairs
    fan_data = []
    for f in all_followers:
        did = getattr(f, "did", None)
        if not did:
            continue
        count = getattr(f, "followers_count", 0) or 0
        fan_data.append((did, count))

    counts = [c for _, c in fan_data]
    mean = statistics.mean(counts)
    std = statistics.stdev(counts) if len(counts) > 1 else 0.0
    lower = max(0.0, mean - std)
    upper = mean + std

    print(f"  followers_count stats: mean={mean:.1f}, std={std:.1f}, band=[{lower:.1f}, {upper:.1f}]")

    # Filter to 1-std-dev band and sort by followers_count descending
    filtered = [(did, cnt) for did, cnt in fan_data if lower <= cnt <= upper]
    filtered.sort(key=lambda x: -x[1])

    raw_count = len(fan_data)
    filtered_count = len(filtered)
    print(f"  {filtered_count} fan(s) within band (of {raw_count} total, {raw_count - filtered_count} excluded)")

    _GRAPH_STATE.set({
        "fan_dids": [did for did, _ in filtered],
        "fan_count_raw": raw_count,
        "fan_count_filtered": filtered_count,
        "mean_followers": round(mean, 2),
        "std_followers": round(std, 2),
        "lower_bound": round(lower, 2),
        "upper_bound": round(upper, 2),
        "last_prefetch": datetime.now(timezone.utc).isoformat(),
    })

    print(f"[discovery] prefetch complete — {filtered_count} fan DID(s) stored")
    return {
        "fans_fetched": raw_count,
        "fans_in_band": filtered_count,
        "fans_excluded": raw_count - filtered_count,
        "mean_followers": round(mean, 2),
        "std_followers": round(std, 2),
        "lower_bound": round(lower, 2),
        "upper_bound": round(upper, 2),
    }


def _fetch_followee_profiles(client, actor_did, cap):
    """
    Fetch ProfileView objects for accounts that actor_did follows, up to cap.
    Unlike _fetch_followee_dids, retains the full ProfileView so callers can
    sort by followers_count without additional API calls.
    """
    profiles = []
    cursor = None

    while len(profiles) < cap:
        try:
            check_read()
        except RateLimitError as e:
            time.sleep(60)
            continue

        try:
            resp = client.get_follows_page(actor_did, limit=100, cursor=cursor)
        except Exception:
            break

        batch = getattr(resp, "follows", []) or []
        profiles.extend(batch)

        cursor = getattr(resp, "cursor", None)
        if not cursor or not batch:
            break

    return profiles[:cap]


# ---------------------------------------------------------------------------
# Phase B — slotted follower graph analysis
# ---------------------------------------------------------------------------

def analyze_follower_graph_slot(client, creator_handle, slot=0, slot_size=2000,
                                followee_cap=500, top_pct=0.20):
    """
    Process one slot of the pre-filtered fan list from
    _system/follower_graph_state.

    For each fan in the slot:
      1. Fetch their followees (ProfileView includes followers_count).
      2. Sort followees by followers_count descending.
      3. Take the top top_pct fraction (e.g. 20% = the accounts they
         consider most worth following).
      4. Add those DIDs to the frequency counter.

    Threshold: accounts appearing in <1% of this slot's fans are excluded.
    Cross-slot accumulation: existing target_account docs have their
    follower_graph_count and follower_graph_checked incremented, keeping the
    cumulative score accurate across all 5 nightly slots.

    Args:
        client:           BlueskyClient (logged in)
        creator_handle:   Creator's handle (excluded from results)
        slot:             0-indexed slot number (0–4 for 10k fans)
        slot_size:        fans per slot (default 2000)
        followee_cap:     max followees to fetch per fan (default 500)
        top_pct:          fraction of followees to count after sorting (default 0.20)
    """
    print(f"[discovery] follower graph slot {slot} — "
          f"top {top_pct * 100:.0f}% of followees by followers_count")

    state_doc = _GRAPH_STATE.get()
    if not state_doc.exists:
        print("  [error] follower_graph_state not found — run prefetch first")
        return

    fan_dids = state_doc.to_dict().get("fan_dids", [])
    start = slot * slot_size
    end = start + slot_size
    slot_dids = fan_dids[start:end]

    if not slot_dids:
        print(f"  [info] slot {slot} is empty — nothing to process")
        return

    print(f"  fans [{start}:{end}] — {len(slot_dids)} fan(s) in this slot")

    # Exclude the creator from results
    creator_did = None
    try:
        p = client.get_profile(creator_handle)
        creator_did = getattr(p, "did", None)
    except Exception:
        pass

    # Frequency count: DID → number of fans whose top-X% includes this account
    frequency: dict[str, int] = {}

    for i, fan_did in enumerate(slot_dids):
        profiles = _fetch_followee_profiles(client, fan_did, followee_cap)
        if not profiles:
            continue

        # Sort by followee's own follower count descending
        profiles.sort(key=lambda p: getattr(p, "followers_count", 0) or 0, reverse=True)

        # Take top X%
        top_n = max(1, math.ceil(len(profiles) * top_pct))
        for prof in profiles[:top_n]:
            did = getattr(prof, "did", None)
            if did and did != creator_did:
                frequency[did] = frequency.get(did, 0) + 1

        if (i + 1) % 100 == 0:
            print(f"  processed {i + 1}/{len(slot_dids)} fans — "
                  f"{len(frequency)} unique followees so far")

    total_checked = len(slot_dids)
    print(f"  {len(frequency)} unique followees across {total_checked} fan(s)")

    # Per-slot threshold: must appear in ≥1% of this slot's fans
    threshold = max(2, int(total_checked * 0.01))
    candidates = {did: cnt for did, cnt in frequency.items() if cnt >= threshold}
    print(f"  {len(candidates)} candidate(s) above threshold ({threshold} appearances)")

    docs_created = 0
    docs_updated = 0

    for did, count in sorted(candidates.items(), key=lambda x: -x[1]):
        doc_ref = _TARGET.document(did)
        existing = doc_ref.get()

        if existing.exists:
            data = existing.to_dict()
            sources = set(data.get("discovery_sources", []))
            sources.add("follower_graph")
            # Accumulate counts across slots for accurate cross-slot scoring
            prev_count = data.get("follower_graph_count", 0)
            prev_checked = data.get("follower_graph_checked", 0)
            new_count = prev_count + count
            new_checked = prev_checked + total_checked
            new_score = _graph_score(new_count, new_checked)
            doc_ref.update({
                "discovery_sources": list(sources),
                "follower_graph_score": new_score,
                "follower_graph_count": new_count,
                "follower_graph_checked": new_checked,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            })
            docs_updated += 1
        else:
            # Fetch profile to populate display fields
            try:
                check_read()
                profile = client.get_profile(did)
            except RateLimitError:
                time.sleep(60)
                try:
                    profile = client.get_profile(did)
                except Exception:
                    continue
            except Exception:
                continue

            score = _graph_score(count, total_checked)
            doc_ref.set({
                **_profile_doc(profile),
                "did": did,
                "discovery_sources": ["follower_graph"],
                "domains": [],
                "starter_pack_score": 0.0,
                "follower_graph_score": score,
                "follower_graph_count": count,
                "follower_graph_checked": total_checked,
                "score": score,
                "tier": 2,
            })
            docs_created += 1

    print(f"[discovery] slot {slot} complete — {len(candidates)} account(s) written")
    return {
        "slot": slot,
        "fans_processed": total_checked,
        "unique_followees": len(frequency),
        "candidates_above_threshold": len(candidates),
        "docs_created": docs_created,
        "docs_updated": docs_updated,
    }


def analyze_follower_graph(client, creator_handle, follower_cap=2000, followee_cap=500):
    """
    For each of the creator's followers (up to follower_cap), fetch who they
    follow (up to followee_cap each). Count how often each followee appears.
    High-frequency accounts → strong Tier 2 candidates.

    Writes/merges into Firestore `target_accounts` with source=follower_graph.
    """
    print(f"[discovery] follower graph analysis — @{creator_handle} (cap: {follower_cap} followers)")

    # Step 1: get creator's followers
    followers = _fetch_all_followers(client, creator_handle, follower_cap)
    print(f"  fetched {len(followers)} follower(s)")

    # Step 2: for each follower, collect who they follow
    frequency: dict[str, int] = {}        # did → count
    follower_profiles: dict[str, object] = {}  # did → ProfileView (for writing later)

    for i, follower in enumerate(followers):
        follower_did = getattr(follower, "did", None)
        if not follower_did:
            continue

        followee_dids = _fetch_followee_dids(client, follower_did, followee_cap)

        for did in followee_dids:
            frequency[did] = frequency.get(did, 0) + 1

        if (i + 1) % 100 == 0:
            print(f"  processed {i + 1}/{len(followers)} followers — {len(frequency)} unique followees so far")

    # Remove the creator themselves
    try:
        creator_profile = client.get_profile(creator_handle)
        frequency.pop(getattr(creator_profile, "did", None), None)
    except Exception:
        pass

    total_checked = len(followers)
    print(f"  {len(frequency)} unique followees across {total_checked} follower(s)")

    # Step 3: fetch profiles for top candidates and write to Firestore
    # Only persist accounts that appeared in ≥1% of followers (signal threshold)
    threshold = max(2, int(total_checked * 0.01))
    candidates = {did: cnt for did, cnt in frequency.items() if cnt >= threshold}
    print(f"  {len(candidates)} candidate(s) above threshold ({threshold} appearances)")

    for did, count in sorted(candidates.items(), key=lambda x: -x[1]):
        score = _graph_score(count, total_checked)

        doc_ref = _TARGET.document(did)
        existing = doc_ref.get()

        if existing.exists:
            data = existing.to_dict()
            sources = set(data.get("discovery_sources", []))
            sources.add("follower_graph")
            doc_ref.update({
                "discovery_sources": list(sources),
                "follower_graph_score": score,
                "follower_graph_count": count,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            })
        else:
            # Fetch profile to populate doc — rate-limited
            try:
                check_read()
                profile = client.get_profile(did)
            except RateLimitError as e:
                print(f"  [rate] {e} — pausing 60s")
                time.sleep(60)
                try:
                    profile = client.get_profile(did)
                except Exception:
                    continue
            except Exception:
                continue

            doc_ref.set({
                **_profile_doc(profile),
                "did": did,
                "discovery_sources": ["follower_graph"],
                "domains": [],
                "starter_pack_score": 0.0,
                "follower_graph_score": score,
                "follower_graph_count": count,
                "score": score,
                "tier": 2,
            })

    print(f"[discovery] follower graph complete — {len(candidates)} account(s) written")


# ---------------------------------------------------------------------------
# 7c — Tier assignment
# ---------------------------------------------------------------------------

def score_and_tier():
    """
    Read all target_accounts, assign tiers and combined scores.

      Both sources  → Tier 1, score = average(starter_pack_score, follower_graph_score)
      follower_graph only → Tier 2, score = follower_graph_score
      starter_pack only   → Tier 3, score = starter_pack_score

    Deduplication is implicit: DID is the document ID.
    """
    print("[discovery] running score_and_tier...")
    docs = list(_TARGET.stream())
    print(f"  {len(docs)} account(s) in target_accounts")

    updated = 0
    tier_counts = {1: 0, 2: 0, 3: 0}

    for doc in docs:
        data = doc.to_dict()
        sources = set(data.get("discovery_sources", []))
        sp_score = data.get("starter_pack_score", 0.0)
        fg_score = data.get("follower_graph_score", 0.0)

        if "starter_pack" in sources and "follower_graph" in sources:
            tier = 1
            score = round((sp_score + fg_score) / 2, 1)
        elif "follower_graph" in sources:
            tier = 2
            score = fg_score
        else:
            tier = 3
            score = sp_score

        _TARGET.document(doc.id).update({
            "tier": tier,
            "score": score,
        })
        updated += 1
        tier_counts[tier] = tier_counts.get(tier, 0) + 1

    print(f"[discovery] score_and_tier complete — {updated} account(s) updated")
    return {
        "accounts_scored": updated,
        "tier1": tier_counts.get(1, 0),
        "tier2": tier_counts.get(2, 0),
        "tier3": tier_counts.get(3, 0),
    }
