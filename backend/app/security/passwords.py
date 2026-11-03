"""Password hashing (Argon2id via pwdlib).

Argon2 has no 72-byte input limit, unlike bcrypt, so long passphrases are
hashed in full.
"""

from pwdlib import PasswordHash
from pwdlib.exceptions import PwdlibError

_hasher = PasswordHash.recommended()


def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if ``plain`` matches ``hashed``. Never raises on mismatch
    or on a malformed / unknown hash string."""
    try:
        return _hasher.verify(plain, hashed)
    except PwdlibError:
        return False
