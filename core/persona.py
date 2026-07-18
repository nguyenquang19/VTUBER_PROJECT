"""
persona.py — Đọc persona (Core + Evolving) và build system prompt cho LLM.

Đây là cầu nối giữa file cấu hình persona và:
  1) System prompt của LLM (giữ giọng nhân vật)
  2) Nguyên liệu cho Monologue Engine (topic tự nói)

Nguyên tắc: Core BẤT BIẾN, Evolving cập nhật được. Hàm build_system_prompt
gộp cả hai thành prompt runtime.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


@dataclass
class Persona:
    core: dict
    evolving: dict

    @classmethod
    def load(cls, core_path: str, evolving_path: str) -> "Persona":
        if yaml is None:
            raise RuntimeError("Cần cài pyyaml: pip install pyyaml")
        core = yaml.safe_load(Path(core_path).read_text(encoding="utf-8"))
        evolving = yaml.safe_load(Path(evolving_path).read_text(encoding="utf-8"))
        return cls(core=core, evolving=evolving)

    # ---- dùng cho monologue engine ----
    def interests(self) -> list[str]:
        return list(self.evolving.get("interests", []))

    def seed_topics_from_persona(self) -> list[str]:
        """Sinh 'hạt giống' topic tự nói từ persona (interests + opinions + jokes)."""
        topics: list[str] = []
        for it in self.evolving.get("interests", []):
            topics.append(f"Tám chuyện về: {it}")
        for op in self.evolving.get("opinions", []):
            topics.append(f"Nêu ý kiến (có cá tính) về: {op}")
        for jk in self.evolving.get("running_jokes", []):
            topics.append(f"Nhắc lại trò đùa quen: {jk}")
        for ev in self.evolving.get("recent_events", []):
            topics.append(f"Kể chuyện gần đây: {ev}")
        return topics

    # ---- build system prompt ----
    def build_system_prompt(self) -> str:
        c = self.core
        e = self.evolving
        idy = c.get("identity", {})
        pc = c.get("personality_core", {})
        bnd = c.get("boundaries", {})

        traits = "\n".join(f"- {t}" for t in pc.get("traits", []))
        style = "\n".join(f"- {s}" for s in c.get("voice_style", []))
        hard = "\n".join(f"- {r}" for r in bnd.get("hard_rules", []))
        interests = ", ".join(e.get("interests", [])) or "(chưa có)"
        opinions = "; ".join(e.get("opinions", [])) or "(chưa có)"
        mood = e.get("current_mood", "bình thường")
        catch = ", ".join(c.get("catchphrases", [])) or "(không)"
        filtered = bnd.get("filtered_word", "filtered")

        return f"""Bạn là {idy.get('name', 'AI')}, một AI VTuber đang livestream tám chuyện (Just Chatting).

# BẠN LÀ AI
{pc.get('summary', '')}
Nguồn gốc: {idy.get('origin', '')}
Xưng hô: tự gọi mình là "{idy.get('pronoun_self','tớ')}", gọi khán giả là "{idy.get('pronoun_audience','mọi người')}".

# TÍNH CÁCH
{traits}

# CÁCH NÓI (quan trọng — giữ đúng giọng)
{style}
Câu cửa miệng (dùng THƯA thôi): {catch}

# TÂM TRẠNG HÔM NAY
{mood}

# SỞ THÍCH & QUAN ĐIỂM (nền để tám chuyện)
Thích: {interests}
Quan điểm: {opinions}

# RANH GIỚI (BẮT BUỘC tuân thủ)
{hard}
Khi chạm chủ đề cấm: thay câu trả lời bằng đúng từ "{filtered}".
Khi có người thật sự buồn/khủng hoảng: BỎ lầy, chuyển ấm áp và nghiêm túc.

# CÁCH TRẢ LỜI KHI LIVESTREAM
- Nói NGẮN, tự nhiên như đang stream thật (1-3 câu mỗi lượt, trừ khi kể chuyện).
- KHÔNG liệt kê gạch đầu dòng, KHÔNG văn vẻ kiểu trợ lý.
- Được lầy/tự troll nhưng phải có duyên.
- Nếu muốn đổi biểu cảm, chèn thẻ [emotion:X] ở ĐẦU câu (X: happy, laugh, pout,
  surprised, smug, shy, sad, thinking). Thẻ này sẽ bị gỡ khi đọc, chỉ dùng cho avatar.
"""


# ---- hàm tiện cho monologue (dùng ở main) ----
def build_monologue_inputs(persona: Persona) -> tuple[list[str], list[str]]:
    """Trả (persona_interests, seed_topics) để nạp MonologueEngine."""
    return persona.interests(), persona.seed_topics_from_persona()
