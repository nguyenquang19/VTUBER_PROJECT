# 🎀 AI VTuber (V1 — Discord-only)

![CI](https://github.com/nguyenquang19/VTUBER_PROJECT.git)

AI VTuber tự chủ kiểu Neuro-sama, chạy local: **tự tám chuyện (Just Chatting)**,
trả lời chat Discord bằng giọng nói, avatar Live2D nhép miệng + biểu cảm, và
**biết lớn lên** qua reflection sau mỗi buổi live.

> **Trạng thái:** Khung (skeleton) chạy được với mock. Đang ghép model thật (Bước A→E).

## ✨ Đặc điểm
- 🗣️ **Tự nói có mạch** — không chờ chat; luôn có monologue engine làm nền.
- 💬 **Turn-taking hai chiều** — chat chen vào, AI phản hồi rồi *về mạch*.
- ⚡ **Barge-in** — tin quan trọng (mod/donate) cắt ngang câu đang nói.
- 🧠 **Biết lớn lên** — memory + reflection cập nhật persona mỗi tuần.
- 🎭 **Persona 2 tầng** — Core bất biến + Evolving tiến hóa (chống trôi tính cách).

## 🚀 Chạy thử ngay (không cần GPU)
```bash
pip install -r requirements.txt
python main.py                    # demo tự nói + chat + barge-in
python -m tests.test_barge_in
python -m tests.test_persona
```

## 📁 Cấu trúc
Xem `docs/architecture.md`. Tóm tắt:
- `core/` — logic thuần (bus, orchestrator, monologue, persona, emotion).
- `modules/inputs/` — nguồn chat (fake_chat mock, discord_bot thật).
- `modules/outputs/` — LLM (vLLM), TTS.
- `modules/avatar/` — VTube Studio.
- `memory/` — bộ nhớ + reflection.
- `config/` — persona + tham số vận hành.

## 🗺️ Lộ trình
Xem `ROADMAP.md`.

## ⚙️ Cài đặt đầy đủ
Xem `docs/setup.md`.

## 📜 License
MIT (code). Live2D model / giọng clone / asset bên thứ ba có license RIÊNG — xem `LICENSE`.

## 🐳 Hạ tầng (Qdrant + Redis)
```bash
docker compose up -d        # khởi động vector DB + Redis (cho memory/Bước E)
docker compose ps           # kiểm tra
docker compose down         # dừng
```

## ✅ Test
```bash
pip install pytest pytest-asyncio
pytest -v                   # 11 test: barge-in + persona + emotion parser
```
> CI tự chạy các test này mỗi lần push (xem `.github/workflows/ci.yml`).
> Đổi `USERNAME/REPO` trong badge CI ở đầu README thành repo thật của bạn.
