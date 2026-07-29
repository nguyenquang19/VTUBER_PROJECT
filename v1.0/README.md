# Mai — AI VTuber (v1.0)

AI VTuber tiếng Việt chạy 100% local trên Windows 11 / RTX 5060 Ti 16GB.

## Stack
- **LLM:** llama.cpp (llama-server) + Gemma 4 12B Q4_K_M
- **STT:** faster-whisper small
- **TTS:** chốt sau Pre-flight Day 2
- **Storage:** SQLite + sqlite-vec
- **Dashboard:** FastAPI + Vanilla JS (Alpine.js từ Phase 6)

## Tài liệu
- [docs/QUICKSTART.md](docs/QUICKSTART.md) — stack tổng quan
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — spec đầy đủ
- [docs/PROCESS.md](docs/PROCESS.md) — kịch bản build từng phase
- [docs/CLAUDE.md](docs/CLAUDE.md) — rules cho Claude Code
- [docs/persona.md](docs/persona.md) — persona spec
- [STATE.md](STATE.md) — trạng thái build hiện tại

## Setup nhanh
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Xem [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) Appendix B + D cho checklist đầy đủ.
