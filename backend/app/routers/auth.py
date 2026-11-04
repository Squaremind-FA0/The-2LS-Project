from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.service import authenticate
from app.config import settings
from app.db import get_db
from app.schemas.auth import LoginRequest, TokenResponse
from app.security.tokens import create_access_token

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    # Public route (PUBLIC_API_PATHS); uses bare get_db because there is no
    # tenant before authentication.
    user = authenticate(db, payload.email, payload.password)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    return TokenResponse(
        access_token=create_access_token(user.id, user.token_version),
        expires_in=settings.ACCESS_TOKEN_TTL_MIN * 60,
    )
