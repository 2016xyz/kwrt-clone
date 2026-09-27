#!/usr/bin/env python3
"""本地 SMTP 测试服务器 —— 用于真实验证邮件发送链路（不用于生产）。"""
import asyncio
import email
import json
import os
import sys
from aiosmtpd.controller import Controller

OUT = "/tmp/smtp_inbox.jsonl"


class Handler:
    def __init__(self):
        self.n = 0

    async def handle_DATA(self, server, session, envelope):
        msg = email.message_from_bytes(envelope.content)
        body = ""
        html = ""
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                body = part.get_payload(decode=True).decode("utf-8", "ignore")
            elif ct == "text/html":
                html = part.get_payload(decode=True).decode("utf-8", "ignore")
        rec = {
            "seq": self.n,
            "from": envelope.mail_from,
            "to": envelope.rcpt_tos,
            "subject": str(msg.get("Subject", "")),
            "date": str(msg.get("Date", "")),
            "body": body,
            "html_len": len(html),
            "html_has_download_link": "/dl/t/" in html,
            "body_has_download_link": "/dl/t/" in body,
            "raw_len": len(envelope.content),
        }
        with open(OUT, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.n += 1
        print(f"[smtp] 收到邮件 #{self.n} from={envelope.mail_from} to={envelope.rcpt_tos} "
              f"subject={rec['subject']!r} html={rec['html_len']}b 链接={rec['html_has_download_link']}",
              flush=True)
        return "250 Message accepted for delivery"


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 2525
    if os.path.exists(OUT):
        os.remove(OUT)
    ctl = Controller(Handler(), hostname="127.0.0.1", port=port)
    ctl.start()
    print(f"[smtp] 本地测试 SMTP 已启动 127.0.0.1:{port}（收件记录 → {OUT}）", flush=True)
    try:
        asyncio.get_event_loop().run_forever()
    except KeyboardInterrupt:
        ctl.stop()