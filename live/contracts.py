"""Narrow server-enforced capabilities shared by humans and the agent."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateMeeting(Contract):
    project_id: str
    artifact_id: str
    title: str = Field(min_length=1, max_length=200)
    responsibility: str = Field(min_length=1, max_length=1500)
    data_scope: Literal["synthetic", "real"] = "real"


class Join(Contract):
    consent: bool


class Responsibility(Contract):
    text: str = Field(min_length=1, max_length=1500)
    expected_generation: int = Field(ge=1)


class Control(Contract):
    action: Literal["pause", "resume", "mute", "unmute", "end", "interrupt"]


class Operation(Contract):
    op_id: str = Field(min_length=1, max_length=100, pattern=r"^[\w-]+$")
    kind: Literal["set_draft_field", "undo", "confirm_decision"] = "set_draft_field"
    row_id: str = Field(min_length=1, max_length=80)
    field: Literal["name", "constraint", "evidence", "pending", "decision", "rationale"]
    expected_field_version: int = Field(ge=0)
    value: str = Field(default="", max_length=8000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)
    meeting_id: str | None = None
    undo_op_id: str | None = None


class Event(Contract):
    source_event_id: str = Field(min_length=1, max_length=180)
    type: Literal["transcript.final", "transcript.interim", "participant.joined", "participant.left", "media.status"]
    speaker_id: str | None = None
    segment_id: str | None = Field(default=None, max_length=120)
    revision: int = Field(default=1, ge=1)
    text: str = Field(default="", max_length=6000)
    occurred_at: float | None = None
    generation: int | None = None


class TaskRequest(Contract):
    task_id: str = Field(min_length=1, max_length=100)
    row_id: str
    query: str = Field(min_length=1, max_length=600)
    evidence_refs: list[str] = Field(min_length=1, max_length=20)


class AgentAction(Contract):
    kind: Literal["set_draft_field", "research", "speak"]
    row_id: str | None = None
    field: Literal["name", "constraint", "evidence", "pending", "decision", "rationale"] | None = None
    value: str = Field(default="", max_length=8000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)


class AgentPlan(Contract):
    actions: list[AgentAction] = Field(default_factory=list, max_length=4)

    @field_validator("actions")
    @classmethod
    def narrow_actions(cls, actions):
        for action in actions:
            if action.kind != "speak" and (not action.row_id or not action.evidence_refs):
                raise ValueError("修改和查证必须指定稳定行 ID 与证据")
            if action.kind == "set_draft_field" and action.field is None:
                raise ValueError("缺少目标字段")
            if action.kind == "speak" and len(action.value) > 350:
                raise ValueError("播报过长")
        return actions
