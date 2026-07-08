from pydantic import BaseModel, ConfigDict, Field


class MergeUsersRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    source_user_id: int = Field(gt=0)
    target_user_id: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=1024)


class MergeUsersTargetResponse(BaseModel):
    id: int
    email: str | None
    telegram_id: int | None
    is_email_verified: bool
    current_subscription_id: int | None


class MergeUsersResponse(BaseModel):
    dry_run: bool
    source_user_id: int
    target_user_id: int
    target: MergeUsersTargetResponse
    moved: dict[str, int]
    conflicts: list[str]
    requires_relogin: bool
