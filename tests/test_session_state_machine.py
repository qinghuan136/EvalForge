"""会话阶段的允许转换、条件、幂等及终态边界，不依赖网络或存储。"""

import unittest
from dataclasses import FrozenInstanceError, replace

from evalforge.server.session_state_machine import (
    SESSION_TRANSITIONS, SessionContext, SessionEvent, SessionState, TransitionRejected,
    next_session_state, preview_session_transition, validate_session_event,
)


class SessionStateMachineTests(unittest.TestCase):
    def setUp(self):
        self.context = SessionContext(is_current=True, hello_validated=True, sync_processed=True)

    def transition(self, state, event, **changes):
        return next_session_state(state, event, replace(self.context, **changes))

    def assert_rejected(self, state, event, reason, **changes):
        with self.assertRaises(TransitionRejected) as caught:
            self.transition(state, event, **changes)
        self.assertEqual(caught.exception.current, state)
        self.assertEqual(caught.exception.event, event)
        self.assertEqual(caught.exception.reason, reason)

    def test_full_lifecycle(self):
        state = self.transition(SessionState.CONNECTED, SessionEvent.HELLO_CONFIRMED)
        self.assertEqual(state, SessionState.SYNCING)
        state = self.transition(state, SessionEvent.READY_REQUESTED)
        self.assertEqual(state, SessionState.READY)
        self.assertEqual(self.transition(state, SessionEvent.CLOSE), SessionState.CLOSED)

    def test_hello_requires_validated_input_and_current_registration(self):
        for changes in ({"hello_validated": False}, {"is_current": False}):
            with self.subTest(changes=changes):
                self.assert_rejected(SessionState.CONNECTED, SessionEvent.HELLO_CONFIRMED,
                                     "guard_rejected", **changes)

    def test_ready_requires_current_session_completed_sync_and_no_pending_uploads(self):
        for changes in ({"is_current": False}, {"sync_processed": False}, {"pending_uploads_count": 1}):
            with self.subTest(changes=changes):
                self.assert_rejected(SessionState.SYNCING, SessionEvent.READY_REQUESTED,
                                     "guard_rejected", **changes)

    def test_sync_snapshot_and_pending_context_can_change_without_changing_phase(self):
        state = SessionState.SYNCING
        snapshot = SessionContext(is_current=True, hello_validated=True,
                                  sync_processed=True, pending_uploads_count=2)
        with self.assertRaises(TransitionRejected):
            next_session_state(state, SessionEvent.READY_REQUESTED, snapshot)
        committed = replace(snapshot, pending_uploads_count=0)
        self.assertEqual(next_session_state(state, SessionEvent.READY_REQUESTED, committed), SessionState.READY)
        self.assertEqual(snapshot.pending_uploads_count, 2)
        self.assertEqual(state, SessionState.SYNCING)

    def test_ready_retry_is_idempotent_but_still_requires_current_ownership(self):
        # 已 ready 的同一会话重试不重新开启一次 sync。
        state = self.transition(SessionState.READY, SessionEvent.READY_REQUESTED, sync_processed=False)
        self.assertEqual(state, SessionState.READY)
        self.assert_rejected(state, SessionEvent.READY_REQUESTED, "guard_rejected", is_current=False)

    def test_renewal_preserves_phase_and_requires_current_registration(self):
        for state in (SessionState.SYNCING, SessionState.READY):
            with self.subTest(state=state):
                self.assertEqual(self.transition(state, SessionEvent.LEASE_RENEWED), state)
                self.assert_rejected(state, SessionEvent.LEASE_RENEWED, "guard_rejected", is_current=False)

    def test_close_is_allowed_without_lease_from_every_state_and_is_idempotent(self):
        for state in SessionState:
            with self.subTest(state=state):
                self.assertEqual(next_session_state(state, SessionEvent.CLOSE, SessionContext()), SessionState.CLOSED)

    def test_all_unlisted_transitions_are_rejected(self):
        allowed = {
            (SessionState.CONNECTED, SessionEvent.HELLO_CONFIRMED),
            (SessionState.SYNCING, SessionEvent.READY_REQUESTED),
            (SessionState.READY, SessionEvent.READY_REQUESTED),
            (SessionState.SYNCING, SessionEvent.LEASE_RENEWED),
            (SessionState.READY, SessionEvent.LEASE_RENEWED),
            *((state, SessionEvent.CLOSE) for state in SessionState),
        }
        for state in SessionState:
            for event in SessionEvent:
                if (state, event) not in allowed:
                    with self.subTest(state=state, event=event):
                        self.assert_rejected(state, event, "unsupported_transition")

    def test_closed_cannot_reopen_even_if_previous_context_is_valid(self):
        for event in SessionEvent:
            if event != SessionEvent.CLOSE:
                with self.subTest(event=event):
                    self.assert_rejected(SessionState.CLOSED, event, "unsupported_transition")

    def test_rejected_transition_leaves_inputs_unchanged(self):
        current = SessionState.SYNCING
        context = SessionContext(is_current=True, sync_processed=True, pending_uploads_count=3)
        with self.assertRaises(TransitionRejected):
            next_session_state(current, SessionEvent.READY_REQUESTED, context)
        self.assertEqual(current, SessionState.SYNCING)
        self.assertEqual(context.pending_uploads_count, 3)

    def test_transition_table_and_context_are_read_only(self):
        with self.assertRaises(TypeError):
            SESSION_TRANSITIONS[(SessionState.CLOSED, SessionEvent.HELLO_CONFIRMED)] = (lambda ctx: True, SessionState.READY)
        with self.assertRaises(FrozenInstanceError):
            self.context.is_current = False

    def test_context_rejects_truthy_strings_bool_counts_and_negative_counts(self):
        for changes in ({"is_current": "false"}, {"hello_validated": 1}, {"sync_processed": None},
                        {"pending_uploads_count": True}, {"pending_uploads_count": 1.5}):
            with self.subTest(changes=changes), self.assertRaises(TypeError):
                SessionContext(**changes)
        with self.assertRaises(ValueError):
            SessionContext(pending_uploads_count=-1)

    def test_transition_boundary_requires_explicit_state_event_and_context_types(self):
        for args in (("syncing", SessionEvent.READY_REQUESTED, self.context),
                     (SessionState.SYNCING, "ready_requested", self.context),
                     (SessionState.SYNCING, SessionEvent.READY_REQUESTED, {})):
            with self.subTest(args=args), self.assertRaises(TypeError):
                next_session_state(*args)

    def test_preview_returns_target_without_changing_current_state_or_context(self):
        current = SessionState.SYNCING
        context = self.context
        preview = preview_session_transition(current, SessionEvent.READY_REQUESTED, context)
        self.assertTrue(preview.allowed)
        self.assertEqual(preview.current, current)
        self.assertEqual(preview.event, SessionEvent.READY_REQUESTED)
        self.assertEqual(preview.next_state, SessionState.READY)
        self.assertIsNone(preview.reason)
        self.assertEqual(current, SessionState.SYNCING)
        self.assertIs(context, self.context)
        with self.assertRaises(FrozenInstanceError):
            preview.next_state = SessionState.CLOSED

    def test_preview_rejected_guard_returns_no_next_state(self):
        for changes in ({"is_current": False}, {"sync_processed": False}, {"pending_uploads_count": 1}):
            with self.subTest(changes=changes):
                preview = preview_session_transition(
                    SessionState.SYNCING, SessionEvent.READY_REQUESTED, replace(self.context, **changes))
                self.assertFalse(preview.allowed)
                self.assertIsNone(preview.next_state)
                self.assertEqual(preview.reason, "guard_rejected")

    def test_preview_unlisted_event_returns_rejection_instead_of_raising(self):
        preview = preview_session_transition(SessionState.CLOSED, SessionEvent.READY_REQUESTED, self.context)
        self.assertFalse(preview.allowed)
        self.assertIsNone(preview.next_state)
        self.assertEqual(preview.reason, "unsupported_transition")

    def test_event_precheck_does_not_bypass_post_operation_guard(self):
        validate_session_event(SessionState.CONNECTED, SessionEvent.HELLO_CONFIRMED)
        preview = preview_session_transition(
            SessionState.CONNECTED, SessionEvent.HELLO_CONFIRMED, SessionContext(hello_validated=True))
        self.assertFalse(preview.allowed)
        self.assertEqual(preview.reason, "guard_rejected")
        confirmed = preview_session_transition(SessionState.CONNECTED, SessionEvent.HELLO_CONFIRMED, self.context)
        self.assertEqual(confirmed.next_state, SessionState.SYNCING)

    def test_event_precheck_rejects_duplicate_hello_before_registration(self):
        with self.assertRaises(TransitionRejected) as caught:
            validate_session_event(SessionState.SYNCING, SessionEvent.HELLO_CONFIRMED)
        self.assertEqual(caught.exception.reason, "unsupported_transition")

    def test_preview_and_precheck_reject_invalid_api_argument_types(self):
        for current, event in (("syncing", SessionEvent.READY_REQUESTED),
                               (SessionState.SYNCING, "ready_requested")):
            with self.subTest(current=current, event=event):
                with self.assertRaises(TypeError):
                    validate_session_event(current, event)
                with self.assertRaises(TypeError):
                    preview_session_transition(current, event, self.context)
        with self.assertRaises(TypeError):
            preview_session_transition(SessionState.SYNCING, SessionEvent.READY_REQUESTED, {})
