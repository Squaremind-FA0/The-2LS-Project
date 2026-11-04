import pytest
from pydantic import ValidationError

from app.config import Settings


def test_jwt_secret_shorter_than_32_bytes_is_rejected():
    with pytest.raises(ValidationError):
        Settings(JWT_SECRET="x" * 31)


def test_jwt_secret_length_is_measured_in_utf8_bytes():
    # 16 two-byte characters == 32 bytes: accepted. 15 == 30 bytes: rejected.
    Settings(JWT_SECRET="é" * 16)
    with pytest.raises(ValidationError):
        Settings(JWT_SECRET="é" * 15)


def test_jwt_secret_of_32_bytes_is_accepted():
    assert Settings(JWT_SECRET="x" * 32).JWT_SECRET == "x" * 32


def test_access_token_ttl_defaults_to_60_minutes(monkeypatch):
    monkeypatch.delenv("ACCESS_TOKEN_TTL_MIN", raising=False)
    assert Settings(JWT_SECRET="x" * 32).ACCESS_TOKEN_TTL_MIN == 60


def test_access_token_ttl_is_overridable_via_env(monkeypatch):
    monkeypatch.setenv("ACCESS_TOKEN_TTL_MIN", "15")
    assert Settings(JWT_SECRET="x" * 32).ACCESS_TOKEN_TTL_MIN == 15
