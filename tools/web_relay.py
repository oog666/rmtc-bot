#!/usr/bin/env python3
"""把裁判端 127.0.0.1:8080 转发到有线网口，比赛机不用 SSH 隧道也能访问。

用法（在裁判机上，保持窗口不关）：
    ./tools/serve_web.sh                       # 默认监听 0.0.0.0:8080
    ./tools/serve_web.sh --listen 192.168.77.1 # 只监听有线地址

比赛机随后直接：
    浏览器  http://192.168.77.1:8080
    AI 地址 http://192.168.77.1:8080/v1/chat/completions

说明：官方裁判端只绑 127.0.0.1，本脚本在同一个端口上另开一个监听地址做 TCP 转发，
不改动裁判端本身。仅用于自测，别在有外网的场合长期开着。
"""

from __future__ import annotations

import argparse
import socket
import socketserver
import sys
import threading


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server: "_Server" = self.server  # type: ignore[assignment]
        try:
            upstream = socket.create_connection((server.target_host, server.target_port), timeout=5)
        except OSError:
            return
        upstream.settimeout(None)
        worker = threading.Thread(target=_pipe, args=(self.request, upstream), daemon=True)
        worker.start()
        _pipe(upstream, self.request)
        worker.join(timeout=3)
        try:
            upstream.close()
        except OSError:
            pass


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    target_host = "127.0.0.1"
    target_port = 8080


def main() -> int:
    ap = argparse.ArgumentParser(description="裁判端 8080 端口转发")
    ap.add_argument("--listen", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
    ap.add_argument("--port", type=int, default=8080, help="监听端口（默认 8080）")
    ap.add_argument("--target", default="127.0.0.1", help="裁判端地址（默认 127.0.0.1）")
    ap.add_argument("--target-port", type=int, default=8080, help="裁判端端口（默认 8080）")
    args = ap.parse_args()

    # 先确认上游在
    try:
        with socket.create_connection((args.target, args.target_port), timeout=3):
            pass
    except OSError as exc:
        print(f"上游 {args.target}:{args.target_port} 连不上（裁判端没启动？）：{exc}", file=sys.stderr)
        return 2

    _Server.target_host = args.target
    _Server.target_port = args.target_port
    try:
        server = _Server((args.listen, args.port), _Handler)
    except OSError as exc:
        print(f"监听 {args.listen}:{args.port} 失败：{exc}", file=sys.stderr)
        return 2

    print(f"转发已启动：http://{args.listen}:{args.port}  →  {args.target}:{args.target_port}", flush=True)
    print("比赛机浏览器打开上面这个地址；Ctrl+C 结束。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
