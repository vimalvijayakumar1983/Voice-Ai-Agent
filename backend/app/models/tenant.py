from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel


class Tenant(BaseModel):
    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    domain: Mapped[str | None] = mapped_column(String(255), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    settings: Mapped[str | None] = mapped_column(Text)  # JSON blob for tenant-level config

    # Relationships
    users = relationship("User", back_populates="tenant", lazy="selectin")
    agents = relationship("Agent", back_populates="tenant", lazy="selectin")
