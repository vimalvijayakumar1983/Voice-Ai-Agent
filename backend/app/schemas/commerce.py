from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

CommerceStatus = Literal[
    "active",
    "checkout_ready",
    "awaiting_confirmation",
    "confirmed",
    "submitting",
    "completed",
    "failed",
    "expired",
]
PaymentMethod = Literal["cod", "store_pickup", "hosted_card"]


class CommerceSessionCreate(BaseModel):
    agent_id: UUID | None = None
    channel: Literal["web_voice", "phone", "operator"] = "web_voice"


class CommerceSearchRequest(BaseModel):
    query: Annotated[str, Field(min_length=2, max_length=120)]
    limit: Annotated[int, Field(ge=1, le=10)] = 5

    @field_validator("query")
    @classmethod
    def clean_query(cls, value: str) -> str:
        return " ".join(value.split())


class CommerceProductRequest(BaseModel):
    product_path: Annotated[str, Field(min_length=2, max_length=500)]


class CommerceCartItemRequest(CommerceProductRequest):
    quantity: Annotated[int, Field(ge=1, le=99)] = 1


class CommerceCustomer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_name: Annotated[str, Field(min_length=1, max_length=80)]
    last_name: Annotated[str, Field(min_length=1, max_length=80)]
    phone: Annotated[str, Field(pattern=r"^\+[1-9]\d{7,14}$")]
    email: Annotated[str, Field(min_length=3, max_length=254)]
    address_line_1: Annotated[str, Field(min_length=4, max_length=200)]
    address_line_2: Annotated[str, Field(max_length=200)] = ""
    city: Annotated[str, Field(min_length=2, max_length=100)]
    emirate: Literal[
        "Abu Dhabi", "Dubai", "Sharjah", "Ajman", "Umm Al Quwain", "Ras Al Khaimah", "Fujairah"
    ]
    landmark: Annotated[str, Field(max_length=160)] = ""


class CommerceCheckoutRequest(BaseModel):
    customer: CommerceCustomer
    payment_method: PaymentMethod


class CommerceConfirmationRequest(BaseModel):
    confirmation_text: Annotated[str, Field(min_length=5, max_length=80)]


class CommerceSubmitRequest(BaseModel):
    confirmation_id: Annotated[str, Field(min_length=16, max_length=64)]


class CommerceActionResponse(BaseModel):
    id: UUID
    session_id: UUID
    action_type: str
    status: str
    request_summary: dict
    result_summary: dict
    error_message: str | None
    duration_ms: int | None
    created_at: datetime

    model_config = {"from_attributes": True}


class CommerceSessionResponse(BaseModel):
    id: UUID
    agent_id: UUID | None
    channel: str
    status: CommerceStatus
    currency: str
    cart_snapshot: dict
    browser_checkpoint: dict
    payment_method: PaymentMethod | None
    confirmed_at: datetime | None
    order_reference: str | None
    checkout_url: str | None
    last_error: str | None
    expires_at: datetime
    created_at: datetime
    updated_at: datetime
    actions: list[CommerceActionResponse] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class CommerceProviderStatus(BaseModel):
    provider: Literal["fepy_browser"] = "fepy_browser"
    enabled: bool
    order_submission_enabled: bool
    shop_origin: str
    execution_mode: Literal["local_chromium", "disabled"]
