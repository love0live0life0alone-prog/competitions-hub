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
        "deadline": data["deadline"],
        "status": "open",
        "createdBy": decoded["uid"],
        "createdAt": firestore.SERVER_TIMESTAMP,
    })

    send_push_to_all_students(
        title="مسابقة جديدة 🎯",
        body=data["title"],
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
    event_type = data["event"]  # "view" | "click" | "submit"
    field_map = {
        "view": "viewedAt",
        "click": "clickedLinkAt",
        "submit": "formSubmittedAt",
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
def send_push_to_all_students(title, body, competition_id):
    tokens_query = db.collection("users").where("role", "==", "student").stream()
    tokens = [doc.to_dict().get("fcmToken") for doc in tokens_query if doc.to_dict().get("fcmToken")]

    if not tokens:
        return

    # FCM allows up to 500 tokens per multicast call — batch if needed.
    for i in range(0, len(tokens), 500):
        batch = tokens[i:i + 500]
        message = messaging.MulticastMessage(
            notification=messaging.Notification(title=title, body=body),
            data={"competitionId": competition_id},
            tokens=batch,
        )
        messaging.send_multicast(message)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))