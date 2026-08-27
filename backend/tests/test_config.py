from app.core.config import Settings


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
