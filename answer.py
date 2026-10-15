from http.server import BaseHTTPRequestHandler
import json, requests

LEDGER_BASE = "https://<paste-real-ledger-base>"   # from your browser DevTools
HEADERS = {"Authorization": "Bearer <token>"}       # if required

def get(path):
    return requests.get(f"{LEDGER_BASE}/{path}", headers=HEADERS, timeout=10).json()

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        q = body.get("question", "").lower()

        answer = "unsupported"

        # --- Example logic; replace once we know the schema ---
        if "revenue" in q and "north" in q:
            orders = get("orders")
            total = sum(o["amount"] for o in orders
                        if o.get("region") == "North" and o.get("type") == "sale")
            answer = round(total, 2)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"answer": answer}).encode())