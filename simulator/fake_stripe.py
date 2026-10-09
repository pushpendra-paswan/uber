"""A local fake of the small part of Stripe that the backend uses (M5.3). For development and tests only: it is NOT part
of the app, it uses only the standard library, and it keeps everything in memory (a restart forgets all sessions).

It implements Checkout Session create and retrieve (with Idempotency-Key), a payment page with a Pay button, signed webhooks
(`checkout.session.completed` and `checkout.session.expired`), and a few `/_...` endpoints to replay and inspect events.
It does NOT model: refunds, disputes, card details or declines, API versions, expiry by the clock, rate limits, other
currencies or payment methods, or anything else Stripe does.

Run it on the host (the backend container reaches it as host.docker.internal, see docker-compose.yml):
    python simulator/fake_stripe.py --webhook-secret whsec_fake_local_secret
and put these in .env, then `docker compose up -d --force-recreate backend`:
    STRIPE_SECRET_KEY=sk_test_fake_local
    STRIPE_WEBHOOK_SECRET=whsec_fake_local_secret
    STRIPE_API_URL=http://host.docker.internal:12111
"""
import argparse
import hashlib
import hmac
import html
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64decode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WEBHOOK_ATTEMPTS = 3
WEBHOOK_TIMEOUT_S = 10
SESSION_LIFETIME_S = 86400

state_lock = threading.Lock()
sessions = {}  # session id -> the Checkout Session dict
idempotency = {}  # Idempotency-Key -> {"params", "response": (status, body) or None while in flight}
events = {}  # event id -> {"id", "type", "session_id", "body" (bytes), "deliveries": [status or error text]}
config = {}


def log(text: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {text}", flush=True)


def stripe_error(status: int, error_type: str, code: str, message: str) -> tuple[int, dict]:
    return status, {"error": {"type": error_type, "code": code, "message": message}}


def parse_form(body: bytes) -> dict:
    """Form-encoded body with Stripe's bracket keys -> a flat {key: first value}, keys kept as sent (decoded)."""
    return {key: values[0] for key, values in urllib.parse.parse_qs(body.decode(), keep_blank_values=True).items()}


def sign(body: bytes) -> str:
    timestamp = str(int(time.time()))
    digest = hmac.new(config["webhook_secret"].encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def deliver(event: dict) -> int | str:
    """One delivery of a stored event with a fresh signature. Returns the HTTP status, or the error text."""
    request = urllib.request.Request(
        config["webhook_url"], data=event["body"], method="POST",
        headers={"Content-Type": "application/json", "Stripe-Signature": sign(event["body"])},
    )
    try:
        with urllib.request.urlopen(request, timeout=WEBHOOK_TIMEOUT_S) as response:
            result = response.status
    except urllib.error.HTTPError as error:
        result = error.code
    except (urllib.error.URLError, OSError) as error:
        result = f"error {type(error).__name__}"
    event["deliveries"].append(result)
    log(f"webhook {event['id']} {event['type']} -> {result}")
    return result


def make_event(event_type: str, session: dict) -> dict:
    event_id = "evt_test_" + secrets.token_hex(12)
    body = json.dumps(
        {"id": event_id, "object": "event", "type": event_type, "created": int(time.time()), "data": {"object": session}},
        separators=(",", ":"),
    ).encode()
    event = {"id": event_id, "type": event_type, "session_id": session["id"], "body": body, "deliveries": []}
    with state_lock:
        events[event_id] = event
    return event


def send_in_background(event_type: str, session: dict) -> None:
    """Sends a new event, retrying up to WEBHOOK_ATTEMPTS times with a growing pause while the answer is not 2xx."""
    event = make_event(event_type, session)

    def run() -> None:
        for attempt in range(WEBHOOK_ATTEMPTS):
            result = deliver(event)
            if isinstance(result, int) and 200 <= result < 300:
                return
            time.sleep(2**attempt)

    threading.Thread(target=run, daemon=True).start()


def create_session(form: dict) -> dict:
    metadata = {key[9:-1]: value for key, value in form.items() if key.startswith("metadata[") and key.endswith("]")}
    unit_amount = int(form.get("line_items[0][price_data][unit_amount]", 0))
    quantity = int(form.get("line_items[0][quantity]", 1))
    session_id = "cs_test_" + secrets.token_hex(16)
    return {
        "id": session_id,
        "object": "checkout.session",
        "mode": form.get("mode"),
        "amount_total": unit_amount * quantity,
        "currency": form.get("line_items[0][price_data][currency]"),
        "payment_status": "unpaid",
        "status": "open",
        "url": f"{config['public_url']}/pay/{session_id}",
        "client_reference_id": form.get("client_reference_id"),
        "metadata": metadata,
        "success_url": form.get("success_url"),
        "cancel_url": form.get("cancel_url"),
        "payment_intent": None,
        "created": int(time.time()),
        "expires_at": int(form["expires_at"]) if form.get("expires_at") else int(time.time()) + SESSION_LIFETIME_S,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "FakeStripe"

    def log_message(self, format: str, *args) -> None:
        pass  # one line per request is written by reply(), without headers or bodies

    def reply(self, status: int, body: dict | str | None = None, location: str | None = None) -> None:
        raw = b"" if body is None else (body.encode() if isinstance(body, str) else json.dumps(body).encode())
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8" if isinstance(body, str) else "application/json")
        self.send_header("Content-Length", str(len(raw)))
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(raw)
        log(f"{self.command} {self.path.split('?')[0]} -> {status}")

    def authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            user = b64decode(header[6:]).decode().split(":", 1)[0]
        except ValueError:
            return False
        return user.startswith("sk_test_")

    def read_body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path)
        body = self.read_body()
        if path.path == "/v1/checkout/sessions":
            self.create_session_request(body)
        elif path.path.startswith("/pay/") and path.path.endswith("/complete"):
            self.complete(path.path.split("/")[2])
        elif path.path.startswith("/_expire/"):
            self.expire(path.path.split("/")[2])
        elif path.path.startswith("/_replay/"):
            query = urllib.parse.parse_qs(path.query)
            self.replay(path.path.split("/")[2], int(query.get("times", ["1"])[0]))
        else:
            self.reply(404, {"error": "not found"})

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/v1/checkout/sessions/"):
            if not self.authorized():
                return self.reply(*stripe_error(401, "invalid_request_error", "api_key_invalid", "Invalid API key"))
            with state_lock:
                session = sessions.get(path.rsplit("/", 1)[1])
                copy = dict(session) if session else None
            if copy is None:
                return self.reply(*stripe_error(404, "invalid_request_error", "resource_missing", "No such checkout session"))
            self.reply(200, copy)
        elif path.startswith("/pay/"):
            self.pay_page(path.split("/")[2])
        elif path == "/_events":
            with state_lock:
                listing = [{key: value for key, value in event.items() if key != "body"} for event in events.values()]
            self.reply(200, {"sessions": len(sessions), "events": listing})
        else:
            self.reply(404, {"error": "not found"})

    def create_session_request(self, body: bytes) -> None:
        if not self.authorized():
            return self.reply(*stripe_error(401, "invalid_request_error", "api_key_invalid", "Invalid API key"))
        form = parse_form(body)
        key = self.headers.get("Idempotency-Key")
        if key:
            with state_lock:
                previous = idempotency.get(key)
                if previous is None:
                    idempotency[key] = {"params": form, "response": None}
            if previous is not None:
                if previous["params"] != form:
                    return self.reply(*stripe_error(
                        400, "idempotency_error", "idempotency_key_in_use",
                        "Keys for idempotent requests can only be used with the same parameters they were first used with."))
                if previous["response"] is None:
                    return self.reply(*stripe_error(
                        409, "invalid_request_error", "lock_timeout",
                        "A request with the same Idempotency-Key is still being processed."))
                return self.reply(*previous["response"])

        time.sleep(config["delay_ms"] / 1000)
        session = create_session(form)
        with state_lock:
            sessions[session["id"]] = session
            if key:
                idempotency[key]["response"] = (200, session)
        self.reply(200, session)

    def pay_page(self, session_id: str) -> None:
        with state_lock:
            session = sessions.get(session_id)
        if session is None:
            return self.reply(404, "<p>No such session</p>")
        amount = f"{session['amount_total'] / 100:,.2f}"
        if session["status"] != "open":
            return self.reply(200, f"<!doctype html><title>FAKE STRIPE</title><h1>FAKE STRIPE, local development only</h1><p>This session is {html.escape(session['status'])}.</p>")
        self.reply(200, f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>FAKE STRIPE</title></head>
<body style="font-family: sans-serif; max-width: 28rem; margin: 2rem auto; padding: 0 1rem;">
<h1>FAKE STRIPE, local development only</h1>
<p>No card is needed and nothing real is charged.</p>
<p>Amount: <strong>{html.escape(session['currency'].upper())} {amount}</strong></p>
<form method="post" action="/pay/{html.escape(session_id)}/complete"><button type="submit">Pay</button></form>
<p><a href="{html.escape(session['cancel_url'] or '/')}">Cancel</a></p>
</body></html>""")

    def complete(self, session_id: str) -> None:
        with state_lock:
            session = sessions.get(session_id)
            if session is not None and session["status"] == "open":
                session.update(status="complete", payment_status="paid", payment_intent="pi_test_" + secrets.token_hex(12))
                snapshot = dict(session)
            else:
                snapshot = None
        if session is None:
            return self.reply(404, "<p>No such session</p>")
        if snapshot is None:
            return self.reply(409, f"<p>This session is {html.escape(session['status'])}.</p>")
        send_in_background("checkout.session.completed", snapshot)
        self.reply(303, None, location=snapshot["success_url"] or "/")

    def expire(self, session_id: str) -> None:
        with state_lock:
            session = sessions.get(session_id)
            if session is not None and session["status"] == "open":
                session["status"] = "expired"
                snapshot = dict(session)
            else:
                snapshot = None
        if snapshot is None:
            return self.reply(404 if session is None else 409, {"error": "not an open session"})
        send_in_background("checkout.session.expired", snapshot)
        self.reply(200, {"id": session_id, "status": "expired"})

    def replay(self, event_id: str, times: int) -> None:
        with state_lock:
            event = events.get(event_id)
        if event is None:
            return self.reply(404, {"error": "no such event"})
        results = []
        threads = [threading.Thread(target=lambda: results.append(deliver(event))) for _ in range(times)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.reply(200, {"event": event_id, "statuses": results})


def main() -> None:
    parser = argparse.ArgumentParser(description="A local fake of the Stripe API used by the backend (development only)")
    parser.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 if the backend container cannot reach 127.0.0.1")
    parser.add_argument("--port", type=int, default=12111)
    parser.add_argument("--public-url", default="http://localhost:12111", help="the address a browser uses; goes into checkout urls")
    parser.add_argument("--webhook-url", default="http://localhost:8000/webhooks/stripe")
    parser.add_argument("--webhook-secret", default=os.environ.get("STRIPE_WEBHOOK_SECRET"), help="or env STRIPE_WEBHOOK_SECRET")
    parser.add_argument("--delay-ms", type=int, default=0, help="a pause inside session creation, to widen races")
    args = parser.parse_args()
    if not args.webhook_secret:
        parser.error("--webhook-secret (or env STRIPE_WEBHOOK_SECRET) is required")

    config.update(
        webhook_secret=args.webhook_secret, webhook_url=args.webhook_url, public_url=args.public_url.rstrip("/"), delay_ms=args.delay_ms
    )
    if args.host == "0.0.0.0":
        log("WARNING: listening on all interfaces; anyone on your network can reach this fake")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    log(f"fake Stripe on http://{args.host}:{args.port} (webhooks to {args.webhook_url}, delay {args.delay_ms} ms)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
