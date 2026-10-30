"""本地 LLM 网关替身：返回固定的事件解析 JSON，用于验证请求链路（仅测试用）。"""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import sys


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        content = json.dumps({
            "events": [
                {"type": "explosion", "start_tick": 25, "end_tick": 26,
                 "rect": [[3, 3], [7, 7]], "note": "mock"},
                {"type": "angel", "start_tick": 30, "end_tick": 46,
                 "points": [[3, 9], [14, 9]], "note": "mock"},
            ],
            "focus": "resources",
            "aggression": 0.5,
            "tactic": "mock-gateway",
        }, ensure_ascii=False)
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
