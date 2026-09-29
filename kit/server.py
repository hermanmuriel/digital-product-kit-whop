"""The small web service that sits behind every product domain.

  POST /webhook                  Whop says "payment succeeded" -> save the order, email the link
  GET  /order-link?payment=      thank-you page asks for the buyer's download link
  GET  /downloads/<token>        the buyer's download page
  GET  /downloads/<token>/<n>    one file
  GET  /health                   {"ok": true}

nginx serves the sales page itself and forwards these paths here. The Buy button is a
Whop checkout link, so this service holds no Whop key at all: only the webhook
secret, which can do nothing except check that a message really came from Whop.
"""

import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from html import escape

from flask import Flask, abort, request, send_from_directory

from kit.common import ENV, FILES_DIR, db, get_setting
from kit.orders import PAYMENT_ID, send_order_email

app = Flask(__name__)

TIMESTAMP = re.compile(r"[0-9]{1,12}")
# Files go out only for these. Anything else (authorized, pending, unresolved...) waits.
PAID = {"paid", "succeeded"}


def signature_ok(payload, headers, secret):
    """Whop signs with Standard Webhooks: base64 HMAC-SHA256 of "<id>.<timestamp>.<body>",
    keyed with the literal bytes of the ws_... secret, prefix included."""
    msg_id = headers.get("webhook-id")
    timestamp = headers.get("webhook-timestamp") or ""
    header = headers.get("webhook-signature")
    if not secret or not msg_id or not header:
        return False
    if not TIMESTAMP.fullmatch(timestamp) or abs(time.time() - int(timestamp)) > 300:
        return False
    signed = f"{msg_id}.{timestamp}.".encode() + payload
    expected = base64.b64encode(hmac.new(secret.encode(), signed, hashlib.sha256).digest())
    # The header can hold several signatures ("v1,abc v1,def") while a secret rotates.
    sigs = [part[3:] for part in header.split() if part.startswith("v1,")]
    return any(hmac.compare_digest(expected, s.encode()) for s in sigs)


def buyer_email(payment):
    # Newer API versions put it at the top; older ones inside "user".
    user = payment.get("user") if isinstance(payment.get("user"), dict) else {}
    return payment.get("customer_email") or user.get("email") or ""


def plan_id(payment):
    plan = payment.get("plan") if isinstance(payment.get("plan"), dict) else {}
    return payment.get("plan_id") or plan.get("id")


def signed_by(payload, headers):
    """Which Whop (live or sandbox) signed this message, or None if neither did."""
    with db() as conn:
        for mode in ("live", "sandbox"):
            if signature_ok(payload, headers, get_setting(conn, f"whop_webhook_secret_{mode}")):
                return mode
    return None


def product_for(conn, payment, mode):
    """The product this payment bought, but only if the product is currently sold in
    the same mode. After going live, a sandbox (test card) payment unlocks nothing."""
    meta = payment.get("metadata") if isinstance(payment.get("metadata"), dict) else {}
    if meta.get("product_slug"):
        row = conn.execute("SELECT * FROM products WHERE slug = ? AND mode = ?",
                           (meta["product_slug"], mode)).fetchone()
        if row:
            return row
    # Backup: the variant id, for a sale made from the product's page on whop.com.
    return conn.execute(
        "SELECT p.* FROM plans v JOIN products p ON p.slug = v.slug "
        "WHERE v.plan_id = ? AND v.mode = ? AND p.mode = ?",
        (plan_id(payment), mode, mode)).fetchone()


@app.post("/webhook")
def webhook():
    payload = request.get_data()
    mode = signed_by(payload, request.headers)
    if not mode:
        abort(400)
    try:
        event = json.loads(payload)
    except ValueError:
        abort(400)
    if not isinstance(event, dict) or event.get("type") != "payment.succeeded":
        return {"received": True}
    payment = event.get("data") if isinstance(event.get("data"), dict) else {}
    pay_id = str(payment.get("id") or "")

    with db() as conn:
        product = product_for(conn, payment, mode)
        # Payments for anything this kit isn't selling (in this mode): acknowledge, ignore.
        if not product or not PAYMENT_ID.fullmatch(pay_id):
            return {"received": True, "ignored": True}
        if payment.get("status") not in PAID:
            app.logger.warning("payment %s has status %r, not sending files", pay_id,
                               payment.get("status"))
            return {"received": True, "not_paid": True}
        email = buyer_email(payment)
        conn.execute(
            "INSERT INTO orders (token, email, slug, session_id, emailed) "
            "VALUES (?, ?, ?, ?, 0) ON CONFLICT(session_id) DO NOTHING",
            (str(uuid.uuid4()), email, product["slug"], pay_id))
        if email:
            conn.execute("UPDATE orders SET email = ? WHERE session_id = ? AND email = ''",
                         (email, pay_id))
        order = conn.execute("SELECT * FROM orders WHERE session_id = ?", (pay_id,)).fetchone()

    if order["emailed"]:
        return {"received": True, "duplicate": True}
    if not order["email"]:
        # The order is saved (the thank-you page still shows the download), but there's
        # no address to email. The seller finds it in Whop and sends it by hand.
        app.logger.error("payment %s has no buyer email. Find it in Whop under Payments, "
                         "then run: ./send_order.py %s their@email.com", pay_id, pay_id)
        return {"received": True, "needs_email": True}
    try:
        send_order_email(product, order["email"], order["token"])
    except Exception:
        # The email is the product. The order stays unsent, so Whop's retry sends it.
        app.logger.exception("order email failed for %s", pay_id)
        abort(500)
    with db() as conn:
        conn.execute("UPDATE orders SET emailed = 1 WHERE session_id = ?", (pay_id,))
    return {"received": True}


@app.get("/order-link")
def order_link():
    """Lets the thank-you page show the download link straight away. Payment ids
    are long and random, and only orders from the last day are answered."""
    pay_id = request.args.get("payment", "")
    if not PAYMENT_ID.fullmatch(pay_id):
        return {"ready": False}, 400
    with db() as conn:
        row = conn.execute(
            "SELECT o.token, p.domain FROM orders o JOIN products p ON p.slug = o.slug "
            "WHERE o.session_id = ? AND o.created_at > datetime('now', '-1 day')",
            (pay_id,)).fetchone()
    if not row:
        return {"ready": False}
    return {"ready": True, "url": f"https://{row['domain']}/downloads/{row['token']}"}


def load_order(token):
    try:
        uuid.UUID(token)
    except ValueError:
        abort(404)
    with db() as conn:
        order = conn.execute(
            "SELECT o.*, p.title, p.files, p.expiry_days FROM orders o "
            "JOIN products p ON p.slug = o.slug WHERE o.token = ?", (token,)).fetchone()
    if not order:
        abort(404)
    created = datetime.fromisoformat(order["created_at"]).replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > created + timedelta(days=order["expiry_days"]):
        abort(410, "This download link has expired.")
    return order


PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{title} downloads</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 560px; margin: 64px auto;
         padding: 0 20px; color: #111; }}
  a.file {{ display: block; padding: 14px 18px; margin: 12px 0; background: #111;
            color: #fff; text-decoration: none; border-radius: 8px; }}
  p.note {{ color: #666; font-size: 14px; }}
</style></head><body>
<h1>{title}</h1>
<p>Your files are below. Click each one to download.</p>
{links}
<p class="note">Need help? Email {support}.</p>
</body></html>"""


@app.get("/downloads/<token>")
def download_page(token):
    order = load_order(token)
    links = "\n".join(
        f'<a class="file" href="/downloads/{token}/{i}">Download {escape(name)}</a>'
        for i, name in enumerate(json.loads(order["files"])))
    return PAGE.format(title=escape(order["title"]), links=links,
                       support=escape(ENV.get("SUPPORT_EMAIL", "")))


@app.get("/downloads/<token>/<int:n>")
def download_file(token, n):
    order = load_order(token)
    files = json.loads(order["files"])
    if not 0 <= n < len(files):
        abort(404)
    with db() as conn:
        conn.execute("UPDATE orders SET downloads = downloads + 1 WHERE token = ?", (token,))
    # The file name comes only from the database, never from the URL.
    return send_from_directory(FILES_DIR / order["slug"], files[n], as_attachment=True)


@app.get("/health")
def health():
    return {"ok": True}
