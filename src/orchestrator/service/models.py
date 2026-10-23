# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Small request models and service-only constants for the HTTP API.

Response bodies are plain, already-normalized dicts built from the core
dataclasses' own ``to_dict()`` methods (see ``api.py``) rather than a second,
parallel schema -- there is only one source of truth for response shape.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

class SettingsUpdateRequest(BaseModel):
    """The one setting a release client changes: the background job runner."""

    model_config = ConfigDict(extra="forbid")

    job_runner_enabled: bool | None = Field(default=None, strict=True)

    @field_validator('job_runner_enabled', mode='before')
    @classmethod
    def require_job_runner_boolean(cls, value: object) -> bool:
        if type(value) is not bool:
            raise ValueError('job_runner_enabled must be a boolean')
        return value
