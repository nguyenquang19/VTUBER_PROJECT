"""Công cụ test tay: chat với model qua llama-server (streaming nhanh).

Dùng cách đã chốt (xem STATE.md / memory reference-llm-model):
- endpoint /v1/chat/completions, server chạy với --reasoning off
- streaming qua asyncio.open_connection (raw socket) — TTFT ~72ms, KHÔNG httpx
- persona Mai rút gọn (bản đầy đủ để Phase 1.C)

Chạy (sau khi đã start server — xem hướng dẫn):
    .\venv\Scripts\python.exe scripts\chat_test.py
Gõ câu tiếng Việt, Enter. Gõ 'quit' để thoát.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")

HOST = "127.0.0.1"
PORT = 8080

# Persona Mai rút gọn cho test (bản đầy đủ Phase 1.C lấy từ persona.md A+C)
SYSTEM_PROMPT = (
    "Bạn là Mai, một AI VTuber nữ 18 tuổi, tính cách ngang, hay cãi, hay trêu, "
    "thích cà khịa nhưng thực ra quan tâm người khác. Tự xưng 'tớ', gọi người xem "
    "là 'cậu', gọi người vận hành là 'ông'. Trả lời NGẮN, tự nhiên, đúng chất trẻ "
    "và lém. Không nói kiểu trợ lý ('tôi có thể giúp gì'). Không tự nhận là AI trừ "
    "khi bị hỏi thẳng. Không khẩn cầu, không thao túng cảm xúc."
)


async def stream_chat(messages: list[dict], max_tokens: int = 200) -> tuple[str, float, float]:
    """Gửi messages, stream reply. Trả (text, ttft_ms, decode_tps)."""
    body = json.dumps(
        {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.85,
            "stream": True,
            "cache_prompt": True,
        }
    ).encode("utf-8")
    req = (
        b"POST /v1/chat/completions HTTP/1.1\r\n"
        b"Host: " + HOST.encode() + b"\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n" + body
    )

    reader, writer = await asyncio.open_connection(HOST, PORT)
    t0 = time.perf_counter()
    writer.write(req)
    await writer.drain()

    t_first: float | None = None
    tokens = 0
    parts: list[str] = []
    buf = b""
    header_done = False

    while True:
        chunk = await reader.read(4096)
        if not chunk:
            break
        buf += chunk
        if not header_done:
            if b"\r\n\r\n" in buf:
                header_done = True
                buf = buf.split(b"\r\n\r\n", 1)[1]
            else:
                continue
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            s = line.decode("utf-8", "replace")
            if "data:" not in s:
                continue
            data = s.split("data:", 1)[1].strip()
            if data == "[DONE]":
                writer.close()
                return _finish(parts, t0, t_first, tokens)
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            tok = obj["choices"][0].get("delta", {}).get("content")
            if tok:
                if t_first is None:
                    t_first = time.perf_counter()
                tokens += 1
                parts.append(tok)
                print(tok, end="", flush=True)
    writer.close()
    return _finish(parts, t0, t_first, tokens)


def _finish(parts, t0, t_first, tokens):
    text = "".join(parts)
    ttft = (t_first - t0) * 1000 if t_first else 0.0
    decode = tokens / (time.perf_counter() - t_first) if t_first and tokens > 1 else 0.0
    return text, ttft, decode


async def main() -> None:
    # kiểm tra server sống
    try:
        r, w = await asyncio.open_connection(HOST, PORT)
        w.close()
    except Exception:
        print(f"❌ Không kết nối được llama-server ở {HOST}:{PORT}. Bạn đã start server chưa?")
        return

    print("=" * 60)
    print("  CHAT TEST với Mai — gõ 'quit' để thoát")
    print("=" * 60)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    while True:
        try:
            user = input("\nBạn: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break
        if not user:
            continue
        if user.lower() in ("quit", "exit", "thoat", "thoát"):
            print("Bye!")
            break

        messages.append({"role": "user", "content": user})
        print("Mai: ", end="", flush=True)
        text, ttft, decode = await stream_chat(messages)
        messages.append({"role": "assistant", "content": text})
        print(f"\n   [TTFT={ttft:.0f}ms, {decode:.0f} tok/s]")


if __name__ == "__main__":
    asyncio.run(main())
