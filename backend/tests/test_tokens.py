import base64
import json
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.config import settings
from app.security.tokens import create_access_token, decode_access_token


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def test_token_round_trip_has_only_expected_claims():
    token = create_access_token(42, 3)
    claims = decode_access_token(token)

    assert set(claims) == {"sub", "tv", "iat", "exp"}
    assert claims["sub"] == "42"
    assert claims["tv"] == 3
    assert claims["exp"] - claims["iat"] == settings.ACCESS_TOKEN_TTL_MIN * 60
    assert "role" not in claims and "company_id" not in claims


def test_expired_token_is_rejected():
    ttl = timedelta(minutes=settings.ACCESS_TOKEN_TTL_MIN)
    token = create_access_token(1, 0, now=datetime.now(UTC) - ttl - timedelta(seconds=1))
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(token)


def test_token_signed_with_another_secret_is_rejected():
    now = datetime.now(UTC)
    claims = {"sub": "1", "tv": 0, "iat": now, "exp": now + timedelta(minutes=5)}
    token = jwt.encode(claims, "another-secret-that-is-at-least-32-bytes", algorithm="HS256")
    with pytest.raises(jwt.InvalidSignatureError):
        decode_access_token(token)


def test_alg_none_token_is_rejected():
    now = int(datetime.now(UTC).timestamp())
    claims = {"sub": "1", "tv": 0, "iat": now, "exp": now + 300}
    token = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(claims)}."
    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(token)


@pytest.mark.parametrize("missing", ["exp", "sub", "tv", "iat"])
def test_token_missing_a_required_claim_is_rejected(missing):
    now = datetime.now(UTC)
    claims = {"sub": "1", "tv": 0, "iat": now, "exp": now + timedelta(minutes=5)}
    del claims[missing]
    token = jwt.encode(claims, settings.JWT_SECRET, algorithm="HS256")
    with pytest.raises(jwt.MissingRequiredClaimError):
        decode_access_token(token)
