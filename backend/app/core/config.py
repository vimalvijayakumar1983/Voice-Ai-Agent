import ipaddress
from typing import Literal
from urllib.parse import urlsplit

from pydantic import EmailStr, Field, field_validator, model_validator
from pydantic_settings import BaseSettings

from app.core.identity import normalize_email


class Settings(BaseSettings):
    # App
    app_name: str = "Voice AI Agent"
    app_env: str = "development"
    # Railway injects this value. A production Railway environment is
    # authoritative so a missing or misspelled APP_ENV cannot disable the
    # production startup gate or runtime security controls.
    railway_environment_name: str = ""
    app_debug: bool = False
    base_url: str = "http://localhost:8000"
    cors_origins: str = "http://localhost:3000"
    # Eight MiB accommodates normal control-plane JSON/imports. Provider
    # webhooks retain their tighter two MiB streaming limit at the route layer.
    max_request_body_bytes: int = Field(
        default=8 * 1024 * 1024,
        ge=2 * 1024 * 1024,
        le=64 * 1024 * 1024,
    )
    registration_mode: Literal["bootstrap", "invite_only", "open"] = "open"
    bootstrap_owner_email: EmailStr | None = Field(default=None, repr=False)
    # Railway overwrites X-Real-IP at its public edge. Keep this opt-in so a
    # non-Railway deployment never trusts a user-supplied forwarding header.
    trust_railway_proxy_headers: bool = False

    # Database
    database_url: str = "postgresql+asyncpg://voiceai:devpassword@localhost:5432/voiceai"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Auth
    secret_key: str = "change-me-in-production"
    # Dedicated envelope-encryption material for integration credentials. When
    # unset, SECRET_KEY is used only as a backwards-compatible fallback. API
    # and worker processes must receive the same value.
    integration_encryption_key: str = ""
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7
    # Token verification must never be switched to an attacker-influenced or
    # accidentally unsupported algorithm through deployment configuration.
    algorithm: Literal["HS256"] = "HS256"

    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_default_from_number: str = ""

    # Smallest.ai Atoms
    smallest_api_key: str = ""
    smallest_base_url: str = "https://api.smallest.ai/atoms/v1"
    smallest_webhook_secret: str = ""
    smallest_webhook_id: str = ""
    smallest_request_timeout_seconds: float = 30.0

    # AI Providers
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    # Stripe
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""

    model_config = {"env_file": ".env", "case_sensitive": False}

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        """Use SQLAlchemy's asyncpg driver with Railway-style Postgres URLs."""
        if value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql+asyncpg://", 1)
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+asyncpg://", 1)
        return value

    @field_validator("app_env", "railway_environment_name", mode="before")
    @classmethod
    def normalize_environment_name(cls, value: object) -> str:
        return str(value or "").strip().lower()

    @field_validator("registration_mode", mode="before")
    @classmethod
    def normalize_registration_mode(cls, value: object) -> object:
        return str(value).strip().lower()

    @field_validator("bootstrap_owner_email", mode="before")
    @classmethod
    def normalize_bootstrap_owner_email(cls, value: object) -> object:
        if value is None or not str(value).strip():
            return None
        return normalize_email(value)

    @property
    def cors_origin_list(self) -> list[str]:
        return [
            origin.strip().rstrip("/") for origin in self.cors_origins.split(",") if origin.strip()
        ]

    @model_validator(mode="after")
    def validate_production_security(self):
        """Refuse to boot production with forgeable or local-only defaults."""
        if self.railway_environment_name == "production":
            self.app_env = "production"

        if self.registration_mode == "bootstrap" and self.bootstrap_owner_email is None:
            raise ValueError(
                "Invalid registration configuration: BOOTSTRAP_OWNER_EMAIL is required "
                "when REGISTRATION_MODE=bootstrap"
            )

        if self.app_env.strip().lower() != "production":
            return self

        errors: list[str] = []
        if len(self.secret_key) < 32 or self.secret_key.startswith("change-me"):
            errors.append("SECRET_KEY must be a non-default value of at least 32 characters")
        if len(self.integration_encryption_key) < 32:
            errors.append("INTEGRATION_ENCRYPTION_KEY must be at least 32 characters")
        if self.integration_encryption_key == self.secret_key:
            errors.append("INTEGRATION_ENCRYPTION_KEY must be separate from SECRET_KEY")
        if not self.trust_railway_proxy_headers:
            errors.append(
                "TRUST_RAILWAY_PROXY_HEADERS must be enabled for production rate limiting"
            )
        if self.registration_mode == "open":
            errors.append("REGISTRATION_MODE=open is not allowed in production")

        base = urlsplit(self.base_url)
        if base.scheme != "https" or not base.hostname or _is_local_hostname(base.hostname):
            errors.append("BASE_URL must be a public HTTPS origin")

        origins = self.cors_origin_list
        if not origins:
            errors.append("CORS_ORIGINS must contain at least one console origin")
        for origin in origins:
            parsed = urlsplit(origin)
            if (
                origin == "*"
                or parsed.scheme != "https"
                or not parsed.hostname
                or _is_local_hostname(parsed.hostname)
            ):
                errors.append("CORS_ORIGINS must contain only public HTTPS origins")
                break

        redis = urlsplit(self.redis_url)
        if (
            redis.scheme not in {"redis", "rediss"}
            or not redis.hostname
            or _is_local_hostname(redis.hostname)
        ):
            errors.append("REDIS_URL must use redis:// or rediss:// with a non-local hostname")

        if len(self.smallest_api_key) < 20:
            errors.append("SMALLEST_API_KEY is required in production")
        if len(self.smallest_webhook_secret) < 16:
            errors.append("SMALLEST_WEBHOOK_SECRET must be at least 16 characters")
        if not self.smallest_webhook_id.strip():
            errors.append("SMALLEST_WEBHOOK_ID is required in production")
        twilio_values = (self.twilio_account_sid, self.twilio_auth_token)
        if any(twilio_values) and not all(twilio_values):
            errors.append("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be configured together")

        if errors:
            raise ValueError("Invalid production configuration: " + "; ".join(errors))
        return self


def _is_local_hostname(hostname: str) -> bool:
    normalized = hostname.rstrip(".").lower()
    if normalized in {"localhost", "host.docker.internal"} or normalized.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return not address.is_global


settings = Settings()
