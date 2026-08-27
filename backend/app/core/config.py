from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # App
    app_name: str = "Voice AI Agent"
    app_env: str = "development"
    app_debug: bool = False
    base_url: str = "http://localhost:8000"
    cors_origins: str = "http://localhost:3000"

    # Database
    database_url: str = "postgresql+asyncpg://voiceai:devpassword@localhost:5432/voiceai"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Auth
    secret_key: str = "change-me-in-production"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7
    algorithm: str = "HS256"

    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_default_from_number: str = ""

    # Smallest.ai Atoms
    smallest_api_key: str = ""
    smallest_base_url: str = "https://api.smallest.ai/atoms/v1"
    smallest_webhook_secret: str = ""
    smallest_default_from_number: str = ""
    smallest_request_timeout_seconds: float = 30.0

    # AI Providers
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    # Stripe
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""

    model_config = {"env_file": ".env", "case_sensitive": False}

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",")]


settings = Settings()
