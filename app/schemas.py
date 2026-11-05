"""Pydantic request schemas for validated external API inputs."""

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, EmailStr, Field


class HelpdeskWebhook(BaseModel):
    """Validated shape accepted from helpdesk event deliveries."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=255)
    ticket_id: str = Field(min_length=1, max_length=255)
    customer_email: EmailStr
    message: str = Field(min_length=1, max_length=10_000)
    reply_url: AnyHttpUrl
