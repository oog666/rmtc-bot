#!/usr/bin/env python3
"""校验 LLM 请求体是否符合上游接口：thinking / response_format / max_tokens 等。

不依赖 ROS，直接起一个本地 HTTP 替身，抓取真实请求体。

    python3 tools/test_llm_payload.py
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "contestant_ws/src/rmtc_bot"))

from rmtc_bot.llm import LLMClient, LLMRequest  # noqa: E402

RECEIVED: list[dict] = []
MODE = {"reject_json": False, "reject_nothink": False}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        RECEIVED.append({"body": body, "auth": self.headers.get("Authorization", "")})

        if MODE["reject_json"] and "response_format" in body:
            payload = {"error": {"message": "response_format unsupported", "code": "invalid_request_error"}}
            self._send(400, payload)
            return
        if MODE["reject_nothink"] and "thinking" in body:
            payload = {"error": {"message": "thinking unsupported", "code": "invalid_request_error"}}
            self._send(400, payload)
            return

        content = json.dumps({"events": [], "focus": "resources", "aggression": 0.3},
                             ensure_ascii=False)
        self._send(200, {"choices": [{"message": {"content": content}}]})

    def _send(self, code: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:
        pass


def start_server() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    return server, f"http://{host}:{port}/v1/chat/completions"


def req() -> LLMRequest:
    return LLMRequest(kind="brief", prompt="只输出一个 JSON 对象", tick=12, session="s1")


def check(name: str, cond: bool, detail: str = "") -> bool:
    ok = bool(cond)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail and not ok else ''}")
    return ok


def main() -> int:
    server, url = start_server()
    ok = True

    print("[1] 默认请求体")
    RECEIVED.clear()
    MODE.update(reject_json=False, reject_nothink=False)
    client = LLMClient(url, "test-token", model="deepseek-flash")
    res = client._call(req())
    body = RECEIVED[-1]["body"] if RECEIVED else {}
    ok &= check("调用成功", res.ok, res.error)
    ok &= check("model=deepseek-flash", body.get("model") == "deepseek-flash", str(body.get("model")))
    ok &= check("thinking 已关闭", body.get("thinking") == {"type": "disabled"}, str(body.get("thinking")))
    ok &= check("response_format=json_object",
                body.get("response_format") == {"type": "json_object"}, str(body.get("response_format")))
    ok &= check("max_tokens=512", body.get("max_tokens") == 512, str(body.get("max_tokens")))
    ok &= check("temperature 存在", "temperature" in body)
    ok &= check("messages 是非空数组", isinstance(body.get("messages"), list) and body["messages"])
    ok &= check("stream=False", body.get("stream") is False)
    ok &= check("Bearer 令牌", RECEIVED[-1]["auth"] == "Bearer test-token", RECEIVED[-1]["auth"])

    print("[2] 上游拒绝 response_format → 自动降级重试")
    RECEIVED.clear()
    MODE.update(reject_json=True, reject_nothink=False)
    client2 = LLMClient(url, "test-token", json_mode=True)
    res2 = client2._call(req())
    ok &= check("降级后仍成功", res2.ok, res2.error)
    ok &= check("共发 2 次", len(RECEIVED) == 2, str(len(RECEIVED)))
    if len(RECEIVED) == 2:
        ok &= check("第 1 次带 response_format", "response_format" in RECEIVED[0]["body"])
        ok &= check("第 2 次去掉 response_format", "response_format" not in RECEIVED[1]["body"])
        ok &= check("第 2 次仍关闭 thinking",
                    RECEIVED[1]["body"].get("thinking") == {"type": "disabled"})

    print("[3] 上游同时拒绝 thinking → 再降一级")
    RECEIVED.clear()
    MODE.update(reject_json=True, reject_nothink=True)
    client3 = LLMClient(url, "test-token")
    res3 = client3._call(req())
    ok &= check("最终仍成功", res3.ok, res3.error)
    ok &= check("最后一次不含 thinking", "thinking" not in RECEIVED[-1]["body"]) if RECEIVED else False

    print("[4] 关闭 thinking / json_mode 的配置生效")
    RECEIVED.clear()
    MODE.update(reject_json=False, reject_nothink=False)
    client4 = LLMClient(url, "test-token", thinking_disabled=False, json_mode=False, max_tokens=128)
    res4 = client4._call(req())
    body4 = RECEIVED[-1]["body"] if RECEIVED else {}
    ok &= check("调用成功", res4.ok, res4.error)
    ok &= check("不带 thinking", "thinking" not in body4)
    ok &= check("不带 response_format", "response_format" not in body4)
    ok &= check("max_tokens=128", body4.get("max_tokens") == 128)

    server.shutdown()
    print("\n结果：" + ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
