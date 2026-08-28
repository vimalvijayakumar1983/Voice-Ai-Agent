from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.services.integration_security import public_integration_config


class IntegrationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    integration_type: str = Field(min_length=1, max_length=50)
    config: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid", "str_strip_whitespace": True}


class IntegrationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    config: dict[str, Any] | None = Field(
        default=None,
        description="Omitted or null preserves the complete encrypted configuration.",
    )
    is_active: bool | None = None
    clear_secrets: list[str] = Field(
        default_factory=list,
        description="Dotted paths of write-only secret fields to remove explicitly.",
    )

    model_config = {"extra": "forbid", "str_strip_whitespace": True}

    @field_validator("name", "is_active")
    @classmethod
    def reject_null_non_nullable_fields(cls, value):
        if value is None:
            raise ValueError("Field cannot be null when provided")
        return value


class IntegrationResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    name: str
    integration_type: str
    config: dict[str, Any]
    secret_fields: list[str] = Field(default_factory=list)
    is_active: bool

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def redact_write_only_secrets(self):
        """Make secret redaction an invariant of the public response model."""
        self.config, detected_paths = public_integration_config(
            self.config,
            self.integration_type,
        )
        self.secret_fields = sorted(set(self.secret_fields).union(detected_paths))
        return self


class IntegrationEncryptionBackfillResponse(BaseModel):
    migrated: int = Field(ge=0)
    remaining: int = Field(ge=0)
