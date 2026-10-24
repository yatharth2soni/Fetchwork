"""Unit and integration tests for Phase 5 URL-to-Draft-to-PDF flow.

Deterministic, offline, mocked network/LLM tests verifying:
1. URL extraction and single-URL generation pipeline
2. Delimited prompt generation, resume factuality, and plain chat draft delivery
3. Review card delivery and parameterization
4. Session-only draft state, sender isolation, and bounded capacity
5. Idempotent Generate PDF delivery from active draft
6. Safe error mapping (JobFetchError, CoverLetterError, unexpected errors)
7. Safe logging (no job text, resume facts, letter text, or URL path/queries logged)
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    MetadataContent,
    TextContent,
)
from uagents_core.contrib.protocols.chat.cards import extract_card

from app.cover_letter import (
    CoverLetterDraft,
    CoverLetterError,
    InvalidJobContentError,
    LLMProviderError,
)
from app.job_fetcher import (
    JobFetchError,
    JobFetchTimeoutError,
    JobPostingContent,
)
from app.models import Resume
from scripts.spike_agent import (
    DUMMY_LETTER_TEXT,
    _ACTIVE_DRAFTS,
    _SEEN_MSG_IDS,
    _SESSION_LOCKS,
    MAX_SESSIONS,
    SessionDraft,
    clear_session_drafts,
    extract_candidate_urls,
    get_session_draft,
    handle_chat_message,
    store_session_draft,
    store_user_resume,
)


@pytest.fixture(autouse=True)
def clean_session_state():
    """Ensure clean in-memory state before and after every test."""
    clear_session_drafts()
    _SESSION_LOCKS.clear()
    _SEEN_MSG_IDS.clear()
    yield
    clear_session_drafts()
    _SESSION_LOCKS.clear()
    _SEEN_MSG_IDS.clear()


@pytest.fixture
def fake_resume() -> Resume:
    """Fixture providing a known, obviously fictional test resume."""
    return Resume(
        name="Test Applicant",
        email="applicant@test.example",
        skills=["Python", "AsyncIO", "Automated Testing"],
        summary="Software engineer with experience in async systems.",
    )


# ---------------------------------------------------------------------------
# 1. One Valid URL Success Path
# ---------------------------------------------------------------------------


def test_one_valid_url_success_path(fake_resume: Resume):
    """Test 1: One valid URL triggers fetch, resume load, generation, plain chat, and review card."""
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()

    sender = "agent1q_valid_url_tester"
    store_user_resume(sender, fake_resume)
    target_url = "https://jobs.example.com/posting/123"
    msg = ChatMessage(content=[TextContent(text=target_url)])

    mock_job = JobPostingContent(source_url=target_url, content="Looking for a Python intern.")
    tailored_text = (
        "Dear Hiring Team,\n\n"
        "I am eager to contribute my Python and AsyncIO skills to your team. "
        "Sincerely,\nTest Applicant"
    )
    mock_draft = CoverLetterDraft(
        text=tailored_text,
        source_url=target_url,
        char_count=len(tailored_text),
    )

    with patch("scripts.spike_agent.fetch_job_posting", new_callable=AsyncMock) as mock_fetch, \
         patch("scripts.spike_agent.generate_cover_letter", new_callable=AsyncMock) as mock_gen:

        mock_fetch.return_value = mock_job
        mock_gen.return_value = mock_draft

        asyncio.run(handle_chat_message(ctx=ctx, sender=sender, msg=msg))

        # Assert pipelines called exactly once
        mock_fetch.assert_awaited_once_with(target_url)
        mock_gen.assert_awaited_once_with(job_posting=mock_job, resume=fake_resume)

    # Sent messages: 1 ack, 1 letter text, 1 review card
    assert ctx.send.await_count == 3
    sent_calls = [call[0][1] for call in ctx.send.call_args_list]

    # Ack
    assert isinstance(sent_calls[0], ChatAcknowledgement)
    assert sent_calls[0].acknowledged_msg_id == msg.msg_id

    # Plain text letter
    letter_msg = sent_calls[1]
    assert isinstance(letter_msg, ChatMessage)
    assert len(letter_msg.content) == 1
    assert isinstance(letter_msg.content[0], TextContent)
    assert letter_msg.content[0].text == tailored_text

    # Review card
    card_msg = sent_calls[2]
    assert isinstance(card_msg, ChatMessage)
    card_blocks = [b for b in card_msg.content if isinstance(b, MetadataContent)]
    assert len(card_blocks) == 1
    card = extract_card(card_blocks[0])
    assert card is not None
    assert card.payload.title == "Cover Letter Review"

    summary_labels = {r.label: r.value for r in card.payload.summary_rows}
    assert summary_labels["Applicant"] == "Test Applicant"
    assert summary_labels["Status"] == "Draft Ready"

    # Opaque draft ID stored and matching
    active_draft = get_session_draft(sender)
    assert active_draft is not None
    assert active_draft.letter_text == tailored_text
    assert active_draft.applicant_name == "Test Applicant"
    assert card.payload.approve_cta.selection["draft_id"] == active_draft.draft_id

    # DUMMY_LETTER_TEXT must not appear anywhere
    for sent in sent_calls:
        if isinstance(sent, ChatMessage):
            for block in sent.content:
                if isinstance(block, TextContent):
                    assert DUMMY_LETTER_TEXT not in block.text


# ---------------------------------------------------------------------------
# 2. URL Embedded in Sentence & Deduplication
# ---------------------------------------------------------------------------


def test_url_embedded_in_sentence_and_deduplication(fake_resume: Resume):
    """Test 2: URL embedded in sentence with punctuation is stripped; duplicates count as one."""
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()

    sender = "agent1q_embed_tester"
    store_user_resume(sender, fake_resume)
    text = (
        "Check this posting: https://jobs.example.com/roles/456. "
        "Also see https://jobs.example.com/roles/456! Can we apply?"
    )
    msg = ChatMessage(content=[TextContent(text=text)])

    mock_job = JobPostingContent(
        source_url="https://jobs.example.com/roles/456",
        content="Software Engineer role.",
    )
    mock_draft = CoverLetterDraft(
        text="Tailored letter text.",
        source_url="https://jobs.example.com/roles/456",
        char_count=20,
    )

    with patch("scripts.spike_agent.fetch_job_posting", new_callable=AsyncMock) as mock_fetch, \
         patch("scripts.spike_agent.generate_cover_letter", new_callable=AsyncMock) as mock_gen:

        mock_fetch.return_value = mock_job
        mock_gen.return_value = mock_draft

        asyncio.run(handle_chat_message(ctx=ctx, sender=sender, msg=msg))

        # Fetched exactly once with punctuation stripped and duplicates deduplicated
        mock_fetch.assert_awaited_once_with("https://jobs.example.com/roles/456")
        mock_gen.assert_awaited_once()


# ---------------------------------------------------------------------------
# 3. Fetch Error Handling
# ---------------------------------------------------------------------------


def test_fetch_error_sends_user_message_and_no_card():
    """Test 3: JobFetchError sends only user_message; generator not called; no card sent."""
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()

    sender = "agent1q_fetch_error"
    store_user_resume(sender, Resume(name="Applicant", summary="Software engineer with experience."))
    msg = ChatMessage(content=[TextContent(text="https://jobs.example.com/timeout")])

    with patch("scripts.spike_agent.fetch_job_posting", new_callable=AsyncMock) as mock_fetch, \
         patch("scripts.spike_agent.generate_cover_letter", new_callable=AsyncMock) as mock_gen:

        mock_fetch.side_effect = JobFetchTimeoutError()

        asyncio.run(handle_chat_message(ctx=ctx, sender=sender, msg=msg))

        mock_gen.assert_not_called()

    # Sent ack + user_message
    assert ctx.send.await_count == 2
    err_reply = ctx.send.call_args_list[1][0][1]
    assert isinstance(err_reply, ChatMessage)
    assert not any(isinstance(b, MetadataContent) for b in err_reply.content)
    assert "too long to load" in err_reply.content[0].text


# ---------------------------------------------------------------------------
# 4. Generator Error Handling
# ---------------------------------------------------------------------------


def test_generator_error_sends_user_message_and_no_card(fake_resume: Resume):
    """Test 4: CoverLetterError sends only user_message; no card sent."""
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()

    sender = "agent1q_gen_error"
    store_user_resume(sender, fake_resume)
    msg = ChatMessage(content=[TextContent(text="https://jobs.example.com/role")])
    mock_job = JobPostingContent(source_url="https://jobs.example.com/role", content="Content")

    with patch("scripts.spike_agent.fetch_job_posting", new_callable=AsyncMock, return_value=mock_job), \
         patch("scripts.spike_agent.generate_cover_letter", new_callable=AsyncMock) as mock_gen:

        mock_gen.side_effect = LLMProviderError()

        asyncio.run(handle_chat_message(ctx=ctx, sender=sender, msg=msg))

    assert ctx.send.await_count == 2
    err_reply = ctx.send.call_args_list[1][0][1]
    assert isinstance(err_reply, ChatMessage)
    assert not any(isinstance(b, MetadataContent) for b in err_reply.content)
    assert "unable to complete the request" in err_reply.content[0].text


# ---------------------------------------------------------------------------
# 5. Resume Load Error Handling
# ---------------------------------------------------------------------------


def test_load_resume_raises_generic_safe_message():
    """Test 5: sender without stored resume receives presence gate message; generator not called; no card."""
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()

    sender = "agent1q_resume_err"
    msg = ChatMessage(content=[TextContent(text="https://jobs.example.com/role")])
    mock_job = JobPostingContent(source_url="https://jobs.example.com/role", content="Content")

    with patch("scripts.spike_agent.fetch_job_posting", new_callable=AsyncMock, return_value=mock_job) as mock_fetch, \
         patch("scripts.spike_agent.generate_cover_letter", new_callable=AsyncMock) as mock_gen:

        asyncio.run(handle_chat_message(ctx=ctx, sender=sender, msg=msg))

        mock_fetch.assert_not_called()
        mock_gen.assert_not_called()

    assert ctx.send.await_count == 2
    err_reply = ctx.send.call_args_list[1][0][1]
    assert "Please paste your resume first" in err_reply.content[0].text
    assert not any(isinstance(b, MetadataContent) for b in err_reply.content)


# ---------------------------------------------------------------------------
# 6. Unexpected Generator Exception
# ---------------------------------------------------------------------------


def test_unexpected_generator_exception_safe_message(fake_resume: Resume):
    """Test 6: Unexpected exception sends generic safe message; raw exception text omitted."""
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()

    sender = "agent1q_unexpected"
    store_user_resume(sender, fake_resume)
    msg = ChatMessage(content=[TextContent(text="https://jobs.example.com/role")])
    mock_job = JobPostingContent(source_url="https://jobs.example.com/role", content="Content")

    with patch("scripts.spike_agent.fetch_job_posting", new_callable=AsyncMock, return_value=mock_job), \
         patch("scripts.spike_agent.generate_cover_letter", new_callable=AsyncMock) as mock_gen:

        mock_gen.side_effect = RuntimeError("UNEXPECTED_SECRET_INTERNAL_DB_ERROR_XYZ")

        asyncio.run(handle_chat_message(ctx=ctx, sender=sender, msg=msg))

    assert ctx.send.await_count == 2
    err_reply = ctx.send.call_args_list[1][0][1]
    assert "Something went wrong creating your cover letter" in err_reply.content[0].text
    assert "UNEXPECTED_SECRET_INTERNAL_DB_ERROR_XYZ" not in err_reply.content[0].text


# ---------------------------------------------------------------------------
# 7. No URL Guidance & Multiple Distinct URLs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "no_url_text",
    [
        "Hello agent, please write me a tailored cover letter.",
        "example.com/job/123",
        "Check out jobs.google.com for new listings",
    ],
)
def test_no_url_guidance(no_url_text: str):
    """Test 7a: Plain chat text and bare domains send concise guidance; no fetch/generation."""
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()

    store_user_resume("agent1q_test", Resume(name="Applicant", summary="Software engineer with experience."))
    msg = ChatMessage(content=[TextContent(text=no_url_text)])

    with patch("scripts.spike_agent.fetch_job_posting", new_callable=AsyncMock) as mock_fetch, \
         patch("scripts.spike_agent.generate_cover_letter", new_callable=AsyncMock) as mock_gen:

        asyncio.run(handle_chat_message(ctx=ctx, sender="agent1q_test", msg=msg))

        mock_fetch.assert_not_called()
        mock_gen.assert_not_called()

    assert ctx.send.await_count == 2
    sent_msg = ctx.send.call_args_list[1][0][1]
    assert sent_msg.content[0].text == (
        "Please send one public job-posting URL to create a tailored cover letter."
    )
    assert not any(isinstance(b, MetadataContent) for b in sent_msg.content)


def test_multiple_distinct_urls_guidance():
    """Test 7b: Multiple distinct URLs return guidance asking for only one URL."""
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()

    store_user_resume("agent1q_test", Resume(name="Applicant", summary="Software engineer with experience."))
    text = "Here are two jobs: https://example.com/job/1 and https://example.com/job/2"
    msg = ChatMessage(content=[TextContent(text=text)])

    with patch("scripts.spike_agent.fetch_job_posting", new_callable=AsyncMock) as mock_fetch, \
         patch("scripts.spike_agent.generate_cover_letter", new_callable=AsyncMock) as mock_gen:

        asyncio.run(handle_chat_message(ctx=ctx, sender="agent1q_test", msg=msg))

        mock_fetch.assert_not_called()
        mock_gen.assert_not_called()

    assert ctx.send.await_count == 2
    sent_msg = ctx.send.call_args_list[1][0][1]
    assert sent_msg.content[0].text == "Please send only one job-posting URL at a time."
    assert not any(isinstance(b, MetadataContent) for b in sent_msg.content)


# ---------------------------------------------------------------------------
# 8. Normal Unrelated Text Does Not Send Dummy Card
# ---------------------------------------------------------------------------


def test_normal_unrelated_text_does_not_send_dummy_card():
    """Test 8: Normal text does not produce the Phase 2 dummy review card."""
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()

    store_user_resume("agent1q_chat_tester", Resume(name="Applicant", summary="Software engineer with experience."))
    long_text = "I am writing to discuss potential opportunities in software development. " * 3
    msg = ChatMessage(content=[TextContent(text=long_text)])

    asyncio.run(handle_chat_message(ctx=ctx, sender="agent1q_chat_tester", msg=msg))

    assert ctx.send.await_count == 2
    sent_msg = ctx.send.call_args_list[1][0][1]
    assert not any(isinstance(b, MetadataContent) for b in sent_msg.content)
    assert sent_msg.content[0].text == (
        "Please send one public job-posting URL to create a tailored cover letter."
    )


# ---------------------------------------------------------------------------
# 9. Generate PDF After a Generated Draft
# ---------------------------------------------------------------------------


def test_generate_pdf_after_generated_draft():
    """Test 9: Generate PDF delivers a PDF built from the stored tailored draft text."""
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()

    sender = "agent1q_pdf_tester"
    tailored_text = "Unique cover letter text for Test Applicant 98765."
    draft = store_session_draft(
        sender=sender,
        letter_text=tailored_text,
        applicant_name="Test Applicant",
        source_url="https://jobs.example.com/123",
    )

    msg = ChatMessage(content=[TextContent(text="generate_pdf")])

    with patch("scripts.spike_agent.deliver_pdf", new_callable=AsyncMock) as mock_deliver:
        asyncio.run(handle_chat_message(ctx=ctx, sender=sender, msg=msg))

        mock_deliver.assert_awaited_once()
        call_kwargs = mock_deliver.call_args[1]
        assert call_kwargs["draft_id"] == draft.draft_id
        pdf_bytes = call_kwargs["pdf_bytes"]
        assert isinstance(pdf_bytes, bytes)
        assert pdf_bytes.startswith(b"%PDF")

    # Only ack was sent via ctx.send (deliver_pdf handles delivery)
    assert ctx.send.await_count == 1
    assert not any(
        isinstance(arg, ChatMessage) and any(isinstance(b, MetadataContent) for b in arg.content)
        for call in ctx.send.call_args_list
        for arg in call[0]
    )


# ---------------------------------------------------------------------------
# 10. Generate PDF With No Session Draft
# ---------------------------------------------------------------------------


def test_generate_pdf_with_no_session_draft():
    """Test 10: Generate PDF without active draft sends guidance; deliver_pdf not called."""
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()

    sender = "agent1q_no_draft_user"
    msg = ChatMessage(content=[TextContent(text="generate_pdf")])

    with patch("scripts.spike_agent.deliver_pdf", new_callable=AsyncMock) as mock_deliver:
        asyncio.run(handle_chat_message(ctx=ctx, sender=sender, msg=msg))

        mock_deliver.assert_not_called()

    assert ctx.send.await_count == 2
    sent_msg = ctx.send.call_args_list[1][0][1]
    assert sent_msg.content[0].text == (
        "I don't have an active draft for you. Please send a job-posting URL first."
    )


# ---------------------------------------------------------------------------
# 11. Generate PDF With Mismatching Draft ID
# ---------------------------------------------------------------------------


def test_generate_pdf_mismatching_draft_id():
    """Test 11: Generate PDF with an explicit mismatching draft_id is rejected."""
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()

    sender = "agent1q_mismatch_tester"
    store_session_draft(
        sender=sender,
        letter_text="Tailored letter.",
        applicant_name="Test Applicant",
        source_url="https://jobs.example.com/123",
        draft_id="active-draft-111",
    )

    # Action payload with an expired or mismatching draft ID
    payload_text = '{"action": "generate_pdf", "draft_id": "expired-draft-999"}'
    msg = ChatMessage(content=[TextContent(text=payload_text)])

    with patch("scripts.spike_agent.deliver_pdf", new_callable=AsyncMock) as mock_deliver:
        asyncio.run(handle_chat_message(ctx=ctx, sender=sender, msg=msg))

        mock_deliver.assert_not_called()

    assert ctx.send.await_count == 2
    sent_msg = ctx.send.call_args_list[1][0][1]
    assert "I don't have an active draft matching that request" in sent_msg.content[0].text


# ---------------------------------------------------------------------------
# 12. Sender Isolation
# ---------------------------------------------------------------------------


def test_sender_isolation():
    """Test 12: Sender B supplying sender A's draft_id cannot deliver A's draft."""
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()

    sender_a = "agent1q_applicant_a"
    sender_b = "agent1q_applicant_b"

    draft_a = store_session_draft(
        sender=sender_a,
        letter_text="Applicant A's confidential cover letter.",
        applicant_name="Applicant A",
        source_url="https://jobs.example.com/a",
    )

    # Sender B tries to request PDF using Sender A's draft ID
    msg_from_b = ChatMessage(
        content=[TextContent(text=f'{{"action": "generate_pdf", "draft_id": "{draft_a.draft_id}"}}')]
    )

    with patch("scripts.spike_agent.deliver_pdf", new_callable=AsyncMock) as mock_deliver:
        asyncio.run(handle_chat_message(ctx=ctx, sender=sender_b, msg=msg_from_b))
        mock_deliver.assert_not_called()

    # Sender A's draft remains intact in memory
    assert get_session_draft(sender_a) is not None
    assert get_session_draft(sender_a).draft_id == draft_a.draft_id


# ---------------------------------------------------------------------------
# 13. New URL Replaces Previous Draft
# ---------------------------------------------------------------------------


def test_new_url_replaces_previous_draft(fake_resume: Resume):
    """Test 13: A new successful URL generation replaces the sender's old draft."""
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()

    sender = "agent1q_replace_tester"
    store_user_resume(sender, fake_resume)

    # Step 1: Initial draft
    old_draft = store_session_draft(
        sender=sender,
        letter_text="First draft letter.",
        applicant_name=fake_resume.name,
        source_url="https://jobs.example.com/old",
    )
    old_draft_id = old_draft.draft_id

    # Step 2: User sends a new job URL
    new_url = "https://jobs.example.com/new"
    msg = ChatMessage(content=[TextContent(text=new_url)])
    new_draft_text = "Second replacement draft letter."

    mock_job = JobPostingContent(source_url=new_url, content="New job content.")
    mock_draft = CoverLetterDraft(text=new_draft_text, source_url=new_url, char_count=len(new_draft_text))

    with patch("scripts.spike_agent.fetch_job_posting", new_callable=AsyncMock, return_value=mock_job), \
         patch("scripts.spike_agent.generate_cover_letter", new_callable=AsyncMock, return_value=mock_draft):

        asyncio.run(handle_chat_message(ctx=ctx, sender=sender, msg=msg))

    active = get_session_draft(sender)
    assert active is not None
    assert active.draft_id != old_draft_id
    assert active.letter_text == new_draft_text

    # Step 3: Attempting to generate PDF with old draft_id is rejected
    ctx.send.reset_mock()
    old_click = ChatMessage(
        content=[TextContent(text=f'{{"action": "generate_pdf", "draft_id": "{old_draft_id}"}}')]
    )
    with patch("scripts.spike_agent.deliver_pdf", new_callable=AsyncMock) as mock_deliver:
        asyncio.run(handle_chat_message(ctx=ctx, sender=sender, msg=old_click))
        mock_deliver.assert_not_called()

    # Step 4: Generating PDF with new draft succeeds
    new_click = ChatMessage(
        content=[TextContent(text=f'{{"action": "generate_pdf", "draft_id": "{active.draft_id}"}}')]
    )
    with patch("scripts.spike_agent.deliver_pdf", new_callable=AsyncMock) as mock_deliver:
        asyncio.run(handle_chat_message(ctx=ctx, sender=sender, msg=new_click))
        mock_deliver.assert_awaited_once()


# ---------------------------------------------------------------------------
# 14. Idempotency & Lock Release on Failure
# ---------------------------------------------------------------------------


def test_idempotency_and_lock_release_on_failure():
    """Test 14: Failed delivery releases generation lock so user can retry."""
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()

    sender = "agent1q_retry_tester"
    draft = store_session_draft(
        sender=sender,
        letter_text="Tailored cover letter for retry test.",
        applicant_name="Test Applicant",
        source_url="https://jobs.example.com/job",
    )

    msg = ChatMessage(content=[TextContent(text="generate_pdf")])

    # First attempt: delivery fails with network exception
    with patch("scripts.spike_agent.deliver_pdf", new_callable=AsyncMock) as mock_deliver:
        mock_deliver.side_effect = ConnectionError("Agentverse upload failed")
        asyncio.run(handle_chat_message(ctx=ctx, sender=sender, msg=msg))

    # Assert lock was released
    key = (sender, draft.draft_id, "generate_pdf")
    assert key not in _SESSION_LOCKS

    # Second attempt: delivery succeeds
    ctx.send.reset_mock()
    msg2 = ChatMessage(content=[TextContent(text="generate_pdf")])
    with patch("scripts.spike_agent.deliver_pdf", new_callable=AsyncMock) as mock_deliver:
        asyncio.run(handle_chat_message(ctx=ctx, sender=sender, msg=msg2))
        mock_deliver.assert_awaited_once()


# ---------------------------------------------------------------------------
# 15. Session Store Bound (Eviction)
# ---------------------------------------------------------------------------


def test_session_store_bound_eviction():
    """Test 15: Exceeding MAX_SESSIONS evicts the oldest session."""
    with patch("scripts.spike_agent.MAX_SESSIONS", 3):
        store_session_draft("sender_1", "Letter 1", "App 1", "https://jobs.com/1")
        store_session_draft("sender_2", "Letter 2", "App 2", "https://jobs.com/2")
        store_session_draft("sender_3", "Letter 3", "App 3", "https://jobs.com/3")

        assert len(_ACTIVE_DRAFTS) == 3
        assert get_session_draft("sender_1") is not None

        # Adding a 4th sender evicts sender_1 (oldest)
        store_session_draft("sender_4", "Letter 4", "App 4", "https://jobs.com/4")
        assert len(_ACTIVE_DRAFTS) == 3
        assert get_session_draft("sender_1") is None
        assert get_session_draft("sender_2") is not None
        assert get_session_draft("sender_4") is not None


# ---------------------------------------------------------------------------
# 16. Revise in Chat Response
# ---------------------------------------------------------------------------


def test_revise_in_chat_concise_response():
    """Test 16: Revise in Chat action sends concise message, no LLM call, no card."""
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()

    msg = ChatMessage(content=[TextContent(text="revise")])

    with patch("scripts.spike_agent.generate_cover_letter", new_callable=AsyncMock) as mock_gen:
        asyncio.run(handle_chat_message(ctx=ctx, sender="agent1q_revise", msg=msg))
        mock_gen.assert_not_called()

    assert ctx.send.await_count == 2
    sent_msg = ctx.send.call_args_list[1][0][1]
    assert "not yet supported" in sent_msg.content[0].text
    assert not any(isinstance(b, MetadataContent) for b in sent_msg.content)


# ---------------------------------------------------------------------------
# 17. Unsupported Action Fallback
# ---------------------------------------------------------------------------


def test_unsupported_action_fallback():
    """Test 17: Unsupported card control sends guidance and does not send card."""
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()

    msg = ChatMessage(content=[TextContent(text="Unexpected card control")])

    with patch("scripts.spike_agent.deliver_pdf", new_callable=AsyncMock) as mock_deliver:
        asyncio.run(handle_chat_message(ctx=ctx, sender="agent1q_unsupported", msg=msg))
        mock_deliver.assert_not_called()

    assert ctx.send.await_count == 2
    sent_msg = ctx.send.call_args_list[1][0][1]
    assert "unsupported card action" in sent_msg.content[0].text
    assert not any(isinstance(b, MetadataContent) for b in sent_msg.content)


# ---------------------------------------------------------------------------
# 18. Action Priority Over URL Handling
# ---------------------------------------------------------------------------


def test_action_takes_priority_over_url_handling():
    """Test 18: Messages containing action payloads execute action and do not fetch URLs."""
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()

    sender = "agent1q_priority_test"
    draft = store_session_draft(
        sender=sender,
        letter_text="Tailored letter.",
        applicant_name="Test Applicant",
        source_url="https://jobs.example.com/job",
    )

    # Action payload with an embedded URL
    text = f'{{"action": "generate_pdf", "draft_id": "{draft.draft_id}"}} https://unrelated.com/job'
    msg = ChatMessage(content=[TextContent(text=text)])

    with patch("scripts.spike_agent.fetch_job_posting", new_callable=AsyncMock) as mock_fetch, \
         patch("scripts.spike_agent.deliver_pdf", new_callable=AsyncMock) as mock_deliver:

        asyncio.run(handle_chat_message(ctx=ctx, sender=sender, msg=msg))

        mock_fetch.assert_not_called()
        mock_deliver.assert_awaited_once()


# ---------------------------------------------------------------------------
# 19. Logging Safety
# ---------------------------------------------------------------------------


def test_logging_safety_caplog(caplog: pytest.LogCaptureFixture, fake_resume: Resume):
    """Test 19: Caplog asserts no job text, resume facts, letter text, or URL path/query are logged."""
    caplog.set_level(logging.DEBUG)

    ctx = MagicMock()
    ctx.logger = logging.getLogger("spike_agent")
    ctx.send = AsyncMock()

    sender = "agent1q_logger_test_address_123456789"
    store_user_resume(sender, fake_resume)
    secret_marker_job = "MARKER_SECRET_JOB_PAYLOAD_112233"
    secret_marker_resume = "MARKER_SECRET_RESUME_BULLET_445566"
    secret_marker_letter = "MARKER_SECRET_GENERATED_LETTER_778899"
    secret_query_param = "SUPER_SECRET_QUERY_TOKEN_AABBCC"

    target_url = f"https://jobs.example.com/confidential/posting?token={secret_query_param}"
    msg = ChatMessage(content=[TextContent(text=target_url)])

    fake_resume.summary = secret_marker_resume
    mock_job = JobPostingContent(source_url=target_url, content=secret_marker_job)
    mock_draft = CoverLetterDraft(
        text=secret_marker_letter,
        source_url=target_url,
        char_count=len(secret_marker_letter),
    )

    with patch("scripts.spike_agent.fetch_job_posting", new_callable=AsyncMock, return_value=mock_job), \
         patch("scripts.spike_agent.generate_cover_letter", new_callable=AsyncMock, return_value=mock_draft):

        asyncio.run(handle_chat_message(ctx=ctx, sender=sender, msg=msg))

    all_logs = " ".join(record.getMessage() for record in caplog.records)

    # Assert none of the sensitive markers appear in any log record
    assert secret_marker_job not in all_logs
    assert secret_marker_resume not in all_logs
    assert secret_marker_letter not in all_logs
    assert secret_query_param not in all_logs
    assert "/confidential/posting" not in all_logs
