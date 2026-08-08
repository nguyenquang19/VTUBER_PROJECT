"""Test T4 — sanitize PII (Phase 8 data pipeline)."""
from __future__ import annotations

from pathlib import Path

import pytest

from services.data.sanitize import (
    configure_hash_salt,
    hash_viewer_id,
    mask_known_identifier,
    mask_pii,
)


class TestHashViewerId:
    def test_hashes_deterministic_16char_with_salt(self) -> None:
        salt = b"a" * 32
        h = hash_viewer_id("UCxq3fZZ_channel", salt=salt)
        assert len(h) == 18 and h.startswith("v_")
        assert h == hash_viewer_id("UCxq3fZZ_channel", salt=salt)
        assert "UCxq3fZZ" not in h                        # không lộ id gốc

    def test_different_salt_unlinks_same_viewer(self) -> None:
        first = hash_viewer_id("viewer", salt=b"a" * 32)
        second = hash_viewer_id("viewer", salt=b"b" * 32)
        assert first != second

    def test_local_salt_created_and_reused(self, tmp_path: Path) -> None:
        path = configure_hash_salt(tmp_path / "privacy_salt.bin")
        first = path.read_bytes()
        configure_hash_salt(path)
        assert len(first) >= 32
        assert path.read_bytes() == first

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

    @pytest.mark.parametrize("raw", [
        "nhắn cho @viewer_real nhé",
        "callback https://example.test/cb?access_token=secret123",
        "server 192.168.10.24",
        "CCCD: 012345678901",
        "tên tôi là Nguyễn Văn An",
        "địa chỉ: 12 Nguyễn Trãi quận 1",
    ])
    def test_common_identifier_fixture_masked(self, raw: str) -> None:
        assert "[PII]" in (mask_pii(raw) or "")

    def test_normal_url_unchanged(self) -> None:
        value = "xem https://example.test/news nhé"
        assert mask_pii(value) == value

    def test_structured_display_name_can_be_masked(self) -> None:
        assert mask_known_identifier("chào NguyenVan nhé", "NguyenVan") == "chào [PII] nhé"

    def test_clean_text_unchanged(self) -> None:
        assert mask_pii("Mai ơi chơi game gì thế") == "Mai ơi chơi game gì thế"

    def test_none_empty(self) -> None:
        assert mask_pii(None) is None
        assert mask_pii("") == ""
