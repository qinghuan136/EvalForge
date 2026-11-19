"""Display-only projections shared by saved history and live tool events."""

from pydantic import JsonValue, TypeAdapter
from pydantic_ai import DeferredToolRequests
from pydantic_ai.conversation import Conversation, ConversationTypeAdapter
from pydantic_ai.messages import RetryPromptPart, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart

from ..schemas.assistant import AssistantConversationView

_PART_FIELDS = {"part_kind", "content", "args", "tool_name", "tool_call_id", "timestamp", "outcome"}
_MESSAGE_FIELDS = {"kind", "timestamp", "run_id", "conversation_id", "model_name", "provider_name", "usage", "finish_reason"}
_METADATA_FIELDS = {"text", "page_context", "source_experiment_id"}
_VISIBLE_PARTS = (UserPromptPart, TextPart, ToolCallPart, ToolReturnPart, RetryPromptPart)
_CALL = TypeAdapter(ToolCallPart)
_RETURN = TypeAdapter(ToolReturnPart)
_DEFERRED = TypeAdapter(DeferredToolRequests)


def display_tool_part(part: ToolCallPart | ToolReturnPart) -> dict[str, JsonValue]:
    adapter = _CALL if isinstance(part, ToolCallPart) else _RETURN
    return adapter.dump_python(part, mode="json", include=_PART_FIELDS)


def display_deferred(requests: DeferredToolRequests) -> dict[str, JsonValue]:
    return _DEFERRED.dump_python(requests, mode="json", include={
        "calls": {"__all__": _PART_FIELDS}, "approvals": {"__all__": _PART_FIELDS},
    })


def display_conversation(conversation: Conversation) -> AssistantConversationView:
    # Include before serializing: hidden provider metadata may not even be JSON serializable.
    messages = {
        index: {**dict.fromkeys(_MESSAGE_FIELDS, True),
                "metadata": {"evalforge": _METADATA_FIELDS},
                "parts": {i: _PART_FIELDS for i, part in enumerate(message.parts)
                          if isinstance(part, _VISIBLE_PARTS)}}
        for index, message in enumerate(conversation.messages)
    }
    data = ConversationTypeAdapter.dump_python(conversation, mode="json", include={
        "conversation_id": True, "revision": True, "usage": True, "messages": messages,
    })
    data["messages"] = [message for message in data["messages"] if message["parts"]]
    data["deferred_tool_requests"] = (display_deferred(conversation.deferred_tool_requests)
                                         if conversation.deferred_tool_requests else None)
    return AssistantConversationView.model_validate(data)
