"""Display projections must not expose or mutate the execution checkpoint."""

import json
import unittest

from pydantic_ai import DeferredToolRequests
from pydantic_ai.conversation import Conversation
from pydantic_ai.messages import (ModelRequest, ModelResponse, SystemPromptPart, TextPart,
                                 ThinkingPart, ToolCallPart, ToolReturnPart, UserPromptPart)

from evalforge.server.assistant.presentation import display_conversation, display_deferred, display_tool_part


class AssistantPresentationTests(unittest.TestCase):
    def test_display_selects_fields_without_rewriting_content(self):
        call = ToolCallPart("inspect", json.dumps({"input": "hello", "headers": {"Authorization": "private-auth"},
                                                  "api_key": "private-key"}), tool_call_id="call-1")
        result = ToolReturnPart("inspect", {"ok": True, "nested": [{"credential": "private-credential"}]},
                                tool_call_id="call-1", metadata={"private": "tool-metadata"})
        conversation = Conversation(conversation_id="conversation-1", revision="revision-1", new_message_index=3,
            messages=[
                ModelRequest(parts=[SystemPromptPart("private-system"), UserPromptPart("injected context and text")],
                             instructions="private-instructions", run_id="run-1", metadata={
                                 "private": "private-message-metadata", "evalforge": {"text": "问题", "private": "private-business-metadata",
                                 "page_context": {"type": "experiment", "id": 7}}}),
                ModelResponse(parts=[ThinkingPart("private-thinking"), call], run_id="run-1",
                              provider_details={"private": "private-provider"}),
                ModelRequest(parts=[result], run_id="run-1"),
                ModelResponse(parts=[TextPart("完成")], model_name="test-model", run_id="run-1"),
            ], deferred_tool_requests=DeferredToolRequests(approvals=[call], metadata={"call-1": {"secret": "private-deferred"}}))

        view = display_conversation(conversation).model_dump(mode="json")
        encoded = json.dumps(view, ensure_ascii=False)
        for hidden in ("private-system", "private-instructions", "private-message-metadata",
                       "private-business-metadata", "private-thinking", "private-provider", "private-deferred", "tool-metadata"):
            self.assertNotIn(hidden, encoded)
        self.assertNotIn("new_message_index", view)
        self.assertNotIn("instructions", view["messages"][0])
        self.assertNotIn("provider_details", view["messages"][1])
        self.assertEqual(view["messages"][0]["parts"][0]["content"], "injected context and text")
        self.assertEqual(view["messages"][0]["metadata"]["evalforge"]["page_context"], {"type": "experiment", "id": 7})
        self.assertEqual(view["messages"][1]["parts"][0], display_tool_part(call))
        self.assertEqual(view["messages"][2]["parts"][0], display_tool_part(result))
        self.assertEqual(view["messages"][1]["parts"][0]["args"], call.args)
        self.assertEqual(view["messages"][2]["parts"][0]["content"], result.content)
        self.assertEqual(view["deferred_tool_requests"], display_deferred(conversation.deferred_tool_requests))
        self.assertEqual(view["messages"][-1]["run_id"], "run-1")
        self.assertEqual(view["messages"][-1]["model_name"], "test-model")
        self.assertIn("private-auth", call.args)
        self.assertEqual(result.content["nested"][0]["credential"], "private-credential")
        self.assertEqual(conversation.messages[0].parts[1].content, "injected context and text")

    def test_unknown_parts_and_nonserializable_internal_metadata_are_not_exported(self):
        conversation = Conversation(messages=[
            ModelRequest(parts=[SystemPromptPart("hidden")], metadata={"private": object()}),
            ModelResponse(parts=[ThinkingPart("hidden")], provider_details={"private": object()}),
            ModelResponse(parts=[TextPart("visible")], metadata={"private": object()}),
        ])
        view = display_conversation(conversation)
        self.assertEqual(len(view.messages), 1)
        self.assertEqual(view.messages[0]["parts"][0]["content"], "visible")

    def test_strings_and_business_fields_are_preserved_verbatim(self):
        content = '{"input":{"token":"efr.identifier.secret","password":"example"}}\n'
        part = ToolCallPart("inspect", content, tool_call_id="call-1")
        self.assertEqual(display_tool_part(part)["args"], content)
        result = ToolReturnPart("inspect", {"headers": {"Authorization": "Bearer example"}}, tool_call_id="call-1")
        self.assertEqual(display_tool_part(result)["content"], result.content)

    def test_empty_conversation_has_no_pending_requests(self):
        view = display_conversation(Conversation(conversation_id="empty", revision="r"))
        self.assertEqual(view.messages, [])
        self.assertIsNone(view.deferred_tool_requests)
        self.assertEqual(view.revision, "r")
