#!/usr/bin/env python3
"""Put a digital product on sale on its own domain, in one command.

  ./new_product.py --name "The Houseplant Handbook" --price 19 \\
      --file handbook.pdf --domain houseplanthandbook.com --seller "Maya Green"

What it does:
  1. Buys the domain at GoDaddy if you don't own it yet (with --buy)
  2. Creates the product, price and checkout link in Whop
  3. Points the domain at this server (GoDaddy or Cloudflare DNS)
  4. Sets up order email from orders@<domain> with Resend
  5. Writes the sales page and thank-you page
  6. Configures nginx and gets an HTTPS certificate
Safe to run again: it updates what exists instead of making duplicates.

Add --sandbox to try it with Whop's sandbox first: same flow, test cards, no real money.

It asks for your Whop, Resend and GoDaddy (or Cloudflare) keys each time and never
saves them. What stays on the server can't touch your money, your domain or your
email account: a Whop webhook secret and a Resend key that can only send email
from this one domain.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from getpass import getpass
from html import escape
from pathlib import Path

from kit.common import (ENV, FILES_DIR, ROOT, WHOP_API_VERSION, WHOP_HOSTS, HttpError, db,
                         get_setting, http, set_setting, whop)
from kit.dns import provider_for

PORT = ENV.get("PORT", "8750")
GREEN, DIM, BOLD, RESET = "\033[32m", "\033[2m", "\033[1m", "\033[0m"


def step(text):
    print(f"\n{BOLD}{text}{RESET}")


def done(text):
    print(f"  {GREEN}✓{RESET} {text}")


def sudo(*cmd, stdin=None):
    subprocess.run(["sudo", *cmd], input=stdin, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


SETUP_KEYS = ("WHOP_API_KEY", "RESEND_API_KEY", "GODADDY_TOKEN", "CLOUDFLARE_API_TOKEN")


def check_env():
    email = ENV.get("SUPPORT_EMAIL", "")
    if "@" not in email or email.endswith("@example.com"):
        sys.exit("Set SUPPORT_EMAIL in .env first. Run: nano .env")
    saved = [k for k in SETUP_KEYS if ENV.get(k)]
    if saved:
        sys.exit(f"Remove these from .env, they shouldn't be saved on the server: "
                 f"{', '.join(saved)}. The command asks for them when it needs them.")


def ask_key(name, label, prefixes=(), optional=False):
    """Typed in (hidden) or taken from the shell environment. Never written to disk."""
    value = os.environ.get(name) or getpass(f"  {label}: ").strip()
    if not value and optional:
        return ""
    if not value or (prefixes and not value.startswith(prefixes)):
        sys.exit(f"That doesn't look like a {label.split(' (')[0]}. Nothing was changed.")
    return value


# ------------------------------------------------------------------ Whop


def whop_product(key, mode, args, slug, domain):
    """Product, variant (the price) and the checkout link behind the Buy button.

    Whop's ids are kept in the local database after each step, so running again (even
    after a failure halfway) updates them instead of making duplicates. A new price
    gets a new variant; a new variant or domain gets a new checkout link, and the old
    link is deleted so it can't keep selling."""
    setting = f"whop:{mode}:{slug}"
    with db() as conn:
        ids = json.loads(get_setting(conn, setting) or "{}")

    def save(**changes):
        ids.update(changes)
        with db() as conn:
            set_setting(conn, setting, json.dumps(ids))

    meta = {"product_slug": slug}
    product = None
    if ids.get("product"):
        try:
            product = whop(key, mode, "PATCH", f"/products/{ids['product']}",
                           {"title": args.name[:80]})
        except HttpError as err:
            if err.status != 404:
                raise
    if not product:
        product = whop(key, mode, "POST", "/products",
                       {"title": args.name[:80], "metadata": meta})
        ids.clear()
        save(product=product["id"])

    plan = None
    if ids.get("plan"):
        try:
            plan = whop(key, mode, "GET", f"/variants/{ids['plan']}")
        except HttpError as err:
            if err.status != 404:
                raise
        same = plan and plan.get("visibility") != "archived" \
            and round(float(plan.get("initial_price") or 0), 2) == round(args.price, 2) \
            and (plan.get("currency") or "").lower() == args.currency
        if plan and not same:
            # Remembered, so it still gets archived if this run stops halfway.
            save(retire=plan["id"])
            plan = None
    if not plan:
        plan = whop(key, mode, "POST", "/variants", {
            "product_id": product["id"],
            "plan_type": "one_time",
            "initial_price": args.price,
            "currency": args.currency,
            "metadata": meta,  # Whop copies this onto every payment
        })
        save(plan=plan["id"])
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO plans (plan_id, slug, mode) VALUES (?, ?, ?)",
                     (plan["id"], slug, mode))

    checkout = None
    if ids.get("checkout") and ids.get("checkout_plan") == plan["id"] \
            and ids.get("checkout_domain") == domain:
        try:
            checkout = whop(key, mode, "GET", f"/checkout_configurations/{ids['checkout']}")
        except HttpError as err:
            if err.status != 404:
                raise
    if not checkout:
        old_checkout = ids.get("checkout")
        checkout = whop(key, mode, "POST", "/checkout_configurations", {
            "mode": "payment",
            "plan_id": plan["id"],
            # Whop adds ?payment=pay_...&status=... when it sends the buyer back.
            "redirect_url": f"https://{domain}/thank-you.html",
            # Copied onto every payment, so the webhook knows which product was bought.
            "metadata": {**meta, "domain": domain},
        })
        save(checkout=checkout["id"], checkout_plan=plan["id"], checkout_domain=domain)
        if old_checkout:
            try:
                whop(key, mode, "DELETE", f"/checkout_configurations/{old_checkout}")
            except HttpError as err:
                if err.status != 404:
                    raise
    if ids.get("retire") and ids["retire"] != plan["id"]:
        # Old price: stop selling it, now that the new one is on sale. Past buyers
        # keep their downloads.
        try:
            whop(key, mode, "PATCH", f"/variants/{ids['retire']}", {"visibility": "archived"})
        except HttpError as err:
            if err.status != 404:
                raise
    if "retire" in ids:
        ids.pop("retire")
        save()

    buy_url = checkout.get("purchase_url") or f"/checkout/{checkout['id']}"
    if buy_url.startswith("/"):
        buy_url = WHOP_HOSTS[mode][1] + buy_url
    return product["id"], plan["id"], buy_url


def whop_webhook(key, mode, domain):
    """One webhook per Whop mode (sandbox/live) is enough for every product."""
    with db() as conn:
        if get_setting(conn, f"whop_webhook_secret_{mode}"):
            return "already set up"
    url = f"https://{domain}/webhook"
    account_id = whop(key, mode, "GET", "/accounts/me")["id"]
    # A webhook left from an earlier install signs with a secret this server no longer
    # has. Its deliveries would all fail, so remove it before making the new one.
    hooks = whop(key, mode, "GET", "/webhooks", query={"account_id": account_id, "first": 100})
    for hook in hooks.get("data", []):
        if hook.get("url") == url:
            whop(key, mode, "DELETE", f"/webhooks/{hook['id']}")
    hook = whop(key, mode, "POST", "/webhooks", {
        "url": url,
        "events": ["payment.succeeded"],
        "api_version_date": WHOP_API_VERSION,
    })
    if not hook.get("webhook_secret"):
        sys.exit("Whop made the webhook but didn't return its signing secret. "
                 "Delete it in Whop (Dashboard > Developer > Webhooks) and run this again.")
    with db() as conn:
        set_setting(conn, f"whop_webhook_secret_{mode}", hook["webhook_secret"])
    return url


def explain_http_error(err, mode):
    """Turns the most common API failures into a next step."""
    msg = str(err)
    if err.status in (401, 403) and "whop.com" in msg:
        other = "without --sandbox" if mode == "sandbox" else "with --sandbox"
        return (f"Whop didn't accept that key ({err.status}). Check you copied the whole key "
                f"and gave it the Admin role. Sandbox and real keys look the same: if this "
                f"is the other kind, run the command again {other}.\n\n{msg}")
    return msg


# ------------------------------------------------------------------ domain, DNS + email


def money(amount):
    return f"${amount['value'] / 100:.2f}" if amount["currencyCode"] == "USD" \
        else f"{amount['value'] / 100:.2f} {amount['currencyCode']}"


def ensure_domain(args, domain, provider_name, dns):
    """Returns a line for the log. Buys the domain only with --buy and a yes."""
    if provider_name != "GoDaddy":
        if args.buy:
            sys.exit("Buying a domain only works with GoDaddy. Run again and paste a GoDaddy token.")
        return f"{domain} is in your Cloudflare account"
    if dns.owns():
        return f"{domain} is already yours"
    quote = dns.quote()
    if not quote:
        sys.exit(f"{domain} is taken. Pick another name.")
    price, renews = money(quote["price"]), money(quote["renewalPrice"])
    if not args.buy:
        sys.exit(f"{domain} is not in your GoDaddy account. It's available for {price} "
                 f"(renews at {renews}/year). Run again with --buy to buy it.")
    agreements = ", ".join(a["url"] for a in quote["requiredAgreements"])
    print(f"  {domain} costs {price} for the first year, then {renews}/year.")
    print(f"  {DIM}Buying accepts GoDaddy's terms: {agreements}{RESET}")
    if not args.yes:
        answer = input("  Buy it with the card on your GoDaddy account? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            sys.exit("Stopped. Nothing was bought.")
    agreed_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    dns.register(quote, agreed_at)
    return f"bought {domain} for {price}"



def resend_domain(key, domain):
    headers = {"Authorization": f"Bearer {key}"}
    existing = [d for d in http("GET", "https://api.resend.com/domains", headers=headers)["data"]
                if d["name"] == domain]
    created = existing[0] if existing else http(
        "POST", "https://api.resend.com/domains", headers=headers, json_body={"name": domain})
    detail = http("GET", f"https://api.resend.com/domains/{created['id']}", headers=headers)
    return detail["id"], detail["status"], detail["records"], headers


def resend_send_key(conn, headers, domain, domain_id):
    """The server keeps a key that can only send email, and only from this domain."""
    if get_setting(conn, f"resend_send_key:{domain}"):
        return "already set up"
    created = http("POST", "https://api.resend.com/api-keys", headers=headers, json_body={
        "name": f"digital-product-kit {domain} (send only)",
        "permission": "sending_access",
        "domain_id": domain_id,
    })
    set_setting(conn, f"resend_send_key:{domain}", created["token"])
    return "send-only key made for this domain"


def wait_for_resend(domain_id, headers, minutes=5):
    http("POST", f"https://api.resend.com/domains/{domain_id}/verify", headers=headers)
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        status = http("GET", f"https://api.resend.com/domains/{domain_id}", headers=headers)["status"]
        if status == "verified":
            return True
        time.sleep(10)
    return False


# ------------------------------------------------------------------ web


def write_pages(args, domain, webroot, buy_url):
    fill = {
        "{{BUY_URL}}": escape(buy_url),
        "{{TITLE}}": escape(args.name),
        "{{DESCRIPTION}}": escape(args.description),
        "{{PRICE}}": escape(format_price(args.price, args.currency)),
        "{{SELLER}}": escape(args.seller),
        "{{SUPPORT_EMAIL}}": escape(ENV["SUPPORT_EMAIL"]),
        "{{COVER}}": f'<img class="cover" src="/cover{Path(args.cover).suffix}" alt="">'
                     if args.cover else "",
    }
    sudo("mkdir", "-p", webroot)
    for name in ("index.html", "thank-you.html"):
        html = (ROOT / "templates" / name).read_text()
        for key, val in fill.items():
            html = html.replace(key, val)
        sudo("tee", f"{webroot}/{name}", stdin=html.encode())
    if args.cover:
        sudo("cp", args.cover, f"{webroot}/cover{Path(args.cover).suffix}")


def nginx_config(domain, webroot, https):
    proxy = f"""
    location ~ ^/(webhook|order-link|downloads/) {{
        limit_req zone=dpk burst=20 nodelay;
        proxy_pass http://127.0.0.1:{PORT};
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }}

    location / {{
        try_files $uri $uri/ =404;
    }}"""
    acme = f"location /.well-known/acme-challenge/ {{ root {webroot}; }}"
    if not https:
        return f"""server {{
    listen 80;
    listen [::]:80;
    server_name {domain} www.{domain};
    root {webroot};
    {acme}
{proxy}
}}
"""
    return f"""server {{
    listen 80;
    listen [::]:80;
    server_name {domain} www.{domain};
    server_tokens off;
    {acme}
    location / {{ return 301 https://{domain}$request_uri; }}
}}

server {{
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name {domain} www.{domain};
    ssl_certificate /etc/letsencrypt/live/{domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;
    server_tokens off;
    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy same-origin always;
    add_header Content-Security-Policy "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'" always;
    root {webroot};
{proxy}
}}
"""


RATE_LIMIT = "limit_req_zone $binary_remote_addr zone=dpk:10m rate=10r/s;\n"


def install_nginx(domain, conf):
    # At most 10 requests a second per visitor to the service (burst 20).
    sudo("tee", "/etc/nginx/conf.d/digital-product-kit.conf", stdin=RATE_LIMIT.encode())
    path = f"/etc/nginx/sites-available/{domain}"
    sudo("tee", path, stdin=conf.encode())
    sudo("ln", "-sf", path, f"/etc/nginx/sites-enabled/{domain}")
    sudo("nginx", "-t")
    sudo("systemctl", "reload", "nginx")


def get_certificate(domain, webroot):
    if subprocess.run(["sudo", "test", "-e", f"/etc/letsencrypt/live/{domain}"]).returncode == 0:
        return "already have one"
    # Fresh DNS can take a minute to reach Let's Encrypt, so try a few times.
    for attempt in range(6):
        try:
            sudo("certbot", "certonly", "--webroot", "-w", webroot, "-d", domain,
                 "-d", f"www.{domain}", "--non-interactive", "--agree-tos",
                 "-m", ENV["SUPPORT_EMAIL"])
            return "issued"
        except subprocess.CalledProcessError as err:
            if attempt == 5:
                sys.exit(f"certbot failed:\n{err.stderr.decode()}")
            print(f"  {DIM}waiting for DNS to spread, retrying in 30s{RESET}")
            time.sleep(30)


# ------------------------------------------------------------------ main


def format_price(price, currency):
    symbol = {"usd": "$", "gbp": "£", "eur": "€"}.get(currency, "")
    amount = f"{price:.2f}".removesuffix(".00")
    return f"{symbol}{amount}" if symbol else f"{amount} {currency.upper()}"


def main():
    ap = argparse.ArgumentParser(description="Put a digital product on sale on its own domain.")
    ap.add_argument("--name", required=True, help="product title")
    ap.add_argument("--price", required=True, type=float, help="e.g. 19 or 19.99")
    ap.add_argument("--file", required=True, action="append", dest="files",
                    help="file the buyer gets (repeat for more than one)")
    ap.add_argument("--domain", required=True, help="e.g. houseplanthandbook.com")
    ap.add_argument("--seller", required=True, help="your name, shown on the page and emails")
    ap.add_argument("--description", default="", help="one or two lines for the sales page")
    ap.add_argument("--cover", help="cover image for the sales page")
    ap.add_argument("--currency", default="usd", help="e.g. usd, eur, cop (default usd)")
    ap.add_argument("--sandbox", action="store_true",
                    help="use Whop's sandbox (test cards, no real money) with a sandbox key")
    ap.add_argument("--server-ip", help="defaults to this server's public IP")
    ap.add_argument("--buy", action="store_true",
                    help="buy the domain at GoDaddy if you don't own it yet")
    ap.add_argument("--yes", action="store_true", help="don't ask before buying the domain")
    args = ap.parse_args()
    check_env()

    domain = args.domain.lower().removeprefix("www.")
    if not re.fullmatch(r"([a-z0-9-]+\.)+[a-z]{2,}", domain):
        sys.exit(f"That doesn't look like a domain: {args.domain} (example: houseplanthandbook.com)")
    slug = re.sub(r"[^a-z0-9]+", "-", args.name.lower()).strip("-")
    args.currency = args.currency.lower()
    if args.price < 1:
        sys.exit("Whop's lowest price is 1.00.")
    mode = "sandbox" if args.sandbox else "live"
    webroot = f"/var/www/{domain}"
    for f in args.files + ([args.cover] if args.cover else []):
        if not Path(f).is_file():
            sys.exit(f"Not a file: {f}")
    if args.sandbox:
        with db() as conn:
            live = conn.execute("SELECT 1 FROM products WHERE domain = ? AND mode = 'live'",
                                (domain,)).fetchone()
        if live:
            sys.exit(f"{domain} already takes real payments. Running with --sandbox would put "
                     f"a test checkout on your real page, where anyone could get the file with "
                     f"a test card. Leave off --sandbox, or test on a different domain.")

    print(f"{BOLD}Setting up {args.name} on {domain}{RESET}")
    print(f"{DIM}Paste each key when asked (right-click or Ctrl+Shift+V). It stays hidden "
          f"and is never saved.{RESET}")
    label = "Whop SANDBOX API key" if args.sandbox else "Whop API key (real payments)"
    whop_key = ask_key("WHOP_API_KEY", label)
    resend_key = ask_key("RESEND_API_KEY", "Resend API key, full access (re_...)", ("re_",))
    godaddy = ask_key("GODADDY_TOKEN", "GoDaddy token (or press Enter to use Cloudflare)",
                      optional=True)
    cloudflare = "" if godaddy else ask_key("CLOUDFLARE_API_TOKEN", "Cloudflare token")

    step("1. Domain")
    provider_name, dns = provider_for(domain, godaddy, cloudflare)
    done(ensure_domain(args, domain, provider_name, dns))

    step("2. Files")
    dest = FILES_DIR / slug
    db().close()  # creates data/ owner-only before the files go in
    dest.mkdir(parents=True, exist_ok=True)
    names = []
    for f in args.files:
        shutil.copy2(f, dest / Path(f).name)
        names.append(Path(f).name)
        done(f"{Path(f).name} stored")

    step("3. Whop" + (" (sandbox)" if args.sandbox else ""))
    product_id, price_id, buy_url = whop_product(whop_key, mode, args, slug, domain)
    done(f"product {product_id}")
    done(f"price {format_price(args.price, args.currency)} ({price_id})")
    done(f"checkout link {buy_url}")
    done(f"webhook {whop_webhook(whop_key, mode, domain)}")
    with db() as conn:
        # mode decides which Whop (sandbox or live) may unlock this product's files.
        conn.execute(
            "INSERT INTO products (slug, title, domain, seller_name, price_id, files, mode) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(slug) DO UPDATE SET "
            "title = excluded.title, domain = excluded.domain, "
            "seller_name = excluded.seller_name, price_id = excluded.price_id, "
            "files = excluded.files, mode = excluded.mode",
            (slug, args.name, domain, args.seller, price_id, json.dumps(names), mode))

    step("4. DNS")
    ip = args.server_ip or http("GET", "https://api.ipify.org?format=json")["ip"]
    dns.set_record("A", "@", ip)
    done(f"{domain} -> {ip} ({provider_name})")
    dns.set_record("CNAME", "www", domain if provider_name == "Cloudflare" else "@")
    done(f"www.{domain} -> {domain}")

    step("5. Order email")
    resend_id, status, records, resend_headers = resend_domain(resend_key, domain)
    for r in records:
        name = r["name"].removesuffix(f".{domain}")
        dns.set_record(r["type"], name, r["value"],
                       priority=int(r["priority"]) if r.get("priority") is not None else None)
        done(f"{r['type']} {name}")
    with db() as conn:
        done(resend_send_key(conn, resend_headers, domain, resend_id))

    step("6. Sales page + HTTPS")
    write_pages(args, domain, webroot, buy_url)
    done("pages written")
    install_nginx(domain, nginx_config(domain, webroot, https=False))
    done("nginx site added")
    done(f"certificate {get_certificate(domain, webroot)}")
    install_nginx(domain, nginx_config(domain, webroot, https=True))
    done("HTTPS on, security headers on, requests rate limited")
    sudo("systemctl", "restart", "digital-product-kit")
    done("shop service restarted")

    step("7. Checking email sending")
    if status == "verified" or wait_for_resend(resend_id, resend_headers):
        done(f"orders@{domain} verified")
    else:
        print(f"  {DIM}Resend is still checking the records. Orders will email once it "
              f"shows 'verified' at resend.com/domains.{RESET}")

    note = "SANDBOX, pay with 4242 4242 4242 4242" if args.sandbox else "LIVE"
    print(f"\n{GREEN}{BOLD}Live: https://{domain}{RESET}  {DIM}({note}){RESET}")
    print(f"{DIM}Your keys were not saved on this server.{RESET}\n")


if __name__ == "__main__":
    try:
        main()
    except HttpError as err:
        sys.exit(explain_http_error(err, "sandbox" if "--sandbox" in sys.argv else "live"))
