"""The order email, shared by the web service and send_order.py (which runs without Flask)."""

import re

from kit.common import ENV, db, get_setting, http

PAYMENT_ID = re.compile(r"pay_[A-Za-z0-9]{8,200}")


def send_order_email(product, email, token):
    link = f"https://{product['domain']}/downloads/{token}"
    # A Resend key that can only send, and only from this product's domain.
    with db() as conn:
        key = get_setting(conn, f"resend_send_key:{product['domain']}")
    http("POST", "https://api.resend.com/emails",
         headers={"Authorization": f"Bearer {key}"},
         json_body={
             "from": f"{product['seller_name']} <orders@{product['domain']}>",
             "to": [email],
             "reply_to": ENV["SUPPORT_EMAIL"],
             "subject": f"Your copy of {product['title']} is ready",
             "text": (
                 f"Hi,\n\nThank you for your order. Your copy of {product['title']} "
                 f"is ready.\n\nDownload it here:\n{link}\n\n"
                 f"That link is yours, so save the files somewhere safe once you "
                 f"have them.\n\nIf anything doesn't work, just reply to this email.\n\n"
                 f"{product['seller_name']}"
             ),
         })
