import os
import json
import base64
import pickle
import imaplib
import email
import requests
from datetime import datetime, timedelta

from flask import Flask, request, jsonify, render_template
from flask_cors import CORS

from email.header import decode_header
from email.mime.text import MIMEText
from googleapiclient.discovery import build

# ================= CONFIG =================

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

EMAIL_ACCOUNT = os.getenv("EMAIL_ACCOUNT")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
GMAIL_TOKEN = os.getenv("GMAIL_TOKEN")

LOG_FILE = "resend_logs.json"

# ===== TELEGRAM ALERT CONFIG =====
ALERT_EVERY_RESEND = os.getenv("ALERT_EVERY_RESEND", "false").lower() == "true"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
# log_print("ALERT_EVERY_RESEND =", ALERT_EVERY_RESEND)

ALERT_LIMIT = int(os.getenv("ALERT_RESEND_LIMIT", 5))
ALERT_WINDOW = int(os.getenv("ALERT_WINDOW_MINUTES", 10))

last_alert_time = None

# ================= APP =================

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret")
CORS(app, supports_credentials=True)

# ================= UTILS =================

def log_print(*args):
    print("🔹", *args, flush=True)

def load_logs():
    if not os.path.exists(LOG_FILE):
        return []
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return []

def save_log(user, merchant_email, subject):
    logs = load_logs()

    logs.append({
        "time": datetime.utcnow().isoformat(),
        "user": user,
        "merchant_email": merchant_email,
        "subject": subject
    })

    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)

# def send_telegram_alert(message):
#     if not BOT_TOKEN or not CHAT_ID:
#         return
#     try:
#         requests.post(
#             f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
#             json={
#                 "chat_id": CHAT_ID,
#                 "text": message,
#                 "parse_mode": "HTML"
#             },
#             timeout=5
#         )
#     except Exception as e:
#         log_print("TELEGRAM ERROR:", e)
def send_telegram_alert(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }

    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        log_print("TELEGRAM ERROR:", e)
# def alert_single_resend(user, merchant_email, subject):
#     msg = (
#         "📨 <b>RESEND EMAIL</b>\n\n"
#         f"👤 User: {user}\n"
#         f"📧 Merchant: {merchant_email}\n"
#         f"📝 Subject: {subject}\n\n"
#         f"⏱ {datetime.utcnow().strftime('%H:%M:%S %d/%m/%Y')}"
#     )
#     send_telegram_alert(msg)
def alert_single_resend(user, merchant_email, subject):
    log_print("📤 SENDING TELEGRAM ALERT...")
    msg = (
        "📨 <b>RESEND EMAIL</b>\n\n"
        f"👤 User: {user}\n"
        f"📧 Merchant: {merchant_email}\n"
        f"📝 Subject: {subject}\n"
        f"⏱ {datetime.utcnow().strftime('%H:%M:%S %d/%m/%Y')}"
    )
    send_telegram_alert(msg)
def check_resend_alert():
    global last_alert_time

    now = datetime.utcnow()
    window_start = now - timedelta(minutes=ALERT_WINDOW)

    if last_alert_time and last_alert_time > window_start:
        return

    logs = load_logs()
    recent = [
        l for l in logs
        if datetime.fromisoformat(l["time"]) >= window_start
    ]

    if len(recent) >= ALERT_LIMIT:
        merchants = {}
        for l in recent:
            merchants[l["merchant_email"]] = merchants.get(l["merchant_email"], 0) + 1

        msg = (
            "⚠️ <b>CẢNH BÁO RESEND</b>\n\n"
            f"🔁 <b>{len(recent)}</b> resend trong {ALERT_WINDOW} phút\n\n"
            "📧 Merchant:\n"
        )
        for m, c in merchants.items():
            msg += f"• {m}: {c}\n"

        msg += f"\n⏱ {now.strftime('%H:%M:%S %d/%m/%Y')}"

        send_telegram_alert(msg)
        last_alert_time = now

# ================= GMAIL =================

def search_inbox_by_merchant(merchant_email):
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
    mail.select("INBOX")

    status, data = mail.search(
        None,
        f'(OR FROM "{merchant_email}" TO "{merchant_email}")'
    )

    results = []
    for eid in data[0].split():
        _, msg_data = mail.fetch(eid, "(RFC822)")
        msg = email.message_from_bytes(msg_data[0][1])

        subject, enc = decode_header(msg.get("Subject"))[0]
        if isinstance(subject, bytes):
            subject = subject.decode(enc or "utf-8", errors="ignore")

        results.append({
            "id": eid.decode(),
            "subject": subject,
            "date": msg.get("Date")
        })

    mail.logout()
    return results

def get_email_body_by_id(email_id):
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
    mail.select("INBOX")

    _, msg_data = mail.fetch(email_id.encode(), "(RFC822)")
    msg = email.message_from_bytes(msg_data[0][1])

    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                break
    else:
        body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

    subject, enc = decode_header(msg.get("Subject"))[0]
    if isinstance(subject, bytes):
        subject = subject.decode(enc or "utf-8", errors="ignore")

    mail.logout()
    return subject, body

def send_gmail_api(to_email, subject, html_body):
    if not GMAIL_TOKEN:
        raise Exception("GMAIL_TOKEN not set")

    creds = pickle.loads(base64.b64decode(GMAIL_TOKEN))
    service = build("gmail", "v1", credentials=creds)

    message = MIMEText(html_body or "", "html", "utf-8")
    message["to"] = to_email
    message["subject"] = subject

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    service.users().messages().send(
        userId="me",
        body={"raw": raw}
    ).execute()

# ================= ROUTES =================

@app.route("/")
def index():
    return render_template("index.html", user={"name": "Admin"})

@app.route("/search", methods=["POST"])
def search():
    merchant_email = request.form.get("merchant_email")
    if not merchant_email:
        return jsonify([])
    return jsonify(search_inbox_by_merchant(merchant_email))

# @app.route("/resend", methods=["POST"])
# def resend():
#     try:
#         email_id = request.form.get("email_id")
#         merchant_email = request.form.get("merchant_email")

#         if not email_id or not merchant_email:
#             return jsonify({"status": "error", "message": "Missing params"}), 400

#         subject, body = get_email_body_by_id(email_id)
#         send_gmail_api(merchant_email, subject, body)

#         # # save_log("admin", merchant_email, subject)
#         # # check_resend_alert()
#         # save_log("admin", merchant_email, subject)
#         # # 🔔 ALERT NGAY
#         # alert_single_resend("admin", merchant_email, subject)
#         # # (nếu vẫn muốn giữ alert theo ngưỡng)
#         # check_resend_alert()
#         return jsonify({"status": "success"})
#     except Exception as e:
#         log_print("RESEND ERROR:", e)
#         return jsonify({"status": "error", "message": str(e)}), 500
@app.route("/resend", methods=["POST"])
def resend():
    try:
        email_id = request.form.get("email_id")
        merchant_email = request.form.get("merchant_email")

        if not email_id or not merchant_email:
            return jsonify({
                "status": "error",
                "message": "Missing email_id or merchant_email"
            }), 400

        subject, body = get_email_body_by_id(email_id)

        send_gmail_api(
            to_email=merchant_email,
            subject=subject,
            html_body=body
        )

        # 📝 LOG
        save_log("admin", merchant_email, subject)

        # 🔔 ALERT MỖI RESEND
        if ALERT_EVERY_RESEND:
            log_print("🔥 ALERT_EVERY_RESEND ENABLED")
            alert_single_resend("admin", merchant_email, subject)

        # ⚠️ ALERT THEO NGƯỠNG (nếu cần)
        check_resend_alert()

        return jsonify({"status": "success"})

    except Exception as e:
        log_print("RESEND ERROR:", e)
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

# @app.route("/auto-resend", methods=["POST"])
# # def auto_resend():
#     try:
#         merchant_email = request.form.get("merchant_email")
#         if not merchant_email:
#             return jsonify({"status": "error", "message": "Missing merchant_email"}), 400

#         emails = search_inbox_by_merchant(merchant_email)
#         if not emails:
#             return jsonify({"status": "error", "message": "Không tìm thấy email"})

#         latest = emails[-1]
#         subject, body = get_email_body_by_id(latest["id"])
#         send_gmail_api(merchant_email, subject, body)

#         # save_log("admin", merchant_email, subject)
#         # check_resend_alert()
#         save_log("admin", merchant_email, subject)
#         # 🔔 ALERT NGAY
#         alert_single_resend("admin", merchant_email, subject)

#         check_resend_alert()
#         return jsonify({"status": "success", "resent_subject": subject})

#     except Exception as e:
#         log_print("AUTO RESEND ERROR:", e)
#         return jsonify({"status": "error", "message": str(e)}), 500
@app.route("/auto-resend", methods=["POST"])
def auto_resend():
    try:
        merchant_email = request.form.get("merchant_email")
        if not merchant_email:
            return jsonify({
                "status": "error",
                "message": "Missing merchant_email"
            }), 400

        emails = search_inbox_by_merchant(merchant_email)
        if not emails:
            return jsonify({
                "status": "error",
                "message": "Không tìm thấy email"
            })

        latest = emails[-1]
        subject, body = get_email_body_by_id(latest["id"])

        send_gmail_api(
            to_email=merchant_email,
            subject=subject,
            html_body=body
        )

        # 📝 LOG
        save_log("admin", merchant_email, subject)

        # 🔔 ALERT MỖI RESEND
        if ALERT_EVERY_RESEND:
            alert_single_resend("admin", merchant_email, subject)

        # ⚠️ ALERT THEO NGƯỠNG
        check_resend_alert()

        return jsonify({
            "status": "success",
            "resent_subject": subject
        })

    except Exception as e:
        log_print("AUTO RESEND ERROR:", e)
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

@app.route("/logs")
def logs():
    return jsonify(load_logs())

# ================= RUN =================

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)