import uuid

from sqlalchemy import Boolean, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TenantScopedModel


class ProviderCredential(TenantScopedModel):
    __tablename__ = "provider_credentials"
    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", name="uq_provider_credentials_tenant_provider"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    # The complete credential payload is authenticated and encrypted. No
    # plaintext or masked derivative is stored in a browser-readable column.
    encrypted_config: Mapped[str] = mapped_column(Text, nullable=False)
    encryption_version: Mapped[int] = mapped_column(default=1, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
