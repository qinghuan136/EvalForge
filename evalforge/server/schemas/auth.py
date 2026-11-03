"""对外账号模型；SecretStr 避免密码意外出现在对象 repr 中。"""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

NewPassword = Annotated[SecretStr, Field(min_length=15, max_length=128)]
ExistingPassword = Annotated[SecretStr, Field(min_length=1, max_length=128)]


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class LoginRequest(Schema):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    password: ExistingPassword

    @field_validator("username", mode="before")
    @classmethod
    def normalize_username(cls, value):
        return value.strip().lower() if isinstance(value, str) else value


class RegisterRequest(LoginRequest):
    password: NewPassword
    display_name: str | None = Field(default=None, max_length=100)

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value):
        if value is not None:
            value = value.strip()
            if not value:
                raise ValueError("display_name must not be blank")
        return value


class PasswordRequest(Schema):
    current_password: ExistingPassword
    new_password: NewPassword


class UserResponse(Schema):
    id: int
    username: str
    display_name: str | None


class RegisteredResponse(Schema):
    user: UserResponse


class SessionResponse(RegisteredResponse):
    session_expires_at: datetime


class LoginResponse(SessionResponse):
    csrf_token: str


class CSRFResponse(Schema):
    csrf_token: str
