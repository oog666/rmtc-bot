"""统一 AI 网关的异步客户端。

设计约束（规则 §11 + 通信协议 §5）：
* 只用 /v1/chat/completions，Bearer 令牌；
* 请求必须异步，绝不能阻塞 {@link Brain} 的逐 Tick 决策；
* 单席位最多 2 个在途请求，这里用单工作线程（并发 1）最稳；
* 429/quota_exceeded 要能识别并降级；5xx/超时可重试；
* 每场至少要有一条成功调用（否则任务分 M=0），因此启动阶段会持续重试"简报"请求。
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class LLMRequest:
    kind: str
    prompt: str
    tick: int
    session: str
    attempts: int = 0


@dataclass
class LLMResult:
    kind: str
    ok: bool
    content: str = ""
    tick: int = -1
    session: str = ""
    error: str = ""
    latency: float = 0.0


class LLMClient:
    def __init__(
        self,
        url: str,
        token: str,
        model: str = "",
        timeout: float = 4.5,
        max_inflight: int = 1,
        thinking_disabled: bool = True,
        json_mode: bool = True,
        max_tokens: int = 512,
        temperature: float = 0.1,
    ) -> None:
        self.url = (url or "").strip()
        self.token = (token or "").strip()
        # 网关固定 deepseek-flash；仍然显式带上，便于日志与本地替身校验
        self.model = (model or "").strip() or "deepseek-flash"
        self.thinking_disabled = thinking_disabled
        self.json_mode = json_mode
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.max_attempts = 3
        self.enabled = bool(self.url and self.token)
        # 主办方每场限量 100 次调用（500 Tick）。留 20 次余量，默认封顶 80，
        # 可用 RMTC_LLM_BUDGET 调整；到顶后不再发起请求，策略自动退化为"无事件版本"。
        try:
            self.budget = max(0, int(os.environ.get("RMTC_LLM_BUDGET", "80")))
        except ValueError:
            self.budget = 80

        self._q: "queue.Queue[LLMRequest]" = queue.Queue(maxsize=24)
        self._results: "queue.Queue[LLMResult]" = queue.Queue()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

        # 统计（不记录令牌与提示词）
        self.calls = 0          # 逻辑请求数（含失败）
        self.attempts = 0       # 真实 HTTP 次数（受 100 次配额约束的就是这个）
        self.success = 0
        self.failures = 0
        self.quota_exhausted = False
        self.budget_hit = False
        self.last_error = ""
        self.last_success_time = 0.0
        self.last_success_tick = -1
        self.last_latency = 0.0

    # ---------- 生命周期 ----------
    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="rmtc-llm", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    # ---------- 对外接口 ----------
    def submit(self, kind: str, prompt: str, tick: int, session: str) -> bool:
        """非阻塞投递。队列满时丢弃最旧请求，保证新信息优先。"""
        if not self.enabled or self.quota_exhausted:
            return False
        with self._lock:
            if self.attempts >= self.budget:
                if not self.budget_hit:
                    self.budget_hit = True
                    self.last_error = "budget_exhausted"
                return False
        req = LLMRequest(kind=kind, prompt=prompt, tick=tick, session=session)
        try:
            self._q.put_nowait(req)
            return True
        except queue.Full:
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(req)
            except queue.Full:
                return False
        return True

    def drain(self) -> List[LLMResult]:
        out: List[LLMResult] = []
        while True:
            try:
                out.append(self._results.get_nowait())
            except queue.Empty:
                break
        return out

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "calls": self.calls,
                "success": self.success,
                "failures": self.failures,
                "quota_exhausted": self.quota_exhausted,
                "attempts": self.attempts,
                "budget": self.budget,
                "remaining": max(0, self.budget - self.attempts),
                "budget_hit": self.budget_hit,
                "last_error": self.last_error,
                "latency": round(self.last_latency, 3),
            }

    # ---------- 内部实现 ----------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                req = self._q.get(timeout=0.25)
            except queue.Empty:
                continue
            if req.session and self._is_stale(req):
                continue
            res = self._call(req)
            self._results.put(res)
            if res.ok:
                with self._lock:
                    self.success += 1
                    self.last_success_time = time.time()
                    self.last_success_tick = req.tick
            elif res.error == "quota_exceeded":
                self.quota_exhausted = True
                self._q.queue.clear()
            elif res.error in ("rate_limit", "concurrency_limit") and req.attempts < 2:
                req.attempts += 1
                time.sleep(0.3 * req.attempts)
                try:
                    self._q.put_nowait(req)
                except queue.Full:
                    pass

    def _is_stale(self, req: LLMRequest) -> bool:
        # 由 Brain 在投递时携带 session；这里只做"排空时不要堆积"的粗过滤
        return False

    def _payload(self, req: LLMRequest, use_json: bool, use_nothink: bool) -> Dict[str, Any]:
        """按官方接口构造请求体。

        网关除 model 外原样转发，所以字段取舍完全由这里决定：
        * thinking 顶层关闭 → 实时决策不需要长思考，延迟和 token 都省
        * response_format=json_object → 让模型只吐 JSON，解析更稳
        * max_tokens 封顶 → 防止异常长输出拖慢回调
        """
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": req.prompt}],
            "stream": False,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if use_nothink:
            payload["thinking"] = {"type": "disabled"}
        if use_json:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _call(self, req: LLMRequest) -> LLMResult:
        use_json = self.json_mode
        use_nothink = self.thinking_disabled
        attempt = 0
        last_err = ""
        while attempt < self.max_attempts and not self._stop.is_set():
            with self._lock:
                if self.attempts >= self.budget:
                    last_err = "budget_exhausted"
                    self.budget_hit = True
                    break
                self.attempts += 1
            attempt += 1
            payload = self._payload(req, use_json, use_nothink)
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.token,
                "Content-Length": str(len(data)),
            }
            started = time.time()
            try:
                request = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    body = resp.read()
                latency = time.time() - started
                self.last_latency = latency
                text = json.loads(body.decode("utf-8"))
                content = ""
                try:
                    content = text["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError):
                    content = ""
                if not isinstance(content, str) or not content.strip():
                    last_err = "empty_content"
                    continue
                with self._lock:
                    self.calls += 1
                return LLMResult(kind=req.kind, ok=True, content=content,
                                 tick=req.tick, session=req.session, latency=latency)
            except urllib.error.HTTPError as exc:
                code = exc.code
                detail = ""
                try:
                    raw = exc.read().decode("utf-8", "ignore")
                    obj = json.loads(raw)
                    detail = str(obj.get("error", {}).get("code", "")) if isinstance(obj, dict) else ""
                except Exception:
                    detail = ""
                last_err = detail or f"http_{code}"
                # 400 往往意味着上游不认某个可选字段：先退 json_mode，再退 thinking，别整条链路废掉
                if code == 400 and use_json:
                    use_json = False
                    last_err = "json_mode_rejected"
                    continue
                if code == 400 and use_nothink:
                    use_nothink = False
                    last_err = "thinking_param_rejected"
                    continue
                if code == 429:
                    if detail == "quota_exceeded":
                        break
                    time.sleep(0.25 * attempt)
                    continue
                if code in (401, 403, 404, 413, 501):
                    break
                if code in (500, 502, 503, 504):
                    time.sleep(0.2 * attempt)
                    continue
                break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_err = f"net:{type(exc).__name__}"
                time.sleep(0.15 * attempt)
                continue
            except (ValueError, KeyError):
                last_err = "bad_response"
                break
        with self._lock:
            self.calls += 1
            self.failures += 1
            self.last_error = last_err
        return LLMResult(kind=req.kind, ok=False, error=last_err, tick=req.tick, session=req.session)
