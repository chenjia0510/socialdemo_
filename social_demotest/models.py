from enum import Enum
from pydantic import BaseModel

class ChatType(str, Enum):
    big_five = "big_five"
    deep_profile = "deep_profile"
    direct = "direct"

class ChatRequest(BaseModel):
    user_id: str
    message: str
    state: str # "big_five" or "deep_profile"
    initial_interest: str = None

class MatchRequest(BaseModel):
    user_id: str
    source: str = "manual"
    force_new: bool = False

class ClearRequest(BaseModel):
    user_id: str

class AcceptRequest(BaseModel):
    user_id: str
    match_id: str
    explicit_reasons: list[str] = []

class DirectChatRequest(BaseModel):
    user_id: str
    contact_id: str
    message: str
    chat_type: str = "direct"  # "direct", "deep_profile"

class MediatorPrivateRequest(BaseModel):
    user_id: str
    other_id: str
    message: str

class SettingsRequest(BaseModel):
    user_id: str
    proactive_frequency: str = "normal"  # "low", "normal", "high"

class MediatorToneRequest(BaseModel):
    user_id: str
    mediator_tone: str = "friend"  # "friend", "gentle", "enthusiastic"
    probe_mode: str | None = None

class MediatorProbeRequest(BaseModel):
    user_id: str
    other_id: str
    force: bool = False

class ProfileMemoryActionRequest(BaseModel):
    user_id: str
    key: str
    action: str
    value: str | None = None

class ResetRequest(BaseModel):
    user_id: str
    state: str  # "big_five" or "deep_profile"
