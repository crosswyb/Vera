"""Small v2 contracts. No table rows, tool execution, or permission actions."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MeetingPermissions(Contract):
    model: bool = False
    search: bool = False
    auto_search: bool = False


class NewMeeting(Contract):
    title: str = Field(min_length=1, max_length=120)
    data_scope: Literal["synthetic", "real"] = "real"
    input_kind: Literal["live", "audio_file"] = "live"
    permissions: MeetingPermissions | None = None


class RenameMeeting(Contract):
    title: str = Field(min_length=1, max_length=120)


class DeleteMeeting(Contract):
    confirmation: str = Field(min_length=1, max_length=120)


class SpeechAuthorization(Contract):
    revoke: bool = False
    provider: Literal["dashscope", "deepseek"] | None = None
    kind: Literal["live", "file"] | None = None
    sources: list[Literal["system", "microphone", "file"]] = Field(default_factory=list, max_length=2)
    revision: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def scope(self):
        if self.revoke:
            if self.model_fields_set != {"revoke"}:
                raise ValueError("revocation takes no scope")
        else:
            allowed = {"file"} if self.kind == "file" else {"system", "microphone"}
            if (self.kind is None or self.provider is None or self.revision is None or not self.sources
                    or not set(self.sources) <= allowed or len(set(self.sources)) != len(self.sources)):
                raise ValueError("invalid speech scope")
        return self


class SpeechSessionRequest(Contract):
    kind: Literal["live", "file"]
    task_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_-]+$")
    sources: list[Literal["system", "microphone", "file"]] = Field(min_length=1, max_length=2)


class CaptureFinish(Contract):
    capture_epoch: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_-]+$")
    saved: bool
    no_audio: bool = False

    @model_validator(mode="after")
    def empty_capture_is_sealed(self):
        if self.no_audio and not self.saved:
            raise ValueError("an empty capture must be successfully sealed")
        return self


class AudioImport(Contract):
    import_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_-]+$")
    file_name: str = Field(min_length=1, max_length=255)
    duration_ms: int = Field(gt=0, le=21600000)
    bytes: int = Field(gt=0, le=2000000000)
    provider: Literal["apple_local", "dashscope", "deepseek"] = "apple_local"
    settings_revision: int | None = Field(default=None, ge=0)


class AudioImportFinish(Contract):
    import_id: str = Field(min_length=1, max_length=120)
    state: Literal["completed", "cancelled", "failed"]
    error: str = Field(default="", max_length=300)


class AudioImportResume(Contract):
    import_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_-]+$")


class RecordingStart(Contract):
    recording_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_-]+$")
    sources: list[Literal["system", "microphone"]] = Field(min_length=1, max_length=2)
    provider: Literal["apple_local", "dashscope", "deepseek"] = "apple_local"
    settings_revision: int | None = Field(default=None, ge=0)


class SourceRef(Contract):
    segment_id: str = Field(min_length=1, max_length=240)
    revision: int = Field(ge=1)


class Utterance(Contract):
    text: str = Field(min_length=1, max_length=5000)
    segment_id: str = Field(default="", max_length=120)
    revision: int = Field(default=1, ge=1)
    event_id: str = Field(default="", max_length=120)


class NewQuestion(Contract):
    title: str = Field(min_length=1, max_length=300)
    source_refs: list[SourceRef] = Field(default_factory=list, max_length=12)
    public_query: str = Field(default="", max_length=128)
    web_search_requested: bool = False


class EditQuestion(Contract):
    expected_version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=300)


class QuestionAction(Contract):
    expected_version: int = Field(ge=1)
    action: Literal["cancel", "retry", "prioritize", "approve"]
    public_query: str = Field(default="", max_length=128)
    web_search_requested: bool = False


class MeetingControl(Contract):
    action: Literal["pause_research", "resume_research", "retry_coordinator", "end"]


class AudioEvent(Contract):
    protocol_version: Literal[1] = 1
    meeting_id: str = Field(min_length=1, max_length=120)
    capture_epoch: str = Field(min_length=1, max_length=120)
    stream_id: str = Field(min_length=1, max_length=120)
    seq: int = Field(ge=1)
    type: Literal["transcript.interim", "transcript.final", "transcript.retracted",
                  "audio.ready", "audio.paused", "audio.stopped", "audio.gap",
                  "audio.error", "audio.level", "audio.status", "audio.recording", "audio.model_status", "model.status", "import.progress",
                  "asr.status", "asr.error", "asr.gap"]
    source: Literal["system", "microphone", "file"]
    provider: Literal["apple_local", "dashscope", "deepseek"] | None = None
    occurred_at: str = Field(min_length=1, max_length=64)
    segment_id: str | None = Field(default=None, max_length=120)
    revision: int | None = Field(default=None, ge=1)
    supersedes_revision: int | None = Field(default=None, ge=1)
    audio_start_ms: int | None = Field(default=None, ge=0)
    audio_end_ms: int | None = Field(default=None, ge=0)
    text: str | None = Field(default=None, max_length=5000)
    level: float | None = Field(default=None, ge=0, le=1)
    status: str | None = Field(default=None, max_length=100)
    code: str | None = Field(default=None, max_length=100)
    message: str | None = Field(default=None, max_length=500)
    state: str | None = Field(default=None, max_length=100)
    detail: str | None = Field(default=None, max_length=500)
    rms: float | None = Field(default=None, ge=0)
    frames_received: int | None = Field(default=None, ge=0)
    non_silent_frames: int | None = Field(default=None, ge=0)
    sample_rate: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def transcript(self):
        if self.type.startswith("transcript."):
            if not self.segment_id or self.revision is None or self.text is None:
                raise ValueError("transcript requires segment, revision and text")
            if self.type != "transcript.retracted" and not self.text.strip():
                raise ValueError("empty transcript must be retracted")
            if self.audio_start_ms is None or self.audio_end_ms is None or self.audio_end_ms < self.audio_start_ms:
                raise ValueError("invalid audio interval")
        if self.type == "asr.gap" and (self.audio_start_ms is None or self.audio_end_ms is None
                                       or self.audio_end_ms < self.audio_start_ms):
            raise ValueError("asr.gap requires an audio interval")
        return self


class OriginalTarget(Contract):
    kind: Literal["standard", "policy", "definition"]
    identifier: str = Field(default="", max_length=160)
    section: str = Field(default="", max_length=40)
    issuer: str = Field(default="", max_length=80)


class QuestionDecision(Contract):
    kind: Literal["create_question", "candidate", "revise_question", "cancel_candidate", "no_action"]
    title: str = Field(default="", max_length=300)
    question_id: str = Field(default="", max_length=120)
    expected_version: int = Field(default=1, ge=1)
    source_refs: list[SourceRef] = Field(default_factory=list, max_length=12)
    public_query: str = Field(default="", max_length=128)
    scope_changed: bool = True
    evidence_need: Literal['public', 'internal', 'mixed', 'explanation'] | None = None
    grounding_quote: str = Field(default="", max_length=500)
    grounding_quote_id: str = Field(default="", max_length=120)
    external_fact: str = Field(default="", max_length=300)
    original_target: OriginalTarget | None = None


class Decisions(Contract):
    actions: list[QuestionDecision] = Field(max_length=12)


class SummaryPoint(Contract):
    text: str = Field(min_length=1, max_length=1200)
    source_refs: list[SourceRef] = Field(min_length=1, max_length=12)


class MeetingSummary(Contract):
    overview: str = Field(min_length=1, max_length=2000)
    topics: list[SummaryPoint] = Field(min_length=1, max_length=16)
    decisions: list[SummaryPoint] = Field(default_factory=list, max_length=16)
    actions: list[SummaryPoint] = Field(default_factory=list, max_length=20)
    open_questions: list[SummaryPoint] = Field(default_factory=list, max_length=16)


class Claim(Contract):
    text: str = Field(min_length=1, max_length=1000)
    source_id: str = Field(min_length=1, max_length=100)
    quote: str = Field(min_length=8, max_length=500)


class AnswerPoint(Contract):
    text: str = Field(min_length=1, max_length=140)
    kind: Literal["fact", "source_claim", "inference", "explanation"]
    claim_indices: list[Annotated[int, Field(ge=1, le=12)]] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def evidence_links(self):
        if any(index < 1 or index > 12 for index in self.claim_indices) or len(set(self.claim_indices)) != len(self.claim_indices):
            raise ValueError("claim indices must be unique, one-based evidence references")
        if self.kind in {"fact", "source_claim"} and not self.claim_indices:
            raise ValueError("factual key points require evidence references")
        return self


class AnswerBrief(Contract):
    short_answer: str = Field(min_length=1)
    key_points: list[AnswerPoint] = Field(min_length=1, max_length=3)
    caveat: str = Field(min_length=1, max_length=180)


class SearchDecision(Contract):
    route: Literal["direct", "web_search"]
    short_answer: str = ""
    answer: str = Field(default="", max_length=1800)

    @model_validator(mode="after")
    def complete_direct_answer(self):
        if self.route == "direct" and not (self.short_answer.strip() and self.answer.strip()):
            raise ValueError("direct requires short_answer and answer")
        return self


class ResearchAnswer(Contract):
    answer: str = Field(min_length=1, max_length=6000)
    claims: list[Claim] = Field(default_factory=list, max_length=12)
    uncertainty: list[str] = Field(default_factory=list, max_length=12)
    evidence_status: Literal["supported", "mixed", "insufficient", "model_explanation"]
    # Older saved answers remain readable without a generated brief.
    brief: AnswerBrief | None = None


class CitationChoice(Contract):
    text: str = Field(min_length=1, max_length=1000)
    source_id: str = Field(min_length=1, max_length=100)
    quote_id: str = Field(default="", max_length=120)
    # Accept legacy literal quotes, but resolve selections before publication.
    quote: str = Field(default="", max_length=500)


class ResearchDraft(ResearchAnswer):
    claims: list[CitationChoice] = Field(default_factory=list, max_length=12)
    brief: AnswerBrief
