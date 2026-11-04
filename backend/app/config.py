from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = "sqlite:///./2ls.db"
    JWT_SECRET: str
    ACCESS_TOKEN_TTL_MIN: int = 60
    CORS_ORIGINS: list[str] = ["http://localhost:3000"]

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
