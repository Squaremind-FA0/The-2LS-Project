"""User mutations that must revoke outstanding access tokens.

Each helper bumps ``User.token_version`` so tokens minted before the change
fail the ``tv`` check in ``get_current_user``. Callers flush/commit.
"""

from app.models import Role, User
from app.security.passwords import hash_password


def deactivate_user(user: User) -> None:
    user.is_active = False
    user.bump_token_version()


def set_role(user: User, role: Role) -> None:
    if user.role == role:
        return
    user.role = role
    user.bump_token_version()


def set_password(user: User, plain: str) -> None:
    user.password_hash = hash_password(plain)
    user.bump_token_version()
