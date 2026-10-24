"""Spike agent to validate ASI:One interactive cards and PDF delivery.

This is a time-boxed spike script (not the final agent) to prove:
1. Chat message acknowledgement (ChatAcknowledgement)
2. Interactive ReviewCardPayload delivery (with draft text in TextContent fallback)
3. Action response parsing and sanitized logging
4. PDF generation with fpdf2 and delivery via ResourceContent (or local file fallback)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fpdf import FPDF
from uagents import Agent, Context, Protocol
from uagents_core.config import AgentverseConfig
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    MetadataContent,
    Resource,
    ResourceContent,
    TextContent,
    chat_protocol_spec,
)
from uagents_core.contrib.protocols.chat.cards import (
    CtaAction,
    ReviewCardPayload,
    ReviewSummaryRow,
    create_card_content,
    extract_card_response,
)
from uagents_core.storage import ExternalStorage

# Configure safe logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("spike_agent")

BACKEND_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BACKEND_DIR / "output"

DUMMY_LETTER_TEXT = (
    "Dear Hiring Manager,\n\n"
    "I am writing to express my enthusiastic interest in the Software Engineering Intern position. "
    "With a solid grounding in Python, async systems, and automated testing, I am eager to contribute "
    "to your team while continuing to build robust software.\n\n"
    "Through my academic and open-source project work, I have designed resilient services, authored comprehensive "
    "test suites, and collaborated across development workflows. I would welcome the opportunity to discuss "
    "how my technical foundation aligns with your team's objectives.\n\n"
    "Sincerely,\nAlex Example"
)

# ---------------------------------------------------------------------------
# In-memory generation lock.
# Key: (sender_address, draft_id, action)  Value: monotonic timestamp when work
# started. A key stays present only while generation is active; a stale key is
# pruned after _IDEMPOTENCY_WINDOW_S in case a process exits unexpectedly.
# ---------------------------------------------------------------------------
_SESSION_LOCKS: dict[tuple[str, str, str], float] = {}
_IDEMPOTENCY_WINDOW_S: float = 120.0


def make_remote_asset_name(draft_id: str) -> str:
    """Return a unique, safe Agentverse asset filename for a PDF delivery.

    Combines a fixed prefix, the sanitized draft ID (max 32 chars, alnum/hyphen
    only), a UTC timestamp, and a uuid4 hex so the same draft can be uploaded
    again without triggering the 409 'asset already exists' error.
    """
    safe_id = re.sub(r"[^a-zA-Z0-9-]", "-", draft_id)[:32]
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    uid = uuid4().hex[:8]
    return f"cover_letter_{safe_id}_{ts}_{uid}.pdf"


def _is_duplicate_request(sender: str, draft_id: str, action: str) -> bool:
    """Return True if an identical request is already in progress.

    Records the (sender, draft_id, action) tuple on the first call and returns
    False. Subsequent calls return True until the generation handler releases
    the lock. Expired entries are pruned as crash recovery.
    """
    import time

    now = time.monotonic()
    key = (sender, draft_id, action)

    # Prune expired entries.
    expired = [k for k, ts in _SESSION_LOCKS.items() if now - ts > _IDEMPOTENCY_WINDOW_S]
    for k in expired:
        del _SESSION_LOCKS[k]

    if key in _SESSION_LOCKS:
        return True

    _SESSION_LOCKS[key] = now
    return False


_SEEN_MSG_IDS: dict[str, float] = {}


def _is_duplicate_msg_id(msg_id: str) -> bool:
    """Return True if msg_id has already been processed within _IDEMPOTENCY_WINDOW_S."""
    import time

    now = time.monotonic()
    expired = [k for k, ts in _SEEN_MSG_IDS.items() if now - ts > _IDEMPOTENCY_WINDOW_S]
    for k in expired:
        del _SEEN_MSG_IDS[k]

    if msg_id in _SEEN_MSG_IDS:
        return True

    _SEEN_MSG_IDS[msg_id] = now
    return False



def render_dummy_pdf(letter_text: str = DUMMY_LETTER_TEXT) -> bytes:
    """Render dummy cover letter bytes using fpdf2."""
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    # 0 width spans entire line width
    pdf.multi_cell(0, 8, letter_text)
    return bytes(pdf.output())


def build_review_card(draft_id: str = "dummy-draft-001") -> MetadataContent:
    """Build ReviewCardPayload as MetadataContent with Generate PDF action."""
    card = ReviewCardPayload(
        title="Cover Letter Review",
        summary_rows=[
            ReviewSummaryRow(label="Applicant", value="Alex Example"),
            ReviewSummaryRow(label="Position", value="Software Engineering Intern"),
            ReviewSummaryRow(label="Status", value="Draft Ready"),
        ],
        approve_cta=CtaAction(
            label="Generate PDF",
            selection={"action": "generate_pdf", "draft_id": draft_id},
            primary=True,
        ),
        reject_cta=CtaAction(
            label="Revise in Chat",
            selection={"action": "revise", "draft_id": draft_id},
        ),
    )
    return create_card_content(card, card_id=uuid4())


def sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Sanitize inbound payload to ensure no sensitive credentials or keys are logged."""
    safe_keys = {"action", "draft_id", "card_id", "cancelled", "text", "kind"}
    sanitized: dict[str, Any] = {}
    for key, value in payload.items():
        if key in safe_keys:
            sanitized[key] = value
        else:
            sanitized[key] = "<redacted>"
    return sanitized


_ZERO_WIDTH_CHARACTERS = "\u200b\u200c\u200d\ufeff\u2060"
_GENERATE_PDF_TEXT_CTAS = {
    "generate pdf",
    "generate_pdf",
    "generatepdf",
    "create pdf",
    "createpdf",
    "download pdf",
    "downloadpdf",
}
_REVISE_TEXT_CTAS = {"revise", "revise in chat", "revise_in_chat", "reviseinchat"}

# Pattern matching a leading ASI:One agent mention: @<word-chars-and-hyphens>,
# optional colon, and trailing whitespace.
# Covers both agent addresses (agent1q...) and display names (@InternAssist).
_MENTION_RE = re.compile(r"^@[\w-]+:?\s*")


def _normalize_cta_text(text: str) -> str:
    """Return the Unicode-normalized text representation used for CTA matching."""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.translate(str.maketrans("", "", _ZERO_WIDTH_CHARACTERS))
    return re.sub(r"\s+", " ", normalized.strip().casefold())


def _strip_agent_mention(text: str) -> str:
    """Remove a single leading @mention token from a string."""
    match = _MENTION_RE.match(text)
    if match:
        return text[match.end():].lstrip()
    return text


def classify_text_cta(text: str) -> str | None:
    """Return the canonical action name if *text* is an exact plain-text CTA, else ``None``."""
    normalized = _normalize_cta_text(text)
    candidate = _strip_agent_mention(normalized)
    compact = re.sub(r"[^a-z0-9]+", "", candidate)

    if candidate in _GENERATE_PDF_TEXT_CTAS or compact in {
        "generatepdf",
        "createpdf",
        "downloadpdf",
    }:
        return "generate_pdf"
    if candidate in _REVISE_TEXT_CTAS or compact in {"revise", "reviseinchat"}:
        return "revise"
    return None


def is_json_like_text(text: str) -> bool:
    """Return True if text begins with a JSON object (after optional @mention and code fences)."""
    raw = text.strip()
    candidate = _strip_agent_mention(raw).strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
    return candidate.startswith("{")


def parse_canonical_text_action(text: str) -> dict[str, Any] | None:
    """Single canonical parser for text action payloads.

    1. Strips leading @mention (e.g. @agent1q... or @InternAssist).
    2. Parses complete JSON action object after mention (e.g. {"action": "generate_pdf", ...}).
    3. Supports plain text CTA commands (generate_pdf, generate pdf, revise, ...).
    Returns dict with at least 'action' and 'draft_id' on success, or None.
    Both log_inbound_diagnostics and parse_action_selection use this function.
    """
    if not text:
        return None

    raw = text.strip()
    candidate = _strip_agent_mention(raw).strip()

    # Handle optional markdown code fence
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate).strip()

    # Check for JSON object
    if candidate.startswith("{"):
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                data = json.loads(candidate[start : end + 1])
                if isinstance(data, dict):
                    action = data.get("action")
                    if action in ("generate_pdf", "revise"):
                        res = dict(data)
                        res["action"] = action
                        res.setdefault("draft_id", "dummy-draft-001")
                        return res
            except Exception:
                pass

    # Check for plain-text CTA command
    cta_action = classify_text_cta(candidate)
    if cta_action:
        return {"action": cta_action, "draft_id": "dummy-draft-001"}

    return None


def log_inbound_diagnostics(ctx: Context, msg: ChatMessage) -> None:
    """Log safe diagnostic details for inbound ChatMessage blocks without leaking sensitive data."""
    ctx.logger.info(
        f"Inbound ChatMessage msg_id={msg.msg_id} (blocks={len(msg.content)})"
    )
    for idx, block in enumerate(msg.content):
        block_cls = type(block).__name__
        block_type = getattr(block, "type", "<none>")
        if isinstance(block, TextContent):
            code_points = [f"U+{ord(char):04X}" for char in block.text[:80]]
            action_info = parse_canonical_text_action(block.text)
            action_category = action_info["action"] if action_info else "none"
            ctx.logger.info(
                f"  [Block {idx}] class={block_cls}, type='{block_type}', "
                f"text_length={len(block.text)}, code_points={code_points}, "
                f"normalized_action={action_category}"
            )
        elif isinstance(block, MetadataContent):
            meta_keys = sorted(list(block.metadata.keys()))
            has_selection = "selection" in block.metadata
            ctx.logger.info(
                f"  [Block {idx}] class={block_cls}, type='{block_type}', "
                f"metadata_keys={meta_keys}, has_selection={has_selection}"
            )
        else:
            ctx.logger.info(
                f"  [Block {idx}] class={block_cls}, type='{block_type}'"
            )


def parse_action_selection(msg: ChatMessage) -> dict[str, Any] | None:
    """Extract action selection from MetadataContent card response or TextContent fallback."""
    # 1. Standard MetadataContent parsed with extract_card_response()
    for block in msg.content:
        if isinstance(block, MetadataContent):
            card_resp = extract_card_response(block)
            if card_resp and card_resp.selection and isinstance(card_resp.selection, dict):
                action = card_resp.selection.get("action")
                if action and isinstance(action, str):
                    return dict(card_resp.selection)

    # 2. MetadataContent direct selection or action key
    for block in msg.content:
        if isinstance(block, MetadataContent):
            raw_selection = block.metadata.get("selection")
            if raw_selection:
                parsed_sel = None
                if isinstance(raw_selection, str):
                    try:
                        parsed_sel = json.loads(raw_selection)
                    except Exception:
                        parsed_sel = None
                elif isinstance(raw_selection, dict):
                    parsed_sel = raw_selection
                if isinstance(parsed_sel, dict):
                    action = parsed_sel.get("action")
                    if action and isinstance(action, str):
                        return dict(parsed_sel)
            action_direct = block.metadata.get("action")
            if action_direct and isinstance(action_direct, str):
                return {
                    "action": action_direct,
                    "draft_id": block.metadata.get("draft_id", "dummy-draft-001"),
                }

    # 3. Canonical TextContent action parsing (JSON action or plain-text CTA)
    for block in msg.content:
        if isinstance(block, TextContent):
            action_info = parse_canonical_text_action(block.text)
            if action_info:
                return action_info

    return None



async def deliver_pdf(
    ctx: Context,
    sender: str,
    draft_id: str,
    pdf_bytes: bytes,
    pdf_storage: str,
    agentverse_api_key: str | None,
    output_dir: Path,
) -> None:
    """Deliver PDF via Agentverse ResourceContent or local file fallback."""
    if pdf_storage == "agentverse" and agentverse_api_key:
        try:
            ctx.logger.info("Attempting Agentverse ExternalStorage PDF upload...")
            storage = ExternalStorage(
                api_token=agentverse_api_key,
                storage_url=AgentverseConfig().storage_api,
            )
            remote_name = make_remote_asset_name(draft_id)
            ctx.logger.info(f"Uploading as remote asset name: {remote_name}")
            # Synchronous requests must run in executor
            asset_id_str = await asyncio.to_thread(
                storage.create_asset,
                name=remote_name,
                content=pdf_bytes,
                mime_type="application/pdf",
                lifetime_hours=24,
            )
            await asyncio.to_thread(
                storage.set_permissions,
                asset_id=asset_id_str,
                agent_address=sender,
                read=True,
                write=False,
            )
            asset_uri = f"agent-storage://{storage.storage_url}/{asset_id_str}"
            ctx.logger.info(f"Agentverse asset created: {asset_id_str}")

            try:
                res_uuid = UUID(asset_id_str)
                if res_uuid.version != 4:
                    res_uuid = uuid4()
            except Exception:
                res_uuid = uuid4()

            await ctx.send(
                sender,
                ChatMessage(
                    timestamp=datetime.now(timezone.utc),
                    msg_id=uuid4(),
                    content=[
                        TextContent(text="Your cover letter PDF has been generated. You can download it below:"),
                        ResourceContent(
                            type="resource",
                            resource_id=res_uuid,
                            resource=Resource(
                                uri=asset_uri,
                                metadata={
                                    "mime_type": "application/pdf",
                                    "role": "document",
                                    "name": "cover_letter.pdf",
                                    "filename": "cover_letter.pdf",
                                },
                            ),
                        ),
                        EndSessionContent(type="end-session"),
                    ],
                ),
            )
            return

        except Exception as exc:
            ctx.logger.warning(
                f"Agentverse storage delivery failed ({exc}); falling back to local storage"
            )

    # Local fallback — used for genuine storage failures or when pdf_storage != "agentverse".
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^a-zA-Z0-9-]", "-", draft_id)[:32]
    filename = f"cover_letter_{safe_id}.pdf"
    file_path = output_dir / filename
    await asyncio.to_thread(file_path.write_bytes, pdf_bytes)
    ctx.logger.info(f"Saved PDF locally to {file_path}")

    await ctx.send(
        sender,
        ChatMessage(
            timestamp=datetime.now(timezone.utc),
            msg_id=uuid4(),
            content=[
                TextContent(
                    text=(
                        f"Cover letter PDF generated and saved locally to {file_path} "
                        "(local file fallback, not a chat attachment)."
                    )
                ),
                EndSessionContent(type="end-session"),
            ],
        ),
    )


async def handle_chat_message(
    ctx: Context,
    sender: str,
    msg: ChatMessage,
    pdf_storage: str = "local",
    agentverse_api_key: str | None = None,
    output_dir: Path = OUTPUT_DIR,
) -> None:
    """Handle an inbound ChatMessage: log diagnostics, acknowledge, and route."""
    ctx.logger.info(f"Received ChatMessage from {sender} (msg_id: {msg.msg_id})")

    # Safe diagnostic logging of inbound blocks (never logs values/secrets)
    log_inbound_diagnostics(ctx, msg)

    # 1. Acknowledge any incoming ChatMessage
    ack = ChatAcknowledgement(
        acknowledged_msg_id=msg.msg_id,
        timestamp=datetime.now(timezone.utc),
    )
    await ctx.send(sender, ack)

    # Deduplication guard for repeated message IDs
    if _is_duplicate_msg_id(str(msg.msg_id)):
        ctx.logger.info(f"Duplicate msg_id={msg.msg_id}; ignoring.")
        return

    # Check for card action selection
    selection = parse_action_selection(msg)

    if selection and selection.get("action") == "generate_pdf":
        draft_id = str(selection.get("draft_id", "dummy-draft-001"))

        # Suppress only a concurrent generate_pdf for the same sender and draft.
        if _is_duplicate_request(sender, draft_id, "generate_pdf"):
            ctx.logger.info(
                f"Duplicate generate_pdf for draft_id={draft_id!r} from sender "
                f"(already in progress). Sending concise reply."
            )
            await ctx.send(
                sender,
                ChatMessage(
                    timestamp=datetime.now(timezone.utc),
                    msg_id=uuid4(),
                    content=[
                        TextContent(
                            text="Your PDF is already being generated. Please wait a moment."
                        )
                    ],
                ),
            )
            return

        # Log sanitized payload (no secrets) and generate PDF
        sanitized = sanitize_payload(selection)
        ctx.logger.info(f"Card action received: {sanitized}")

        generation_key = (sender, draft_id, "generate_pdf")
        try:
            pdf_bytes = render_dummy_pdf(DUMMY_LETTER_TEXT)
            await deliver_pdf(
                ctx=ctx,
                sender=sender,
                draft_id=draft_id,
                pdf_bytes=pdf_bytes,
                pdf_storage=pdf_storage,
                agentverse_api_key=agentverse_api_key,
                output_dir=output_dir,
            )
        finally:
            _SESSION_LOCKS.pop(generation_key, None)
        return  # Never fall through to review card after PDF delivery

    elif selection and selection.get("action") == "revise":
        sanitized = sanitize_payload(selection)
        ctx.logger.info(f"Revise action received: {sanitized}")
        await ctx.send(
            sender,
            ChatMessage(
                timestamp=datetime.now(timezone.utc),
                msg_id=uuid4(),
                content=[
                    TextContent(
                        text="Please type your suggested revisions directly in this chat."
                    )
                ],
            ),
        )
        return

    # If text is JSON-like but was invalid or unsupported, send one concise error (never review card)
    text_blocks = [block for block in msg.content if isinstance(block, TextContent)]
    if any(is_json_like_text(b.text) for b in text_blocks):
        ctx.logger.warning("Received invalid or unsupported JSON action payload; sending error reply.")
        await ctx.send(
            sender,
            ChatMessage(
                timestamp=datetime.now(timezone.utc),
                msg_id=uuid4(),
                content=[
                    TextContent(
                        text=(
                            "I received an invalid or unsupported action payload. "
                            "Please click 'Generate PDF' or type 'generate_pdf'."
                        )
                    )
                ],
            ),
        )
        return

    if len(text_blocks) == 1 and 1 <= len(_normalize_cta_text(text_blocks[0].text)) <= 80:
        # Temporary spike fallback for ASI:One clients that send an unrecognized
        # bare CTA label. Do not resend the card or generate a PDF in this branch.
        await ctx.send(
            sender,
            ChatMessage(
                timestamp=datetime.now(timezone.utc),
                msg_id=uuid4(),
                content=[
                    TextContent(
                        text=(
                            "I received an unsupported card action. Please type "
                            "`generate_pdf` to continue."
                        )
                    )
                ],
            ),
        )
        return

    # 2. Reply with dummy letter draft in TextContent and Review card
    ctx.logger.info("Replying with draft cover letter and review card")
    card_content = build_review_card(draft_id="dummy-draft-001")
    intro_text = (
        "Here is your drafted cover letter for review:\n\n"
        f"{DUMMY_LETTER_TEXT}\n\n"
        "Review the draft above. To request changes, reply in chat. "
        "When ready, click 'Generate PDF' below."
    )
    await ctx.send(
        sender,
        ChatMessage(
            timestamp=datetime.now(timezone.utc),
            msg_id=uuid4(),
            content=[
                TextContent(text=intro_text),
                card_content,
            ],
        ),
    )



def create_spike_agent(
    name: str = "internassist_spike",
    seed: str | None = None,
    port: int = 8000,
    mailbox: bool = False,
    pdf_storage: str = "local",
    agentverse_api_key: str | None = None,
    output_dir: Path = OUTPUT_DIR,
) -> Agent:
    """Create and configure the spike uAgent with AgentChatProtocol."""
    agent = Agent(
        name=name,
        seed=seed,
        port=port,
        mailbox=mailbox,
    )

    chat_proto = Protocol(spec=chat_protocol_spec)

    @chat_proto.on_message(ChatMessage)
    async def on_chat_message(ctx: Context, sender: str, msg: ChatMessage):
        await handle_chat_message(
            ctx=ctx,
            sender=sender,
            msg=msg,
            pdf_storage=pdf_storage,
            agentverse_api_key=agentverse_api_key,
            output_dir=output_dir,
        )

    @chat_proto.on_message(ChatAcknowledgement)
    async def on_acknowledgement(ctx: Context, sender: str, msg: ChatAcknowledgement):
        ctx.logger.debug(
            f"Received acknowledgement for msg_id: {msg.acknowledged_msg_id}"
        )

    agent.include(chat_proto, publish_manifest=True)
    return agent


def main() -> None:
    """Entry point for running the spike agent locally."""
    # Attempt to read backend/.env if present
    env_file = BACKEND_DIR / ".env"
    agentverse_key = None
    pdf_storage = "local"
    seed = None
    port = 8000
    name = "internassist_spike"

    if env_file.is_file():
        try:
            from app.config import load_settings

            settings = load_settings(env_file)
            name = settings.agent_name
            seed = settings.agent_seed or None
            port = settings.agent_port
            pdf_storage = settings.pdf_storage
            agentverse_key = settings.agentverse_api_key
            logger.info("Loaded settings from backend/.env")
        except Exception as exc:
            logger.warning(
                f"Could not load backend/.env ({exc}); using spike defaults"
            )

    mailbox_mode = os.getenv("USE_MAILBOX", "false").lower() in ("true", "1", "yes")

    agent = create_spike_agent(
        name=name,
        seed=seed,
        port=port,
        mailbox=mailbox_mode,
        pdf_storage=pdf_storage,
        agentverse_api_key=agentverse_key,
        output_dir=OUTPUT_DIR,
    )
    logger.info(
        f"Starting spike agent '{agent.name}' (address: {agent.address}, mailbox={mailbox_mode})"
    )
    agent.run()


if __name__ == "__main__":
    main()
