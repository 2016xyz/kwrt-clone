#!/usr/bin/env python3
"""一个最小的**真实 SMTP 服务器**，用于端到端验证发信链路。

为什么不用 mock：本项目已多次踩到「单元测试全绿、真实链路不通」——
所以发信验证必须走真实的 SMTP 握手（EHLO/MAIL FROM/RCPT TO/DATA），
并且把收到的原始报文留给断言去解析。

用法（作为库）：
    from tools._smtp_sink import Sink
    s = Sink(("127.0.0.1", 0)); s.start()
    ...                       # 触发发信
    msg = s.wait(timeout=10)  # 拿到第一封（原始 RFC822 文本）
    s.stop()

也可直接跑：
    python3 tools/_smtp_sink.py 2525     # 前台监听并把邮件打到 stdout
"""
from __future__ import annotations

import re
import socketserver
import threading
import time

__all__ = ["Sink"]


class _Handler(socketserver.StreamRequestHandler):
    """极简 SMTP：够 smtplib 用即可（不实现 AUTH，故测试时 mail.user 留空）。"""

    def _send(self, line: str):
        self.wfile.write((line + "\r\n").encode())
        self.wfile.flush()

    def handle(self):
        srv: "Sink" = self.server.sink  # type: ignore[attr-defined]
        self._send("220 smtp-sink ESMTP ready")
        from_addr = ""
        rcpts: list[str] = []
        in_data = False
        buf: list[str] = []
        while True:
            raw = self.rfile.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if in_data:
                if line == ".":
                    in_data = False
                    srv.record(from_addr, rcpts, "\n".join(buf))
                    buf = []
                    self._send("250 OK queued")
                else:
                    # SMTP 点透明：以 "." 开头的行会被发送方加倍，这里还原
                    buf.append(line[1:] if line.startswith("..") else line)
                continue
            up = line.upper()
            if up.startswith("EHLO") or up.startswith("HELO"):
                self._send("250-smtp-sink")
                self._send("250-8BITMIME")
                self._send("250 SIZE 104857600")
            elif up.startswith("MAIL FROM:"):
                m = re.search(r"<([^>]*)>", line)
                from_addr = m.group(1) if m else line.split(":", 1)[1].strip()
                rcpts = []
                self._send("250 OK")
            elif up.startswith("RCPT TO:"):
                m = re.search(r"<([^>]*)>", line)
                if m:
                    rcpts.append(m.group(1))
                self._send("250 OK")
            elif up.startswith("DATA"):
                in_data = True
                self._send("354 End data with <CR><LF>.<CR><LF>")
            elif up.startswith("RSET"):
                from_addr, rcpts, buf, in_data = "", [], [], False
                self._send("250 OK")
            elif up.startswith("NOOP"):
                self._send("250 OK")
            elif up.startswith("QUIT"):
                self._send("221 Bye")
                break
            else:
                self._send("250 OK")


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class Sink:
    """SMTP 收件箱。线程安全，可等到 N 封。"""

    def __init__(self, addr=("127.0.0.1", 2525)):
        self._srv = _Server(addr, _Handler)
        self._srv.sink = self  # type: ignore[attr-defined]
        self.host, self.port = self._srv.server_address[0], self._srv.server_address[1]
        self._lock = threading.Lock()
        self._msgs: list[dict] = []
        self._th: threading.Thread | None = None

    # ---- 生命周期 ----
    def start(self):
        self._th = threading.Thread(target=self._srv.serve_forever,
                                    kwargs={"poll_interval": 0.1}, daemon=True)
        self._th.start()
        return self

    def stop(self):
        try:
            self._srv.shutdown()
            self._srv.server_close()
        except Exception:
            pass

    # ---- 记录 / 读取 ----
    def record(self, frm: str, to: list[str], data: str):
        with self._lock:
            self._msgs.append({"from": frm, "to": list(to), "data": data,
                               "at": time.time()})

    def wait(self, n: int = 1, timeout: float = 10.0) -> dict | None:
        """等到第 n 封（1-based）出现；超时返回 None。"""
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                if len(self._msgs) >= n:
                    return self._msgs[n - 1]
            time.sleep(0.05)
        return None

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._msgs)

    def all(self) -> list[dict]:
        with self._lock:
            return list(self._msgs)

    def clear(self):
        with self._lock:
            self._msgs.clear()


def _main(port: int):
    s = Sink(("127.0.0.1", port)).start()
    print(f"[sink] 监听 127.0.0.1:{s.port}（Ctrl-C 退出）", flush=True)
    try:
        while True:
            time.sleep(0.5)
            while s.count:
                m = s.all()[0]
                with s._lock:  # noqa: SLF001  —— 单进程调试脚本，直接消费
                    s._msgs.pop(0)
                print(f"[sink] {m['from']} -> {m['to']}\n" + "-" * 60, flush=True)
                print(m["data"], flush=True)
                print("-" * 60, flush=True)
    except KeyboardInterrupt:
        s.stop()


if __name__ == "__main__":
    import sys
    _main(int(sys.argv[1]) if len(sys.argv) > 1 else 2525)
