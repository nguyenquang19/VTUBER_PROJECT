# Baselines — B0 (ROADMAP_AUTONOMOUS_HOST §BƯỚC 0)

Chỗ lưu snapshot 4 metric TRƯỚC mỗi thay đổi lớn (Phase A, C0, …).
Không có mốc → không so được sau khi tune.

## Quy trình đo 1 baseline

1. Chạy Mai 30' liên tục (chat thật hoặc replay). turns.jsonl sẽ tự ghi qua LLMTurnRunner.
2. Chạy `python scripts/eval_transcript.py --since <ISO khi bắt đầu>` → dán số vào 1 file mới `YYYYMMDD_<tag>.md` (copy template dưới).
3. Human-rate 20 câu Mai ngẫu nhiên trong log → thang 1-10 (naturalness/hostness). Không tự chấm.
4. Commit file baseline.

## Target sau Phase A (§BƯỚC 0 bảng metric)

| Metric | Target |
|---|---|
| opener_repeat_ratio | <0.10 |
| dead_air > 10s | thấy được, giảm dần sau C0 |
| mood_exposition_count | **= 0 sau A1** |
| naturalness/hostness | ≥7/10 |

## Template — copy vào `YYYYMMDD_<tag>.md`

```md
# Baseline YYYY-MM-DD — <tag>

- Git ref: <commit sha>
- Stream length: 30 min
- Source: <youtube live / discord / replay / simulated>
- Command: `python scripts/eval_transcript.py --since <ISO> --file logs/turns.jsonl`

## Auto metrics

| Metric | Value | Target |
|---|---|---|
| total_turns |  |  |
| chat_reply / ambient |  /  |  |
| opener_repeat_ratio |  | <0.10 |
| opener top-5 |  |  |
| dead_air >10s (count) |  | giảm sau C0 |
| dead_air gap max (s) |  |  |
| mood_exposition_count |  | 0 sau A1 |
| parse_fail_count |  |  |

## Human rate (20 câu ngẫu nhiên)

- Naturalness (1-10):
- Hostness (1-10):
- 3 câu tệ nhất — trích + lý do:
  1.
  2.
  3.
- 3 câu ổn nhất:
  1.
  2.
  3.

## Ghi chú free-form

<những gì lộ pattern máy, dead-air lâu ở đâu, mood bám hay lệch, chat bị bỏ, v.v.>
```
