"""
University Competitions Hub — Backend (Flask, deployed on Railway)

Responsibilities:
- Verify Firebase ID tokens sent from the frontend
- Send push notifications via Firebase Cloud Messaging when a new
  competition is published (kept server-side so the FCM server key
  never reaches the browser)
- Receive and store interaction events (view / click / submit / follow-up),
  including the in-app team registration form data sent on "submit"
- Restrict admin-only routes

Requires environment variables (set these in Railway, never commit them):
  FIREBASE_SERVICE_ACCOUNT_JSON  -> full service account JSON as a string
  ALLOWED_EMAIL_DOMAIN           -> e.g. "std.eng.edu.eg" (your university domain)
"""

import os
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify
from flask_cors import CORS
import firebase_admin
from firebase_admin import credentials, auth, firestore, messaging

app = Flask(__name__)
CORS(app)

# ---------- Firebase Admin init ----------
service_account_info = json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT_JSON"])
cred = credentials.Certificate(service_account_info)
firebase_admin.initialize_app(cred)
db = firestore.client()

ALLOWED_DOMAIN = os.environ.get("ALLOWED_EMAIL_DOMAIN", "feng.bu.edu.eg")


def verify_request_user():
    """Verify the Firebase ID token sent in the Authorization header.
    Returns the decoded token dict, or None if invalid."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header.split(" ", 1)[1]
    try:
        decoded = auth.verify_id_token(token)
        email = decoded.get("email", "").lower()
        if ALLOWED_DOMAIN and not email.endswith("@" + ALLOWED_DOMAIN.lower()):
            return None
        return decoded
    except Exception:
        return None


def require_admin(decoded_token):
    uid = decoded_token["uid"]
    user_doc = db.collection("users").document(uid).get()
    return user_doc.exists and user_doc.to_dict().get("role") == "admin"


# ---------- Health check (Railway) ----------
@app.route("/")
def health():
    return jsonify({"status": "ok"})


# ---------- Create / publish a competition (admin only) ----------
@app.route("/api/competitions", methods=["POST"])
def create_competition():
    decoded = verify_request_user()
    if not decoded:
        return jsonify({"error": "unauthorized"}), 401
    if not require_admin(decoded):
        return jsonify({"error": "forbidden"}), 403

    data = request.get_json()
    competition_ref = db.collection("competitions").document()
    competition_ref.set({
        "title": data["title"],
        "description": data["description"],
        "link": data["link"],
        "attachmentUrl": data.get("attachmentUrl"),
        "attachmentName": data.get("attachmentName"),
        "whatsappGroupLink": data.get("whatsappGroupLink"),
        # "deadline" is the overall competition deadline (e.g. results/event
        # date). "applicationDeadline" is when registration/applying closes —
        # it's tracked separately so admins can renew/extend it on its own
        # (see PATCH /api/competitions/<id>/application-deadline) without
        # touching the competition's overall deadline.
        "deadline": data["deadline"],
        "applicationDeadline": data.get("applicationDeadline") or data["deadline"],
        "status": "open",
        "createdBy": decoded["uid"],
        "createdAt": firestore.SERVER_TIMESTAMP,
    })

    send_push(
        title="مسابقة جديدة 🎯",
        body=f"{data['title']} — سجّل دلوقتي قبل ما يفوتك الموعد",
        tokens=get_all_recipient_tokens(),
        competition_id=competition_ref.id,
    )

    return jsonify({"id": competition_ref.id}), 201


# ---------- Log a student interaction (view / click / submit) ----------
@app.route("/api/interactions", methods=["POST"])
def log_interaction():
    decoded = verify_request_user()
    if not decoded:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json()
    doc_id = f"{data['competitionId']}_{decoded['uid']}"
    ref = db.collection("competition_interactions").document(doc_id)

    update_payload = {
        "competitionId": data["competitionId"],
        "studentId": decoded["uid"],
        "lastUpdatedAt": firestore.SERVER_TIMESTAMP,
    }
    event_type = data["event"]  # "view" | "click" | "submit" | "whatsapp_join"
    field_map = {
        "view": "viewedAt",
        "click": "clickedLinkAt",
        "submit": "formSubmittedAt",
        # Set when the student taps "Join WhatsApp group" after registering.
        # Like "click", this reflects that they opened the invite link, not
        # confirmed proof they stayed in the group — same caveat as the
        # external-form problem this whole flow was built to avoid.
        "whatsapp_join": "whatsappJoinedAt",
    }
    if event_type in field_map:
        update_payload[field_map[event_type]] = firestore.SERVER_TIMESTAMP

    # "submit" carries the in-app registration form (team/leader/contact
    # details) collected right after the student presses "Apply now" —
    # this is what actually drives the funnel + admin follow-up table,
    # instead of relying on an external form we have no visibility into.
    if event_type == "submit":
        registration = data.get("registration") or {}
        participation_type = registration.get("participationType")
        if participation_type not in ("individual", "team"):
            return jsonify({"error": "invalid participationType"}), 400

        leader_name = (registration.get("leaderName") or "").strip()
        leader_phone = (registration.get("leaderPhone") or "").strip()
        leader_email = (registration.get("leaderEmail") or "").strip()
        if not leader_name or not leader_phone or not leader_email:
            return jsonify({"error": "missing required registration fields"}), 400

        team_members = []
        if participation_type == "team":
            for m in registration.get("teamMembers", []):
                name = (m.get("name") or "").strip()
                phone = (m.get("phone") or "").strip()
                if name and phone:
                    team_members.append({"name": name, "phone": phone})

        update_payload.update({
            "participationType": participation_type,
            "leaderName": leader_name,
            "leaderPhone": leader_phone,
            "leaderEmail": leader_email,
            "supervisorName": (registration.get("supervisorName") or "").strip() or None,
            "teamMembers": team_members,
            "notes": (registration.get("notes") or "").strip() or None,
        })

    ref.set(update_payload, merge=True)
    return jsonify({"ok": True})


# ---------- Push notification helper ----------
CRON_SECRET = os.environ.get("CRON_SECRET", "")


def get_all_recipient_tokens(exclude_registered_for=None):
    """Tokens for students + admins. If exclude_registered_for is a competition id,
    students who already submitted the form for it are skipped (they don't need
    a reminder) — admins and everyone else still get it."""
    submitted_ids = set()
    if exclude_registered_for:
        subs = db.collection("competition_interactions").where(
            "competitionId", "==", exclude_registered_for
        ).stream()
        submitted_ids = {
            s.to_dict().get("studentId") for s in subs if s.to_dict().get("formSubmittedAt")
        }

    users = db.collection("users").where("role", "in", ["student", "admin"]).stream()
    tokens = []
    for u in users:
        ud = u.to_dict()
        if ud.get("role") == "student" and u.id in submitted_ids:
            continue
        token = ud.get("fcmToken")
        if token:
            tokens.append(token)
    return tokens


def send_push(title, body, tokens, competition_id=None):
    if not tokens:
        print("push: no tokens to send to — skipping")
        return

    print(f"push: sending '{title}' to {len(tokens)} token(s)")
    base_url = "https://love0live0life0alone-prog.github.io/competitions-hub"
    link = f"{base_url}/home.html?competition={competition_id}" if competition_id else f"{base_url}/home.html"

    for i in range(0, len(tokens), 500):
        batch = tokens[i:i + 500]
        messages = [
            messaging.Message(
                notification=messaging.Notification(title=title, body=body),
                data={"competitionId": competition_id or ""},
                webpush=messaging.WebpushConfig(
                    notification=messaging.WebpushNotification(
                        icon=f"{base_url}/icon-192.png",
                        badge=f"{base_url}/icon-192.png",
                    ),
                    fcm_options=messaging.WebpushFCMOptions(link=link),
                ),
                token=token,
            )
            for token in batch
        ]
        try:
            response = messaging.send_each(messages)
            success = sum(1 for r in response.responses if r.success)
            failure = len(response.responses) - success
            print(f"push: {success} succeeded, {failure} failed")
            for j, r in enumerate(response.responses):
                if not r.success:
                    err = str(r.exception)
                    print(f"push: token[{j}] failed — {err}")
                    if "not-registered" in err or "Unregistered" in err or "Device unregistered" in err:
                        clean_dead_token(batch[j])
        except Exception as e:
            print(f"push send error: {e}")


def clean_dead_token(dead_token):
    """Remove a stale fcmToken from whichever user document still has it,
    so the next login/registerPushToken() call replaces it with a fresh one."""
    try:
        docs = db.collection("users").where("fcmToken", "==", dead_token).stream()
        for d in docs:
            d.reference.update({"fcmToken": firestore.DELETE_FIELD})
            print(f"push: cleared dead token for user {d.id}")
    except Exception as e:
        print(f"push: clean_dead_token error: {e}")


EGYPT_TZ = ZoneInfo("Africa/Cairo")


# ---------- Close competition + announce winners (admin only) ----------
@app.route("/api/competitions/<competition_id>/close", methods=["POST"])
def close_competition(competition_id):
    decoded = verify_request_user()
    if not decoded:
        return jsonify({"error": "unauthorized"}), 401
    if not require_admin(decoded):
        return jsonify({"error": "forbidden"}), 403

    data = request.get_json() or {}
    winners = data.get("winners", [])

    comp_ref = db.collection("competitions").document(competition_id)
    comp_snap = comp_ref.get()
    if not comp_snap.exists:
        return jsonify({"error": "not found"}), 404
    comp = comp_snap.to_dict()

    comp_ref.update({"status": "closed", "winners": winners})

    for w in winners:
        if not w.get("studentId"):
            continue
        db.collection("competition_interactions").document(
            f"{competition_id}_{w['studentId']}"
        ).set(
            {"followUpStatus": "completed", "finalResult": "won", "rank": w.get("rank")},
            merge=True,
        )

    names = "، ".join(w.get("name", "") for w in winners[:3])
    body = f"{comp.get('title', '')} — الفايزين: {names}" if names else f"نتائج {comp.get('title', '')} إتعلنت"

    send_push(
        title="نتائج المسابقة إتعلنت 🏆",
        body=body,
        tokens=get_all_recipient_tokens(),
        competition_id=competition_id,
    )
    return jsonify({"ok": True})


# ---------- Deadline reminders (called by an external cron pinger) ----------
@app.route("/api/cron/deadline-reminders", methods=["GET", "POST"])
def deadline_reminders():
    if not CRON_SECRET or request.headers.get("X-Cron-Secret") != CRON_SECRET:
        return jsonify({"error": "forbidden"}), 403

    now = datetime.now(EGYPT_TZ).replace(tzinfo=None)
    sent = []

    for doc_snap in db.collection("competitions").where("status", "==", "open").stream():
        c = doc_snap.to_dict()
        deadline_str = c.get("applicationDeadline") or c.get("deadline")
        if not deadline_str:
            continue
        try:
            deadline = datetime.fromisoformat(deadline_str)
        except ValueError:
            continue

        hours_left = (deadline - now).total_seconds() / 3600

        if 24 < hours_left <= 48 and not c.get("reminder48hSent"):
            send_push(
                title="⏰ باقي يومين على قفل التقديم",
                body=f"{c.get('title', '')} — سجّل دلوقتي قبل ما يفوتك",
                tokens=get_all_recipient_tokens(exclude_registered_for=doc_snap.id),
                competition_id=doc_snap.id,
            )
            doc_snap.reference.update({"reminder48hSent": True})
            sent.append(f"{doc_snap.id}:48h")

        if 0 < hours_left <= 24 and not c.get("reminder24hSent"):
            send_push(
                title="🚨 باقي يوم واحد بس على قفل التقديم",
                body=f"{c.get('title', '')} — آخر فرصة للتسجيل",
                tokens=get_all_recipient_tokens(exclude_registered_for=doc_snap.id),
                competition_id=doc_snap.id,
            )
            doc_snap.reference.update({"reminder24hSent": True})
            sent.append(f"{doc_snap.id}:24h")

    return jsonify({"ok": True, "sent": sent})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))