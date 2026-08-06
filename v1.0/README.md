# Mai — AI VTuber (v1.0)

AI VTuber tiếng Việt chạy 100% local trên Windows 11 / RTX 5060 Ti 16GB.

## Stack
- **LLM:** llama.cpp (llama-server) + Gemma 4 12B Q4_K_M
- **TTS:** VieNeu-TTS v3 Turbo (48kHz, GPU streaming)
- **STT:** faster-whisper small (Phase 5, deferred)
- **Storage:** SQLite + sqlite-vec
- **Dashboard:** FastAPI + Vanilla JS (canvas chart, 100% local)

## Tài liệu

Bộ tài liệu kỹ thuật canonical ở **[docs/dev_manual/](docs/dev_manual/README.md)** — đọc file đó trước.

- [docs/dev_manual/01_architecture.md](docs/dev_manual/01_architecture.md) — kiến trúc + data flow
- [docs/dev_manual/02_modules.md](docs/dev_manual/02_modules.md) — logic từng module
- [docs/dev_manual/03_operations.md](docs/dev_manual/03_operations.md) — chạy / config / dashboard / debug
- [docs/dev_manual/04_extending.md](docs/dev_manual/04_extending.md) — thêm module / đóng góp
- [docs/CLAUDE.md](docs/CLAUDE.md) — rules N1-N8 cho AI
- [docs/persona.md](docs/persona.md) — persona spec
- [STATE.md](STATE.md) — trạng thái build hiện tại

## Setup nhanh
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Xem [docs/dev_manual/03_operations.md](docs/dev_manual/03_operations.md) cho setup + chạy đầy đủ.
