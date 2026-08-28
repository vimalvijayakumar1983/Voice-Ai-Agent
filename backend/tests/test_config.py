import pytest
from pydantic import ValidationError

from app.core.config import Settings

PRODUCTION_SETTINGS = {
    "app_env": "production",
    "secret_key": "s" * 32,
    "integration_encryption_key": "i" * 32,
    "base_url": "https://api.voice.example.com",
    "cors_origins": "https://voice.example.com",
    "redis_url": "redis://redis.railway.internal:6379/0",
    "trust_railway_proxy_headers": True,
    "registration_mode": "invite_only",
    "smallest_api_key": "sk_" + "a" * 32,
    "smallest_webhook_secret": "w" * 32,
    "smallest_webhook_id": "webhook-production-123",
}


def test_railway_postgres_url_uses_asyncpg_driver():
    settings = Settings(database_url="postgresql://user:pass@postgres.railway.internal:5432/app")

    assert settings.database_url == (
        "postgresql+asyncpg://user:pass@postgres.railway.internal:5432/app"
    )


def test_cors_origins_are_trimmed_and_empty_values_are_removed():
    settings = Settings(cors_origins=" https://app.example.com/, ,https://admin.example.com ")

    assert settings.cors_origin_list == [
        "https://app.example.com",
        "https://admin.example.com",
    ]


def test_valid_production_configuration_passes_startup_gate():
    settings = Settings(**PRODUCTION_SETTINGS)
    assert settings.app_env == "production"


@pytest.mark.parametrize("app_env", [None, "prodution"])
def test_railway_production_marker_is_authoritative(monkeypatch, app_env):
    for name, value in PRODUCTION_SETTINGS.items():
        if name != "app_env":
            monkeypatch.setenv(name.upper(), str(value))
    if app_env is None:
        monkeypatch.delenv("APP_ENV", raising=False)
    else:
        monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", " Production ")

    settings = Settings(_env_file=None)

    assert settings.app_env == "production"


def test_railway_production_marker_runs_startup_security_gate(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")

    with pytest.raises(ValidationError, match="Invalid production configuration"):
        Settings(_env_file=None)


def test_railway_production_marker_enforces_registration_gate():
    with pytest.raises(ValidationError, match="REGISTRATION_MODE=open"):
        Settings(
            **{
                **PRODUCTION_SETTINGS,
                "app_env": "prodution",
                "railway_environment_name": "production",
                "registration_mode": "open",
            }
        )


def test_jwt_algorithm_is_fixed_to_hs256():
    assert Settings().algorithm == "HS256"

    with pytest.raises(ValidationError, match="algorithm"):
        Settings(algorithm="ES256")


def test_refresh_cookie_name_is_bounded_and_legacy_bridge_defaults_off():
    settings = Settings()
    assert settings.refresh_cookie_name == "vai_refresh_token"
    assert settings.legacy_session_migration_enabled is False

    assert Settings(refresh_cookie_name="custom_refresh").refresh_cookie_name == "custom_refresh"
    with pytest.raises(ValidationError):
        Settings(refresh_cookie_name="invalid cookie name")


def test_request_body_limit_defaults_to_eight_mib_and_is_bounded():
    assert Settings().max_request_body_bytes == 8 * 1024 * 1024
    with pytest.raises(ValidationError):
        Settings(max_request_body_bytes=(2 * 1024 * 1024) - 1)
    with pytest.raises(ValidationError):
        Settings(max_request_body_bytes=(64 * 1024 * 1024) + 1)


def test_registration_modes_are_canonical_and_bootstrap_email_is_validated():
    settings = Settings(
        registration_mode=" BOOTSTRAP ",
        bootstrap_owner_email=" Owner@Example.COM ",
    )
    assert settings.registration_mode == "bootstrap"
    assert str(settings.bootstrap_owner_email) == "owner@example.com"
    assert "owner@example.com" not in repr(settings)

    with pytest.raises(ValidationError, match="BOOTSTRAP_OWNER_EMAIL is required"):
        Settings(registration_mode="bootstrap", bootstrap_owner_email="")
    with pytest.raises(ValidationError):
        Settings(registration_mode="bootstrap", bootstrap_owner_email="not-an-email")


def test_production_rejects_open_registration_and_accepts_bootstrap():
    with pytest.raises(ValidationError, match="REGISTRATION_MODE=open"):
        Settings(**{**PRODUCTION_SETTINGS, "registration_mode": "open"})

    settings = Settings(
        **{
            **PRODUCTION_SETTINGS,
            "registration_mode": "bootstrap",
            "bootstrap_owner_email": "owner@example.com",
        }
    )
    assert settings.registration_mode == "bootstrap"


@pytest.mark.parametrize(
    "overrides",
    [
        {"secret_key": "change-me-in-production"},
        {"integration_encryption_key": ""},
        {"base_url": "http://localhost:8000"},
        {"cors_origins": "*"},
        {"cors_origins": "http://localhost:3000"},
        {"cors_origins": "https://voice.example.com/console"},
        {"cors_origins": "https://user@voice.example.com"},
        {"cors_origins": "https://voice.example.com?tenant=1"},
        {"redis_url": "redis://localhost:6379/0"},
        {"redis_url": "https://redis.example.com"},
        {"trust_railway_proxy_headers": False},
        {"registration_mode": "open"},
        {"smallest_api_key": ""},
        {"smallest_webhook_secret": ""},
        {"smallest_webhook_id": ""},
        {"twilio_account_sid": "AC123", "twilio_auth_token": ""},
    ],
)
def test_insecure_production_configuration_fails_closed(overrides):
    with pytest.raises(ValidationError, match="Invalid production configuration"):
        Settings(**{**PRODUCTION_SETTINGS, **overrides})


def test_production_encryption_and_signing_keys_are_separate():
    with pytest.raises(ValidationError, match="must be separate"):
        Settings(
            **{
                **PRODUCTION_SETTINGS,
                "integration_encryption_key": PRODUCTION_SETTINGS["secret_key"],
            }
        )
