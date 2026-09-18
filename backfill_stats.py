"""
One-time backfill / recount: sets competitions/{id}.stats.* from the EXISTING
competition_interactions documents, using the same rule the server uses:
one count per (student, event, competition) when the event field is set.

ORDER (important):
  1. Deploy the new app.py (Railway) FIRST. From then on the server keeps
     stats.* up to date itself.
  2. Run this script right after, ideally while nobody is using the app.
     It writes ABSOLUTE counts (never increments), so it is idempotent:
     running it twice gives the same numbers. Events the new server counted
     between step 1 and 2 are already included in the recount, because their
     interaction fields are already set.
  3. Only then publish the new admin.html.

Writes ONLY competitions/{id}.stats.{viewed,clicked,submitted,whatsappJoined}
via update() with dotted paths: no other field is touched, and no document is
ever created.

Do not call this from app.py. Do not run it on server startup.

Usage:
    FIREBASE_SERVICE_ACCOUNT_JSON='...' python backfill_stats.py
"""
import os
import json
from collections import defaultdict

import firebase_admin
from firebase_admin import credentials, firestore
from google.api_core.exceptions import NotFound

service_account_info = json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT_JSON"])
cred = credentials.Certificate(service_account_info)
firebase_admin.initialize_app(cred)
db = firestore.client()

FIELD_TO_STAT = {
    "viewedAt": "viewed",
    "clickedLinkAt": "clicked",
    "formSubmittedAt": "submitted",
    "whatsappJoinedAt": "whatsappJoined",
}


def zero_stats():
    return {"viewed": 0, "clicked": 0, "submitted": 0, "whatsappJoined": 0}


def main():
    counts = defaultdict(zero_stats)
    seen = defaultdict(set)  # (competitionId, statKey) -> student keys already counted
    total_docs = 0
    duplicates = 0

    for snap in db.collection("competition_interactions").stream():
        total_docs += 1
        d = snap.to_dict()
        comp_id = d.get("competitionId")
        if not comp_id:
            continue
        student_key = d.get("studentId") or snap.id
        for field, stat_key in FIELD_TO_STAT.items():
            if not d.get(field):
                continue
            marker = (comp_id, stat_key)
            if student_key in seen[marker]:
                duplicates += 1  # same student counted twice -> skip (server rule)
                continue
            seen[marker].add(student_key)
            counts[comp_id][stat_key] += 1

    comp_docs = list(db.collection("competitions").stream())
    comp_ids = {c.id for c in comp_docs}
    orphans = set(counts.keys()) - comp_ids
    print(f"Read {total_docs} interaction docs. Found {len(comp_docs)} competitions.")
    print(f"Duplicate (student,event,competition) entries skipped: {duplicates}")
    print(f"Interaction groups pointing to missing competitions (ignored): {len(orphans)}")

    written = 0
    for comp_snap in comp_docs:
        stats = counts.get(comp_snap.id) or zero_stats()  # no interactions -> zeros
        try:
            comp_snap.reference.update({f"stats.{k}": v for k, v in stats.items()})
            written += 1
            print(f"  {comp_snap.id}: {stats}")
        except NotFound:
            print(f"  {comp_snap.id}: deleted meanwhile, skipped")

    print(f"Backfill complete. Updated {written} competitions.")


if __name__ == "__main__":
    main()