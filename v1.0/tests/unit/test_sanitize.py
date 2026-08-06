"""Test T4 — sanitize PII (Phase 8 data pipeline)."""
from __future__ import annotations

from services.data.sanitize import hash_viewer_id, mask_pii


class TestHashViewerId:
    def test_hashes_deterministic_8char(self) -> None:
        h = hash_viewer_id("UCxq3fZZ_channel")
        assert len(h) == 8
        assert h == hash_viewer_id("UCxq3fZZ_channel")   # ổn định
        assert "UCxq3fZZ" not in h                        # không lộ id gốc

    def test_none_and_empty(self) -> None:
        assert hash_viewer_id(None) is None
        assert hash_viewer_id("") is None

    def test_different_ids_different_hash(self) -> None:
        assert hash_viewer_id("a") != hash_viewer_id("b")


class TestMaskPII:
    def test_email_masked(self) -> None:
        assert "@" not in mask_pii("liên hệ abc@gmail.com nhé")
        assert "[PII]" in mask_pii("abc@gmail.com")

    def test_phone_masked(self) -> None:
        assert "[PII]" in mask_pii("gọi tớ 0912345678 đi")

    def test_token_masked(self) -> None:
        assert "[PII]" in mask_pii("key sk_live_abcdefghij1234567890xyz")

    def test_clean_text_unchanged(self) -> None:
        assert mask_pii("Mai ơi chơi game gì thế") == "Mai ơi chơi game gì thế"

    def test_none_empty(self) -> None:
        assert mask_pii(None) is None
        assert mask_pii("") == ""
