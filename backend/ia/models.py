# -*- coding: utf-8 -*-
"""IA-01 / reporting Pydantic models (camelCase JSON).

tenantId: long, optional during smoke (target-state required).
subjectKey: optional task keyword; AUTHORIZATION_REMINDER omits it.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CamelModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


DispatchType = Literal["INITIAL", "PROMISE_FOLLOW_UP", "CALLBACK_APPOINTMENT"]
IdentityMode = Literal["CLIENT_IDENTITY", "BRAND_SELF"]
Scenario = Literal["AUTHORIZATION_REMINDER"]


class Recipient(CamelModel):
    name: str
    mobile: str
    email: str | None = None


class Identity(CamelModel):
    mode: IdentityMode
    customerBrandName: str | None = None
    ourBrandName: str | None = None
    clientBuName: str | None = None


class ContactSlots(CamelModel):
    callbackNumber: str
    workingHours: str | None = "09:00-18:00"
    workingHoursTimezone: str | None = None


class ChannelSlots(CamelModel):
    emailEnabled: bool = False
    fallbackSmsEnabled: bool = False
    initialSmsSentAt: str | None = None
    fallbackSmsSentAt: str | None = None
    smsTextPrefix: str | None = None
    emailSubject: str | None = None
    fallbackSmsContent: str | None = None
    publicAccountName: str | None = None
    publicAccountOwner: str | None = None
    resendChannel: str | None = None


class OrderSlots(CamelModel):
    orderType: Literal["ONLINE", "OFFLINE"]
    authorizationStatus: Literal["NOT_COMPLETED", "COMPLETED"]
    statusUpdatedAt: str
    deadline: str | None = None
    authorizationMode: str | None = None


class Slots(CamelModel):
    contact: ContactSlots
    channels: ChannelSlots = Field(default_factory=ChannelSlots)
    order: OrderSlots
    workOrderNo: str | None = None
    # Reserved for future scenarios (IA slots.scenarioPayload.*)
    scenarioPayload: dict[str, Any] | None = None


class Context(CamelModel):
    historyLinked: bool = False
    previousTaskId: int | None = None
    lastSpokenSummary: str | None = None
    remainingItems: list[Any] | None = None
    recipientPromisedTime: str | None = None
    lastTriggerFamily: str | None = None
    clientNoteFiltered: str | None = None


class RetryPolicy(CamelModel):
    maxAttempts: int = 2
    retryIntervalMinutes: int = 30


class TaskDispatchRequest(CamelModel):
    """IA-01 POST /va-algo/v1/task-dispatch body."""

    dispatchId: str
    taskRevision: int
    dispatchType: DispatchType
    taskId: int
    externalOrderId: str
    scenario: Scenario = "AUTHORIZATION_REMINDER"
    scriptId: str
    scriptVersion: str
    recipient: Recipient
    identity: Identity
    slots: Slots
    context: Context = Field(default_factory=Context)
    callWindow: str = "09:00-21:00"
    timezone: str | None = "Asia/Shanghai"
    requestedAt: str | None = None
    deadline: str | None = None  # smoke: ignore
    retryPolicy: RetryPolicy = Field(default_factory=RetryPolicy)
    # Smoke: optional (omit OK). Target-state: required before 2nd tenant.
    tenantId: int | None = None
    # Optional task keyword; AUTHORIZATION_REMINDER typically omits.
    subjectKey: str | None = None
    contractVersion: str | None = None


class TaskDispatchResponse(CamelModel):
    accepted: bool
    duplicate: bool
    plannedCallAt: str
    taskRevision: int


class TaskDispatchError(CamelModel):
    accepted: bool = False
    errorCode: str
    message: str


# --- Platform reporting payloads (algorithm → platform) ---


class CallEventRequest(CamelModel):
    """IA-03A POST /report/voice/callEvents."""

    taskId: int
    callId: int
    taskRevision: int
    executionSequence: int
    eventType: Literal["CALL_STARTED", "CALL_FINISHED", "CALL_SKIPPED", "TERMINATION_COMPLETED"]
    attemptNo: int | None = None
    attemptCounted: bool | None = None
    callOutcome: str | None = None
    hangupInitiator: str | None = None
    dialFailureReason: str | None = None
    startedAt: str | None = None
    endedAt: str | None = None
    durationSeconds: int | None = None
    skipReason: str | None = None
    scriptVersion: str | None = None
    taskStatus: str | None = None
    closeReason: str | None = None
    failureEvidence: dict[str, Any] | None = None
    # Reserved / pass-through
    resultBundleId: str | None = None
    tenantId: int | None = None


class CallOutcomeRequest(CamelModel):
    """IA-03B POST /report/voice/callOutcomes."""

    taskId: int
    callId: int
    scenario: Scenario = "AUTHORIZATION_REMINDER"
    scenarioSchemaVersion: str = "1.0"
    compliance: dict[str, Any]
    recipientPromisedTime: str | None = None
    reschedule: dict[str, Any]
    humanNeeded: bool = False
    recipientWithdrawn: bool = False
    feedbackItems: list[dict[str, Any]] = Field(default_factory=list)
    scenarioPayload: dict[str, Any] = Field(default_factory=dict)
    # Reserved / pass-through
    resultBundleId: str | None = None
    tenantId: int | None = None
    outcomeId: str | None = None


class ArtifactUploadMeta(CamelModel):
    """IA-05 (merged) multipart meta for POST /report/voice/recording."""

    taskId: int
    callId: int
    artifactType: Literal["RECORDING", "TRANSCRIPT"]
    # Reserved / pass-through
    tenantId: int | None = None
    uploadRequestId: str | None = None
