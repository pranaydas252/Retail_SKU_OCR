"""API request models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ConfirmScanRequest(BaseModel):
    """Operator-confirmed field values.

    Values are sent as the operator left them — edited or accepted unchanged.
    The server re-validates before persisting rather than trusting the client
    (CLAUDE.md section 13); the device is a capture terminal, not an authority
    on what is valid.

    A field may be explicitly null when the operator confirms it is genuinely
    absent from the label. That is meaningful information, not a missing value.
    """

    fields: dict[str, str | None] = Field(
        default_factory=dict,
        description="Field name to confirmed value, e.g. {'batchNumber': 'A23C91'}",
    )
    device_id: str | None = Field(default=None, alias="deviceId")

    model_config = {"populate_by_name": True}
