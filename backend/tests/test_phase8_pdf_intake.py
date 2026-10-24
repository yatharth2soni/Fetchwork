"""Phase 8 tests: inbound ResourceContent without a verified PDF-bytes path.

Official ASI:One attachment docs deliver HTTPS Resource URIs; Chat Interface
storage is opaque and Resource has no inline bytes field. This suite proves
we detect ResourceContent, guide the user to paste `resume:`, and make zero
Jina/LLM/PDF/storage/load_resume calls.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from uagents_core.contrib.protocols.chat import (  # noqa: E402
    ChatAcknowledgement,
    ChatMessage,
    Resource,
    ResourceContent,
    TextContent,
)

from scripts.spike_agent import (  # noqa: E402
    PDF_UPLOAD_UNSUPPORTED_MESSAGE,
    _SEEN_MSG_IDS,
    _SESSION_LOCKS,
    clear_all_resumes,
    clear_session_drafts,
    count_resource_content_blocks,
    get_stored_resume,
    handle_chat_message,
    message_contains_resource_content,
    store_user_resume,
)
from app.data.resume import load_resume  # noqa: E402
from app.models import Resume  # noqa: E402


@pytest.fixture(autouse=True)
def clean_phase8_state():
    clear_session_drafts()
    clear_all_resumes()
    _SESSION_LOCKS.clear()
    _SEEN_MSG_IDS.clear()
    yield
    clear_session_drafts()
    clear_all_resumes()
    _SESSION_LOCKS.clear()
    _SEEN_MSG_IDS.clear()


@pytest.fixture
def spy_load_resume():
    with patch("scripts.spike_agent.load_resume", wraps=load_resume) as mock_load:
        yield mock_load
        mock_load.assert_not_called()


def _resource_msg(
    *,
    uri: str = "https://example.invalid/opaque/attachment",
    mime: str = "application/pdf",
    text: str | None = None,
    extra_resources: int = 0,
) -> ChatMessage:
    blocks: list = []
    if text is not None:
        blocks.append(TextContent(text=text))
    resources = [
        Resource(
            uri=uri,
            metadata={"mime_type": mime, "role": "attachment"},
        )
    ]
    for i in range(extra_resources):
        resources.append(
            Resource(
                uri=f"{uri}-{i}",
                metadata={"mime_type": mime, "role": "attachment"},
            )
        )
    blocks.append(
        ResourceContent(
            type="resource",
            resource_id=uuid4(),
            resource=resources[0] if len(resources) == 1 else resources,
        )
    )
    return ChatMessage(content=blocks)


def test_helpers_detect_resource_without_reading_uri_values():
    msg = _resource_msg(uri="https://secret.example/path/never-log-me.pdf")
    assert message_contains_resource_content(msg) is True
    assert count_resource_content_blocks(msg) == 1
    plain = ChatMessage(content=[TextContent(text="hello")])
    assert message_contains_resource_content(plain) is False


def test_resource_attachment_gives_fallback_and_zero_external_calls(
    spy_load_resume, caplog: pytest.LogCaptureFixture
):
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()
    sender = "agent1q_phase8_sender_a"
    secret_uri = "https://cdn.example.invalid/user-uploads/jane-example-resume.pdf"
    secret_name = "Jane Example"
    msg = _resource_msg(uri=secret_uri, text=f"Please use {secret_name}'s resume")

    with (
        patch("scripts.spike_agent.fetch_job_posting", new_callable=AsyncMock) as mock_fetch,
        patch(
            "scripts.spike_agent.generate_cover_letter", new_callable=AsyncMock
        ) as mock_gen,
        patch("scripts.spike_agent.deliver_pdf", new_callable=AsyncMock) as mock_deliver,
        patch("scripts.spike_agent.render_cover_letter_pdf") as mock_render,
        caplog.at_level(logging.DEBUG),
    ):
        asyncio.run(handle_chat_message(ctx=ctx, sender=sender, msg=msg))

        mock_fetch.assert_not_called()
        mock_gen.assert_not_called()
        mock_deliver.assert_not_called()
        mock_render.assert_not_called()

    assert get_stored_resume(sender) is None

    sent = [call.args[1] for call in ctx.send.await_args_list]
    assert isinstance(sent[0], ChatAcknowledgement)
    texts = [
        block.text
        for m in sent
        if isinstance(m, ChatMessage)
        for block in m.content
        if isinstance(block, TextContent)
    ]
    assert PDF_UPLOAD_UNSUPPORTED_MESSAGE in texts
    assert secret_uri not in "".join(texts)
    assert secret_name not in "".join(texts)

    # Logs may include truncated sender; must not include URI or extracted names.
    log_blob = " ".join(str(a) for call in ctx.logger.method_calls for a in call.args)
    log_blob += " " + caplog.text
    assert secret_uri not in log_blob
    assert "jane-example-resume" not in log_blob


def test_resource_plus_job_url_still_skips_fetcher_and_generator(spy_load_resume):
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()
    sender = "agent1q_phase8_sender_b"
    # Pre-store a resume so a job URL alone would otherwise proceed.
    store_user_resume(
        sender,
        Resume(
            name="Sam Sample",
            summary="Skills: Python\nExperience:\n- Built APIs with tests.",
        ),
    )
    msg = _resource_msg(
        text="https://jobs.example.com/posting/99",
        uri="https://example.invalid/resume.pdf",
    )

    with (
        patch("scripts.spike_agent.fetch_job_posting", new_callable=AsyncMock) as mock_fetch,
        patch(
            "scripts.spike_agent.generate_cover_letter", new_callable=AsyncMock
        ) as mock_gen,
    ):
        asyncio.run(handle_chat_message(ctx=ctx, sender=sender, msg=msg))
        mock_fetch.assert_not_called()
        mock_gen.assert_not_called()

    texts = [
        block.text
        for call in ctx.send.await_args_list
        if isinstance(call.args[1], ChatMessage)
        for block in call.args[1].content
        if isinstance(block, TextContent)
    ]
    assert PDF_UPLOAD_UNSUPPORTED_MESSAGE in texts
    # Existing resume must be left untouched on unsupported attachment path.
    stored = get_stored_resume(sender)
    assert stored is not None
    assert stored.name == "Sam Sample"


def test_generate_pdf_cta_still_wins_over_resource_block(spy_load_resume):
    """Card/CTA parsing runs before attachment guidance (routing priority)."""
    from scripts.spike_agent import store_session_draft

    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()
    sender = "agent1q_phase8_cta"
    store_session_draft(
        sender,
        "Dear Hiring Manager,\n\nBody.\n\nSincerely,\nSam Sample",
        "Sam Sample",
        "https://jobs.example.com/x",
    )
    msg = ChatMessage(
        content=[
            TextContent(text="generate_pdf"),
            ResourceContent(
                type="resource",
                resource_id=uuid4(),
                resource=Resource(
                    uri="https://example.invalid/should-not-trigger-pdf-intake",
                    metadata={"mime_type": "application/pdf", "role": "attachment"},
                ),
            ),
        ]
    )

    with patch("scripts.spike_agent.deliver_pdf", new_callable=AsyncMock) as mock_deliver:
        mock_deliver.return_value = None
        with patch("scripts.spike_agent.render_cover_letter_pdf", return_value=b"%PDF-1.4"):
            asyncio.run(handle_chat_message(ctx=ctx, sender=sender, msg=msg))
        mock_deliver.assert_awaited()

    texts = [
        block.text
        for call in ctx.send.await_args_list
        for block in getattr(call.args[1], "content", [])
        if isinstance(block, TextContent)
    ]
    assert PDF_UPLOAD_UNSUPPORTED_MESSAGE not in texts


def test_pasted_resume_path_still_works_after_phase8_routing(spy_load_resume):
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()
    sender = "agent1q_phase8_paste"
    resume_text = (
        "resume:\n"
        "Name: Jane Example\n"
        "Skills: Python, Git\n"
        "Experience:\n"
        "- Built and tested a Python CLI application."
    )
    msg = ChatMessage(content=[TextContent(text=resume_text)])
    asyncio.run(handle_chat_message(ctx=ctx, sender=sender, msg=msg))
    stored = get_stored_resume(sender)
    assert stored is not None
    assert stored.name == "Jane Example"
