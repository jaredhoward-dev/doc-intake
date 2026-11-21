"""A fake local-model (Ollama-style) server for tests, run as its own process.

    python -I tests/fake_model.py CONFIG_JSON SEEN_JSONL

It binds 127.0.0.1 only, on a free port, and prints that port on stdout once it accepts connections. GET answers
like /api/tags; POST answers like /api/generate with the reply in CONFIG_JSON (re-read on every request, so a test
can change it while the server runs): {"reply": {...}, "raw": null | "text"}. Each POST body is appended to
SEEN_JSONL as one JSON line.

It runs apart from the test process so that process never has a thread: the reader forks for bounded reads, and
fork() in a multi-threaded process can deadlock (Python 3.12+ warns about it).
"""
import http.server
import json
import sys


def make_handler(config_path, seen_path):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self._send(200, {"models": []})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with open(seen_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(body) + "\n")
            with open(config_path, encoding="utf-8") as f:
                cfg = json.load(f)
            raw = cfg.get("raw")
            self._send(200, {"response": raw if raw is not None else json.dumps(cfg.get("reply"))})

        def _send(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def main(argv):
    config_path, seen_path = argv
    srv = http.server.HTTPServer(("127.0.0.1", 0), make_handler(config_path, seen_path))
    print(srv.server_address[1], flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main(sys.argv[1:])
