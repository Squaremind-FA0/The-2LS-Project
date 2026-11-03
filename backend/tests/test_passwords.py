from app.security.passwords import hash_password, verify_password


def test_hash_verify_round_trip():
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed) is True


def test_wrong_password_does_not_verify():
    hashed = hash_password("correct horse battery staple")
    assert verify_password("Correct horse battery staple", hashed) is False


def test_hash_is_argon2id():
    assert hash_password("whatever-password").startswith("$argon2id$")


def test_long_password_is_not_truncated():
    long_password = "é" * 60 + "x" * 30  # 150 UTF-8 bytes
    assert len(long_password.encode("utf-8")) > 100
    hashed = hash_password(long_password)

    assert verify_password(long_password, hashed) is True
    # Same first 72 bytes, different tail: bcrypt would accept this, argon2 must not.
    variant = long_password[:-1] + "y"
    assert variant.encode("utf-8")[:72] == long_password.encode("utf-8")[:72]
    assert verify_password(variant, hashed) is False
    assert verify_password(long_password.encode("utf-8")[:72].decode("utf-8"), hashed) is False


def test_verify_against_malformed_hash_returns_false():
    assert verify_password("anything", "not-a-real-hash") is False
