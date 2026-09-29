#!/usr/bin/env python3
"""Email a buyer their download link by hand.

  ./send_order.py pay_XXXXXXXX buyer@example.com

Use it when the shop log says a payment "has no buyer email": the order is saved,
but Whop didn't include the buyer's address. Find the payment in your Whop
dashboard (Payments), copy the buyer's email, and run this. Running it again for
the same payment sends the email again, which is also how to re-send a lost one.
"""

import re
import sys

from kit.common import db
from kit.orders import PAYMENT_ID, send_order_email


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__.strip().split("\n\n")[0])
    pay_id, email = sys.argv[1].strip(), sys.argv[2].strip()
    if not PAYMENT_ID.fullmatch(pay_id):
        sys.exit(f"That doesn't look like a Whop payment id (pay_...): {pay_id}")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        sys.exit(f"That doesn't look like an email address: {email}")
    with db() as conn:
        order = conn.execute("SELECT * FROM orders WHERE session_id = ?", (pay_id,)).fetchone()
        product = order and conn.execute("SELECT * FROM products WHERE slug = ?",
                                         (order["slug"],)).fetchone()
    if not order or not product:
        sys.exit(f"No order for {pay_id} on this server. Check the payment id in Whop. "
                 f"If the payment is there, look at the shop log for why it wasn't saved: "
                 f"sudo journalctl -u digital-product-kit -n 50")
    send_order_email(product, email, order["token"])
    with db() as conn:
        conn.execute("UPDATE orders SET email = ?, emailed = 1 WHERE session_id = ?",
                     (email, pay_id))
    print(f"Sent {product['title']} to {email}.")


if __name__ == "__main__":
    main()
