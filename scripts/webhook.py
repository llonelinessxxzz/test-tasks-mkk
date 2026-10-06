import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        payload = json.loads(body)
        print(json.dumps(payload, ensure_ascii=False), flush=True)
        self.send_response(503 if self.path == "/fail" else 200)
        self.end_headers()
        self.wfile.write(b"ok")


if __name__ == "__main__":
    print("Webhook receiver: http://0.0.0.0:8081 (/fail returns 503)", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8081), WebhookHandler).serve_forever()
