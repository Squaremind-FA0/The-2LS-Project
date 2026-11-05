from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = "sqlite:///./2ls.db"
    JWT_SECRET: str
    ACCESS_TOKEN_TTL_MIN: int = 60
    CORS_ORIGINS: list[str] = ["http://localhost:3000"]

    # Dispatch (G3): matching radius, location freshness, offer ring + TTL,
    # and the sweeper's re-offer bound.
    DISPATCH_RADIUS_KM: float = 10.0
    DISPATCH_LOCATION_MAX_AGE_MIN: float = 5.0
    DISPATCH_RING_SIZE: int = 10
    DISPATCH_OFFER_TTL_S: int = 30
    DISPATCH_MAX_ROUNDS: int = 5

    # Realtime offer push (G4): first-message auth deadline, server heartbeat
    # period (a peer silent for two periods is dropped) and per-connection
    # outbound queue bound.
    WS_AUTH_TIMEOUT_S: float = 5.0
    WS_HEARTBEAT_S: float = 30.0
    WS_QUEUE_MAX: int = 256

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, value):
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("JWT_SECRET")
    @classmethod
    def _secret_long_enough(cls, value: str) -> str:
        # HS256 keys shorter than the 256-bit hash output weaken the MAC.
        if len(value.encode("utf-8")) < 32:
            raise ValueError("JWT_SECRET must be at least 32 bytes (UTF-8 encoded)")
        return value


settings = Settings()
