import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4


import pytest
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    MetadataContent,
    ResourceContent,
    TextContent,
)
from uagents_core.contrib.protocols.chat.cards import (
    create_card_response_content,
    extract_card,
)

# Ensure backend directory is in sys.path
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from scripts.spike_agent import (
    DUMMY_LETTER_TEXT,
    _IDEMPOTENCY_WINDOW_S,
    _SESSION_LOCKS,
    _is_duplicate_request,
    _strip_agent_mention,
    build_review_card,
    classify_text_cta,
    deliver_pdf,
    handle_chat_message,
    is_json_like_text,
    log_inbound_diagnostics,
    make_remote_asset_name,
    parse_action_selection,
    parse_canonical_text_action,
    render_dummy_pdf,
    sanitize_payload,
)





def test_render_dummy_pdf_produces_valid_pdf_bytes():
    """Verify render_dummy_pdf creates non-empty PDF bytes with %PDF magic header."""
    pdf_bytes = render_dummy_pdf(DUMMY_LETTER_TEXT)
    assert isinstance(pdf_bytes, bytes)
    assert len(pdf_bytes) > 200
    assert pdf_bytes.startswith(b"%PDF")


def test_build_review_card_schema():
    """Verify review card structure adheres strictly to the installed ReviewCardPayload schema."""
    content = build_review_card(draft_id="draft-test-123")
    assert isinstance(content, MetadataContent)
    assert content.metadata["card_protocol_version"] == "1"
    assert content.metadata["requires_card_interaction"] == "true"
    assert content.metadata["card_kind"] == "review"

    # Validate with extract_card helper
    card = extract_card(content)
    assert card is not None
    assert card.payload.title == "Cover Letter Review"
    assert card.payload.approve_cta.label == "Generate PDF"
    assert card.payload.approve_cta.selection == {
        "action": "generate_pdf",
        "draft_id": "draft-test-123",
    }
    assert card.payload.reject_cta is not None
    assert card.payload.reject_cta.selection == {
        "action": "revise",
        "draft_id": "draft-test-123",
    }


def test_sanitize_payload_redacts_unknown_and_sensitive_keys():
    """Verify sanitize_payload only preserves safe fields and redacts secrets."""
    payload = {
        "action": "generate_pdf",
        "draft_id": "test-id",
        "secret_token": "sk-1234567890",
        "api_key": "super-secret",
    }
    sanitized = sanitize_payload(payload)
    assert sanitized["action"] == "generate_pdf"
    assert sanitized["draft_id"] == "test-id"
    assert sanitized["secret_token"] == "<redacted>"
    assert sanitized["api_key"] == "<redacted>"


def test_parse_action_selection_from_metadata_content():
    """Verify parsing selection from standard MetadataContent card response."""
    response_meta = create_card_response_content(
        card_id=uuid4(),
        selection={"action": "generate_pdf", "draft_id": "d-456"},
    )
    msg = ChatMessage(content=[response_meta])
    selection = parse_action_selection(msg)
    assert selection == {"action": "generate_pdf", "draft_id": "d-456"}


def test_parse_action_selection_from_direct_metadata_selection_json():
    """Verify parsing selection when metadata contains raw JSON string without standard wrapper."""
    meta = MetadataContent(
        metadata={
            "selection": json.dumps({"action": "generate_pdf", "draft_id": "d-direct"}),
            "card_id": str(uuid4()),
        }
    )
    msg = ChatMessage(content=[meta])
    selection = parse_action_selection(msg)
    assert selection == {"action": "generate_pdf", "draft_id": "d-direct"}


def test_parse_action_selection_from_direct_metadata_action_key():
    """Verify parsing action when metadata directly provides the action and draft_id."""
    meta = MetadataContent(
        metadata={
            "action": "generate_pdf",
            "draft_id": "d-direct-action",
        }
    )
    msg = ChatMessage(content=[meta])
    selection = parse_action_selection(msg)
    assert selection == {"action": "generate_pdf", "draft_id": "d-direct-action"}


def test_parse_action_selection_from_text_fallback():
    """Verify parsing selection from legacy JSON text fallback with allowed keys."""
    msg = ChatMessage(
        content=[TextContent(text='{"action": "generate_pdf", "draft_id": "d-789"}')]
    )
    selection = parse_action_selection(msg)
    assert selection == {"action": "generate_pdf", "draft_id": "d-789"}


def test_parse_action_selection_malformed_selection():
    """Verify malformed JSON or disallowed extra keys in text fallback are safely rejected."""
    # Malformed JSON in metadata
    meta_bad = MetadataContent(metadata={"selection": "{not-valid-json"})
    assert parse_action_selection(ChatMessage(content=[meta_bad])) is None

    # Malformed JSON in text
    msg_bad_json = ChatMessage(content=[TextContent(text="{not-valid-json}")])
    assert parse_action_selection(msg_bad_json) is None

    # Text fallback with unsupported action
    msg_unsupported = ChatMessage(
        content=[
            TextContent(
                text='{"action": "unsupported_action", "draft_id": "d-1"}'
            )
        ]
    )
    assert parse_action_selection(msg_unsupported) is None

    # Text fallback without 'action' key
    msg_no_action = ChatMessage(content=[TextContent(text='{"draft_id": "d-1"}')])
    assert parse_action_selection(msg_no_action) is None


def test_parse_action_selection_returns_none_for_ordinary_user_text():
    """Verify arbitrary ordinary user text is never mistakenly parsed as a card action."""
    test_cases = [
        "Hello, please write a cover letter.",
        "Click Generate PDF below",
        "Here is my feedback: please shorten paragraph 2.",
        "{Please draft my letter}",
    ]
    for text in test_cases:
        msg = ChatMessage(content=[TextContent(text=text)])
        assert parse_action_selection(msg) is None, f"Failed for text: {text!r}"


def test_log_inbound_diagnostics_redacts_text_and_records_cta_shape():
    """Verify text diagnostics retain shape data without logging the text value."""
    ctx = MagicMock()
    ctx.logger = MagicMock()
    inbound_text = "Generate\u200b PDF with private feedback"

    log_inbound_diagnostics(
        ctx,
        ChatMessage(content=[TextContent(text=inbound_text)]),
    )

    log_lines = [call.args[0] for call in ctx.logger.info.call_args_list]
    assert all(inbound_text not in line for line in log_lines)
    assert any("text_length=35" in line for line in log_lines)
    assert any("U+200B" in line for line in log_lines)
    assert any("normalized_action=none" in line for line in log_lines)


def test_classify_text_cta_supports_all_exact_equivalents():
    """Verify every supported CTA equivalent maps to its intended action."""
    cases = [
        ("Generate PDF", "generate_pdf"),
        ("generate pdf", "generate_pdf"),
        ("generate_pdf", "generate_pdf"),
        ("generatepdf", "generate_pdf"),
        ("create pdf", "generate_pdf"),
        ("createpdf", "generate_pdf"),
        ("download pdf", "generate_pdf"),
        ("downloadpdf", "generate_pdf"),
        ("revise", "revise"),
        ("Revise in Chat", "revise"),
        ("revise in chat", "revise"),
        ("revise_in_chat", "revise"),
        ("reviseinchat", "revise"),
    ]
    for input_text, expected_action in cases:
        assert classify_text_cta(input_text) == expected_action


def test_classify_text_cta_handles_invisible_unicode_and_punctuation():
    """Verify CTA classification survives known client presentation wrappers."""
    cases = [
        ("Generate\u200bPDF", "generate_pdf"),
        ("Generate\u00a0PDF", "generate_pdf"),
        ("Generate-PDF", "generate_pdf"),
        ("Create / PDF", "generate_pdf"),
        ("Download.PDF", "generate_pdf"),
        ("Revise—in—Chat", "revise"),
    ]
    for input_text, expected_action in cases:
        assert classify_text_cta(input_text) == expected_action


def test_classify_text_cta_strips_agent_mention():
    """Verify ASI:One @mention prefix is stripped before CTA matching.

    ASI:One sends direct messages to an agent prefixed with the agent's
    @address, e.g. '@agent1q07xd... generate_pdf'. The mention must be
    stripped, and only the remainder matched as an exact CTA.
    """
    mention_cases = [
        # Agent address mention (confirmed ASI:One live behavior)
        (
            "@agent1q07xd588w2te2e7jj89240jxgxz66fdft09typ052egt4k5ngyhtkwh59ud generate_pdf",
            "generate_pdf",
        ),
        (
            "@agent1q07xd588w2te2e7jj89240jxgxz66fdft09typ052egt4k5ngyhtkwh59ud Generate PDF",
            "generate_pdf",
        ),
        # Display name mention
        ("@InternAssist generate pdf", "generate_pdf"),
        ("@InternAssist generate_pdf", "generate_pdf"),
        ("@InternAssist revise", "revise"),
        ("@InternAssist Revise in Chat", "revise"),
        # Case variants
        ("@INTERNASSIST GENERATE PDF", "generate_pdf"),
    ]
    for input_text, expected_action in mention_cases:
        result = classify_text_cta(input_text)
        assert result == expected_action, (
            f"Expected {expected_action!r} for {input_text!r}, got {result!r}"
        )


def test_classify_text_cta_mention_does_not_match_embedded_command():
    """Verify commands embedded inside normal prose are never matched.

    After mention-stripping, the remainder must be an exact CTA phrase.
    Sentences that contain a CTA word but surround it with other words must not
    trigger any action.
    """
    non_action_cases = [
        # Embedded in sentence — no @mention
        "Can you explain generate_pdf?",
        "I want to generate_pdf later",
        "Please generate a PDF for me",
        "Let me know when you generate pdf",
        # @mention with a non-CTA remainder
        "@InternAssist please help me",
        "@InternAssist can you generate a pdf?",
        # Two @mentions in a row (second is not a CTA)
        "@InternAssist @otheragent generate_pdf",
        # Trailing garbage after CTA
        "@InternAssist generate pdf now",
        "@InternAssist generate_pdf please",
        # Plain word that isn't a CTA
        "@InternAssist hello",
    ]
    for text in non_action_cases:
        result = classify_text_cta(text)
        assert result is None, (
            f"Expected None for non-CTA text {text!r}, got {result!r}"
        )


def test_parse_action_selection_mention_prefix_reaches_deliver_pdf():
    """Verify @mention-prefixed generate_pdf triggers deliver_pdf exactly once.

    Covers the confirmed ASI:One live behavior where direct messages arrive as
    '@agent1q... generate_pdf' TextContent blocks.
    """
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()

    agent_addr = "agent1q07xd588w2te2e7jj89240jxgxz66fdft09typ052egt4k5ngyhtkwh59ud"
    mention_text = f"@{agent_addr} generate_pdf"
    msg = ChatMessage(content=[TextContent(text=mention_text)])

    with patch("scripts.spike_agent.deliver_pdf", new_callable=AsyncMock) as mock_deliver:
        asyncio.run(
            handle_chat_message(
                ctx=ctx,
                sender=agent_addr,
                msg=msg,
                pdf_storage="local",
            )
        )

        # deliver_pdf must be called exactly once
        mock_deliver.assert_awaited_once()
        assert mock_deliver.call_args[1]["draft_id"] == "dummy-draft-001"

        # Only the ChatAcknowledgement is sent via ctx.send (deliver_pdf is mocked)
        ctx.send.assert_awaited_once()
        sent_ack = ctx.send.call_args[0][1]
        assert sent_ack.acknowledged_msg_id == msg.msg_id

        # No new Review Card must have been sent
        assert not any(
            isinstance(arg, ChatMessage)
            and any(isinstance(b, MetadataContent) for b in arg.content)
            for call in ctx.send.call_args_list
            for arg in call[0]
        )


def test_parse_action_selection_text_cta_rejects_non_cta_text():
    """Verify plain-text CTA fallback does NOT match partial or unrelated phrases."""
    non_cta_cases = [
        "Generate PDF now",          # trailing word
        "Please Generate PDF",       # leading word
        "Generate",                  # incomplete
        "PDF",                       # partial
        "Revise it",                 # trailing word
        "Please revise",             # leading word
        "generate_pdf_now",          # underscore suffix
        "please generate a PDF for me",
        "I want to revise my letter.",
        "",                          # empty string
        "   ",                       # whitespace only
    ]
    for text in non_cta_cases:
        msg = ChatMessage(content=[TextContent(text=text)])
        assert parse_action_selection(msg) is None, (
            f"Expected None for non-CTA text {text!r}"
        )


def test_exact_text_cta_reaches_deliver_pdf_exactly_once():
    """Verify clicking Generate PDF triggers deliver_pdf exactly once and sends no new Review Card."""
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()

    msg = ChatMessage(content=[TextContent(text="Generate PDF")])

    with patch("scripts.spike_agent.deliver_pdf", new_callable=AsyncMock) as mock_deliver:
        asyncio.run(
            handle_chat_message(
                ctx=ctx,
                sender="agent1q_test",
                msg=msg,
                pdf_storage="local",
            )
        )

        # 1. deliver_pdf was awaited exactly once
        mock_deliver.assert_awaited_once()
        assert mock_deliver.call_args[1]["draft_id"] == "dummy-draft-001"

        # 2. Sent ChatAcknowledgement for msg.msg_id
        ctx.send.assert_awaited_once()
        sent_ack = ctx.send.call_args[0][1]
        assert sent_ack.acknowledged_msg_id == msg.msg_id

        # 3. No new Review Card was sent in reply
        assert not any(
            isinstance(arg, ChatMessage) and any(isinstance(b, MetadataContent) for b in arg.content)
            for call in ctx.send.call_args_list
            for arg in call[0]
        )


def test_unsupported_single_text_action_does_not_call_deliver_pdf_or_resend_card():
    """Verify the temporary unsupported-CTA fallback is safe and non-looping."""
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()

    msg = ChatMessage(content=[TextContent(text="Unexpected card control")])

    with patch("scripts.spike_agent.deliver_pdf", new_callable=AsyncMock) as mock_deliver:
        asyncio.run(
            handle_chat_message(
                ctx=ctx,
                sender="agent1q_test",
                msg=msg,
                pdf_storage="local",
            )
        )

        # 1. deliver_pdf was NOT called
        mock_deliver.assert_not_called()

        # 2. Sent ChatAcknowledgement + unsupported-action reply, with no Review Card
        assert ctx.send.await_count == 2
        sent_msg = ctx.send.call_args_list[1][0][1]
        assert isinstance(sent_msg, ChatMessage)
        assert not any(isinstance(b, MetadataContent) for b in sent_msg.content)
        assert sent_msg.content[0].text == (
            "I received an unsupported card action. Please type `generate_pdf` to continue."
        )


def test_long_ordinary_text_still_sends_review_card():
    """Verify the temporary fallback is limited to normalized text of 80 characters."""
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()
    msg = ChatMessage(content=[TextContent(text="x" * 81)])

    with patch("scripts.spike_agent.deliver_pdf", new_callable=AsyncMock) as mock_deliver:
        asyncio.run(handle_chat_message(ctx=ctx, sender="agent1q_test", msg=msg))

    mock_deliver.assert_not_called()
    sent_msg = ctx.send.call_args_list[1][0][1]
    assert any(isinstance(block, MetadataContent) for block in sent_msg.content)



def test_deliver_pdf_local_fallback(tmp_path: Path):
    """Verify local fallback writes PDF file to disk and sends clear explanation."""
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()

    pdf_bytes = render_dummy_pdf()
    draft_id = "test-draft-local"

    asyncio.run(
        deliver_pdf(
            ctx=ctx,
            sender="test-user-address",
            draft_id=draft_id,
            pdf_bytes=pdf_bytes,
            pdf_storage="local",
            agentverse_api_key=None,
            output_dir=tmp_path,
        )
    )

    expected_file = tmp_path / f"cover_letter_{draft_id}.pdf"
    assert expected_file.is_file()
    assert expected_file.read_bytes() == pdf_bytes

    ctx.send.assert_awaited_once()
    sent_msg = ctx.send.call_args[0][1]
    assert isinstance(sent_msg, ChatMessage)
    assert any(
        "local file fallback" in block.text
        for block in sent_msg.content
        if isinstance(block, TextContent)
    )
    assert any(isinstance(block, EndSessionContent) for block in sent_msg.content)


def test_deliver_pdf_agentverse_resource_content(tmp_path: Path):
    """Verify Agentverse delivery creates asset, sets permissions, and emits ResourceContent."""
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()

    pdf_bytes = render_dummy_pdf()
    draft_id = "test-draft-av"
    mock_asset_id = str(uuid4())

    with patch("scripts.spike_agent.ExternalStorage") as mock_storage_cls:
        mock_instance = MagicMock()
        mock_instance.storage_url = "https://agentverse.ai/v1/storage"
        mock_instance.create_asset.return_value = mock_asset_id
        mock_storage_cls.return_value = mock_instance

        asyncio.run(
            deliver_pdf(
                ctx=ctx,
                sender="agent1q_sender",
                draft_id=draft_id,
                pdf_bytes=pdf_bytes,
                pdf_storage="agentverse",
                agentverse_api_key="mock-key",
                output_dir=tmp_path,
            )
        )

        # Remote name must be unique (starts with prefix, ends with .pdf, not static).
        call_kwargs = mock_instance.create_asset.call_args[1]
        remote_name = call_kwargs["name"]
        assert remote_name.startswith("cover_letter_test-draft-av_"), (
            f"Expected unique remote name prefix, got: {remote_name!r}"
        )
        assert remote_name.endswith(".pdf")
        # Must include a timestamp-like segment and a uid hex segment
        assert "T" in remote_name and "Z" in remote_name

        mock_instance.set_permissions.assert_called_once_with(
            asset_id=mock_asset_id,
            agent_address="agent1q_sender",
            read=True,
            write=False,
        )

        ctx.send.assert_awaited_once()
        sent_msg = ctx.send.call_args[0][1]
        assert isinstance(sent_msg, ChatMessage)
        resource_blocks = [
            b for b in sent_msg.content if isinstance(b, ResourceContent)
        ]
        assert len(resource_blocks) == 1
        assert str(resource_blocks[0].resource_id) == mock_asset_id
        assert resource_blocks[0].resource.metadata["mime_type"] == "application/pdf"


# ---------------------------------------------------------------------------
# New focused tests for this session's fixes
# ---------------------------------------------------------------------------


def test_make_remote_asset_name_is_unique_across_two_calls():
    """Verify two calls with the same draft_id produce different remote asset names.

    This prevents HTTP 409 'asset already exists' errors on Agentverse.
    """
    name1 = make_remote_asset_name("dummy-draft-001")
    name2 = make_remote_asset_name("dummy-draft-001")
    assert name1.startswith("cover_letter_dummy-draft-001_")
    assert name1.endswith(".pdf")
    assert name1 != name2, "Two consecutive calls must produce distinct names"


def test_parse_action_selection_mention_prefixed_json():
    """Verify mention-prefixed JSON payload is parsed correctly.

    ASI:One can send a follow-up message like:
      '@agent1q... {"action":"generate_pdf","draft_id":"dummy-draft-001"}'
      '@InternAssist {"action": "generate_pdf"}'
    The @mention must be stripped and the JSON parsed as a valid action.
    """
    agent_addr = "agent1q07xd588w2te2e7jj89240jxgxz66fdft09typ052egt4k5ngyhtkwh59ud"
    payload_text = f'@{agent_addr} {{"action":"generate_pdf","draft_id":"dummy-draft-001"}}'
    msg = ChatMessage(content=[TextContent(text=payload_text)])
    result = parse_action_selection(msg)
    assert result is not None
    assert result["action"] == "generate_pdf"
    assert result["draft_id"] == "dummy-draft-001"

    # Display-name mention without draft_id
    msg2 = ChatMessage(content=[TextContent(text='@InternAssist {"action": "generate_pdf"}')])
    result2 = parse_action_selection(msg2)
    assert result2 is not None
    assert result2["action"] == "generate_pdf"

    # Payload containing extra CTA keys (e.g. primary: true, as sent by ASI:One card clicks)
    payload_with_primary = f'@{agent_addr} {{"action":"generate_pdf","draft_id":"dummy-draft-001","primary":true}}'
    msg4 = ChatMessage(content=[TextContent(text=payload_with_primary)])
    result4 = parse_action_selection(msg4)
    assert result4 is not None
    assert result4["action"] == "generate_pdf"
    assert result4["draft_id"] == "dummy-draft-001"

    # Markdown code-fence wrapped payload
    payload_markdown = f'@{agent_addr} ```json\n{{"action":"generate_pdf","draft_id":"dummy-draft-001"}}\n```'
    msg5 = ChatMessage(content=[TextContent(text=payload_markdown)])
    result5 = parse_action_selection(msg5)
    assert result5 is not None
    assert result5["action"] == "generate_pdf"


def test_parse_action_selection_mention_prefixed_json_invalid_or_missing():
    """Verify invalid JSON, missing action, unknown action, or embedded prose return None safely."""
    agent_addr = "agent1q07xd588w2te2e7jj89240jxgxz66fdft09typ052egt4k5ngyhtkwh59ud"

    # Malformed JSON
    msg1 = ChatMessage(content=[TextContent(text=f'@{agent_addr} {{"action": "generate_pdf"')])
    assert parse_action_selection(msg1) is None

    # Missing action key
    msg2 = ChatMessage(content=[TextContent(text=f'@{agent_addr} {{"draft_id": "dummy-draft-001"}}')])
    assert parse_action_selection(msg2) is None

    # Non-string action
    msg3 = ChatMessage(content=[TextContent(text=f'@{agent_addr} {{"action": 123}}')])
    assert parse_action_selection(msg3) is None

    # Unauthorized action name
    msg4 = ChatMessage(content=[TextContent(text=f'@{agent_addr} {{"action": "unauthorized_command"}}')])
    assert parse_action_selection(msg4) is None

    # Embedded mention before JSON in a sentence (not a leading mention / primary content)
    msg6 = ChatMessage(content=[TextContent(text=f'Please run @{agent_addr} {{"action": "generate_pdf"}}')])
    assert parse_action_selection(msg6) is None


def test_concurrent_generate_pdf_does_not_call_deliver_pdf_twice():
    """Verify an active generation suppresses a concurrent duplicate request."""
    import scripts.spike_agent as sa

    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()

    sender = "agent1q_idempotency_test"
    draft_id = "idem-draft-001"
    msg = ChatMessage(
        content=[TextContent(text="generate_pdf")]
    )

    # Clear any stale session lock for this sender+draft.
    key = (sender, draft_id, "generate_pdf")
    sa._SESSION_LOCKS.pop(key, None)

    # Simulate the first handler holding the in-progress lock while it uploads.
    assert _is_duplicate_request(sender, draft_id, "generate_pdf") is False

    with patch("scripts.spike_agent.deliver_pdf", new_callable=AsyncMock) as mock_deliver:
        asyncio.run(
            handle_chat_message(
                ctx=ctx, sender=sender, msg=msg, pdf_storage="local"
            )
        )

    mock_deliver.assert_not_awaited()
    assert ctx.send.await_count == 2
    already_msg_texts = [
        block.text
        for call in ctx.send.call_args_list
        for arg in call[0]
        if isinstance(arg, ChatMessage)
        for block in arg.content
        if isinstance(block, TextContent)
    ]
    assert any("already" in text for text in already_msg_texts)

    # Cleanup: remove the lock so other tests are not affected.
    sa._SESSION_LOCKS.pop(key, None)


def test_generation_lock_is_released_after_pdf_delivery():
    """Verify a completed delivery does not block a later Generate PDF click."""
    import scripts.spike_agent as sa

    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()
    sender = "agent1q_release_lock_test"
    draft_id = "dummy-draft-001"
    key = (sender, draft_id, "generate_pdf")
    sa._SESSION_LOCKS.pop(key, None)

    with patch("scripts.spike_agent.deliver_pdf", new_callable=AsyncMock) as mock_deliver:
        asyncio.run(
            handle_chat_message(
                ctx=ctx,
                sender=sender,
                msg=ChatMessage(content=[TextContent(text="generate_pdf")]),
                pdf_storage="local",
            )
        )
        assert key not in sa._SESSION_LOCKS

        asyncio.run(
            handle_chat_message(
                ctx=ctx,
                sender=sender,
                msg=ChatMessage(content=[TextContent(text="generate_pdf")]),
                pdf_storage="local",
            )
        )

    assert mock_deliver.await_count == 2
    assert key not in sa._SESSION_LOCKS


def test_agentverse_success_response_includes_resource_content(tmp_path: Path):
    """Verify successful Agentverse upload sends ResourceContent with agent-storage URI."""
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()

    pdf_bytes = render_dummy_pdf()
    mock_asset_id = str(uuid4())

    with patch("scripts.spike_agent.ExternalStorage") as mock_storage_cls:
        mock_instance = MagicMock()
        mock_instance.storage_url = "https://agentverse.ai/v1/storage"
        mock_instance.create_asset.return_value = mock_asset_id
        mock_storage_cls.return_value = mock_instance

        asyncio.run(
            deliver_pdf(
                ctx=ctx,
                sender="agent1q_recv",
                draft_id="rsc-draft-001",
                pdf_bytes=pdf_bytes,
                pdf_storage="agentverse",
                agentverse_api_key="mock-key",
                output_dir=tmp_path,
            )
        )

    ctx.send.assert_awaited_once()
    sent_msg = ctx.send.call_args[0][1]
    assert isinstance(sent_msg, ChatMessage)

    resource_blocks = [b for b in sent_msg.content if isinstance(b, ResourceContent)]
    assert len(resource_blocks) == 1, "Must include exactly one ResourceContent block"
    rc = resource_blocks[0]
    assert rc.resource.uri.startswith("agent-storage://"), (
        f"URI must use agent-storage scheme, got: {rc.resource.uri!r}"
    )
    assert rc.resource.metadata["mime_type"] == "application/pdf"
    assert str(rc.resource_id) == mock_asset_id


def test_plain_generate_pdf_text_reaches_deliver_pdf_once():
    """Regression: plain 'generate_pdf' TextContent must still trigger deliver_pdf once."""
    import scripts.spike_agent as sa

    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()

    sender = "agent1q_plain_regression"
    draft_id = "dummy-draft-001"
    key = (sender, draft_id, "generate_pdf")
    sa._SESSION_LOCKS.pop(key, None)  # ensure clean state

    msg = ChatMessage(content=[TextContent(text="generate_pdf")])

    with patch("scripts.spike_agent.deliver_pdf", new_callable=AsyncMock) as mock_deliver:
        asyncio.run(
            handle_chat_message(
                ctx=ctx,
                sender=sender,
                msg=msg,
                pdf_storage="local",
            )
        )
        mock_deliver.assert_awaited_once()
        assert mock_deliver.call_args[1]["draft_id"] == draft_id
        # No review card in reply
        assert not any(
            isinstance(arg, ChatMessage)
            and any(isinstance(b, MetadataContent) for b in arg.content)
            for call in ctx.send.call_args_list
            for arg in call[0]
        )

    sa._SESSION_LOCKS.pop(key, None)


def test_handle_chat_message_mention_prefixed_json_calls_deliver_pdf_once_no_card():
    """Verify @agent... {"action":"generate_pdf",...} calls deliver_pdf once and sends no review card."""
    import scripts.spike_agent as sa

    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()

    sender = "agent1q_json_handler_test"
    draft_id = "json-draft-001"
    key = (sender, draft_id, "generate_pdf")
    sa._SESSION_LOCKS.pop(key, None)

    agent_addr = "agent1q07xd588w2te2e7jj89240jxgxz66fdft09typ052egt4k5ngyhtkwh59ud"
    payload_text = f'@{agent_addr} {{"action":"generate_pdf","draft_id":"{draft_id}","primary":true}}'
    msg = ChatMessage(content=[TextContent(text=payload_text)])

    with patch("scripts.spike_agent.deliver_pdf", new_callable=AsyncMock) as mock_deliver:
        asyncio.run(
            handle_chat_message(
                ctx=ctx,
                sender=sender,
                msg=msg,
                pdf_storage="local",
            )
        )
        mock_deliver.assert_awaited_once()
        assert mock_deliver.call_args[1]["draft_id"] == draft_id
        # No review card in reply
        assert not any(
            isinstance(arg, ChatMessage)
            and any(isinstance(b, MetadataContent) for b in arg.content)
            for call in ctx.send.call_args_list
            for arg in call[0]
        )

    sa._SESSION_LOCKS.pop(key, None)


def test_handle_chat_message_invalid_json_like_sends_error_no_card():
    """Verify invalid or unsupported JSON-like text returns one concise error and never sends a review card."""
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()

    sender = "agent1q_bad_json_test"
    agent_addr = "agent1q07xd588w2te2e7jj89240jxgxz66fdft09typ052egt4k5ngyhtkwh59ud"
    # Unsupported action in JSON
    payload_text = f'@{agent_addr} {{"action":"unknown_action","draft_id":"dummy-001"}}'
    msg = ChatMessage(content=[TextContent(text=payload_text)])

    with patch("scripts.spike_agent.deliver_pdf", new_callable=AsyncMock) as mock_deliver:
        asyncio.run(
            handle_chat_message(
                ctx=ctx,
                sender=sender,
                msg=msg,
                pdf_storage="local",
            )
        )
        # deliver_pdf must NOT be called
        mock_deliver.assert_not_called()
        # No review card in reply
        assert not any(
            isinstance(arg, ChatMessage)
            and any(isinstance(b, MetadataContent) for b in arg.content)
            for call in ctx.send.call_args_list
            for arg in call[0]
        )
        # Sent exactly an ack + error reply
        assert ctx.send.await_count == 2
        sent_err = ctx.send.call_args_list[1][0][1]
        assert isinstance(sent_err, ChatMessage)
        assert "invalid or unsupported action" in sent_err.content[0].text


def test_handle_chat_message_duplicate_msg_id_is_ignored():
    """Verify repeated msg_id does not trigger duplicate delivery or duplicate replies."""
    import scripts.spike_agent as sa

    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send = AsyncMock()

    sender = "agent1q_dup_msg_test"
    fixed_id = uuid4()
    msg = ChatMessage(msg_id=fixed_id, content=[TextContent(text="generate_pdf")])

    sa._SEEN_MSG_IDS.pop(str(fixed_id), None)
    key = (sender, "dummy-draft-001", "generate_pdf")
    sa._SESSION_LOCKS.pop(key, None)

    with patch("scripts.spike_agent.deliver_pdf", new_callable=AsyncMock) as mock_deliver:
        # First call: process normally
        asyncio.run(handle_chat_message(ctx=ctx, sender=sender, msg=msg, pdf_storage="local"))
        assert mock_deliver.await_count == 1

        # Second call with the exact same msg_id: should acknowledge and return immediately
        ctx.send.reset_mock()
        asyncio.run(handle_chat_message(ctx=ctx, sender=sender, msg=msg, pdf_storage="local"))
        # deliver_pdf not called again
        assert mock_deliver.await_count == 1
        # Only ChatAcknowledgement was sent, no other messages
        assert ctx.send.await_count == 1
        sent_ack = ctx.send.call_args[0][1]
        assert isinstance(sent_ack, ChatAcknowledgement)

    sa._SEEN_MSG_IDS.pop(str(fixed_id), None)
    sa._SESSION_LOCKS.pop(key, None)
