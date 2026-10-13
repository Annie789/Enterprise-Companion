from typing import List, Optional

from pydantic import BaseModel, Field


class ConfluenceConnectionRequest(BaseModel):
    user_id: str
    base_url: str
    api_token: str
    space_keys: List[str] = Field(default_factory=list)
    email: Optional[str] = None


class JiraConnectionRequest(BaseModel):
    user_id: str
    server_url: str
    email: str
    api_token: str
    project_keys: List[str] = Field(default_factory=list)


class SlackConnectionRequest(BaseModel):
    user_id: str
    bot_token: str
    channel_ids: List[str] = Field(default_factory=list)


class GoogleDriveConnectionRequest(BaseModel):
    user_id: str
    folder_ids: List[str] = Field(default_factory=list)


class ConnectionResponse(BaseModel):
    connection_id: str
    user_id: str
    source: str
    status: str