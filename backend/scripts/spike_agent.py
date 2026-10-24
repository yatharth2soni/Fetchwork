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
import urllib.parse
from dataclasses import dataclass
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
from uagents_core.types import DeliveryStatus

# Configure safe logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("spike_agent")

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.cover_letter import (
    CoverLetterDraft,
    CoverLetterError,
    MAX_RESUME_CHARS,
    generate_cover_letter,
)
from app.data.resume import load_resume
from app.job_fetcher import JobFetchError, fetch_job_posting
from app.models import Resume

OUTPUT_DIR = BACKEND_DIR / "output"
OUTBOUND_SEND_ATTEMPTS = 3
OUTBOUND_RETRY_DELAYS_SECONDS = (1, 2)

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
# Session-only draft state (Phase 5).
# Key: sender_address (str) -> SessionDraft
# In-memory only; all session drafts are lost on agent restart (no persistence).
# Store size is bounded by MAX_SESSIONS with oldest-first eviction.
# ---------------------------------------------------------------------------
MAX_SESSIONS: int = 1000
MAX_RESUME_SESSIONS: int = 1000
MAX_NAME_CHARS: int = 100
MIN_RESUME_EXTRA_NONWS_CHARS: int = 40
MAX_RAW_MESSAGE_CHARS: int = MAX_RESUME_CHARS + 500

RESUME_FORMAT_INSTRUCTIONS = (
    "Please send your resume in this format:\n"
    "resume:\n"
    "Name: Your Name\n"
    "Skills: ...\n"
    "Experience:\n"
    "- ..."
)
RESUME_OVERSIZED_INSTRUCTIONS = (
    "Your resume is too long. Please shorten it and try again.\n\n"
    "Please send your resume in this format:\n"
    "resume:\n"
    "Name: Your Name\n"
    "Skills: ...\n"
    "Experience:\n"
    "- ..."
)

# Phase 8: inbound ResourceContent is detectable, but PDF *bytes* are not
# available via a verified protocol path (see docs/ASI_INTEGRATION_NOTES.md).
PDF_UPLOAD_UNSUPPORTED_MESSAGE = (
    "PDF upload is not available in this ASI:One agent channel yet. "
    "Please paste your resume beginning with `resume:`."
)


def message_contains_resource_content(msg: ChatMessage) -> bool:
    """True if the inbound ChatMessage includes one or more ResourceContent blocks."""
    return any(isinstance(block, ResourceContent) for block in msg.content)


def count_resource_content_blocks(msg: ChatMessage) -> int:
    """Count ResourceContent blocks without reading URIs or metadata values."""
    return sum(1 for block in msg.content if isinstance(block, ResourceContent))


class ResumeOversizedError(ValueError):
    """Raised when submitted resume exceeds supported size limits."""


class ResumeParseError(ValueError):
    """Raised when submitted resume fails structural or content validation."""


# ---------------------------------------------------------------------------
# In-memory Resume Store (Phase 7).
# Sessions are stored strictly in process memory and are lost on agent restart
# (no persistence, no database, no file storage).
# Key: sender_address (str) -> Resume
# ---------------------------------------------------------------------------
_RESUME_STORE: dict[str, Resume] = {}
_RESUME_VERSIONS: dict[str, int] = {}
_RESUME_TIMESTAMPS: dict[str, float] = {}


def _clear_session(sender: str) -> None:
    """Clear a sender's stored resume, active draft, and bump resume version.

    Sessions are stored strictly in process memory and are lost on agent restart (no persistence).
    """
    _RESUME_STORE.pop(sender, None)
    _RESUME_TIMESTAMPS.pop(sender, None)
    _ACTIVE_DRAFTS.pop(sender, None)
    _RESUME_VERSIONS[sender] = _RESUME_VERSIONS.get(sender, 0) + 1


def get_stored_resume(sender: str) -> Resume | None:
    """Retrieve the in-memory resume for a sender, or None."""
    return _RESUME_STORE.get(sender)


def get_resume_version(sender: str) -> int:
    """Retrieve the current resume version counter for a sender."""
    return _RESUME_VERSIONS.get(sender, 0)


def store_user_resume(sender: str, resume: Resume) -> None:
    """Store or replace the in-memory Resume for a sender.

    Evicts the oldest session (resume + draft) if capacity is reached.
    Clears that sender's old active draft and increments the resume version.
    """
    import time

    now = time.monotonic()
    if len(_RESUME_STORE) >= MAX_RESUME_SESSIONS and sender not in _RESUME_STORE:
        oldest_sender = min(
            _RESUME_STORE.keys(), key=lambda s: _RESUME_TIMESTAMPS.get(s, 0.0)
        )
        _clear_session(oldest_sender)

    _RESUME_STORE[sender] = resume
    _RESUME_TIMESTAMPS[sender] = now
    _ACTIVE_DRAFTS.pop(sender, None)
    _RESUME_VERSIONS[sender] = _RESUME_VERSIONS.get(sender, 0) + 1


def clear_all_resumes() -> None:
    """Clear all in-memory stored resumes, versions, and timestamps (test teardown helper)."""
    _RESUME_STORE.clear()
    _RESUME_VERSIONS.clear()
    _RESUME_TIMESTAMPS.clear()


def parse_resume_text(text: str) -> Resume:
    """Parse user-submitted resume text into an existing Resume model.

    The raw text is stored in the existing Resume.summary field so it reaches
    the Phase 4 prompt unaltered, with the applicant name in Resume.name.
    """
    if len(text) > MAX_RAW_MESSAGE_CHARS:
        raise ResumeOversizedError("Raw resume message exceeds size limit")

    if not text.startswith("resume:"):
        raise ResumeParseError("Resume text does not begin with lowercase 'resume:'")

    body = text[len("resume:"):]
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    body = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", body)

    if len(body) > MAX_RESUME_CHARS:
        raise ResumeOversizedError("Resume body exceeds MAX_RESUME_CHARS")

    lines = body.split("\n")
    name_val: str | None = None
    name_line_idx: int | None = None

    for idx, line in enumerate(lines):
        line_stripped = line.strip()
        if line_stripped.lower().startswith("name:"):
            val = line_stripped[5:].strip()
            if val:
                name_val = val
                name_line_idx = idx
                break

    if not name_val:
        raise ResumeParseError("Missing or empty Name line")

    if len(name_val) > MAX_NAME_CHARS:
        raise ResumeParseError(f"Name exceeds {MAX_NAME_CHARS} characters")

    other_lines = [l for i, l in enumerate(lines) if i != name_line_idx]
    has_further_line = any(l.strip() for l in other_lines)
    other_nonws = sum(len(re.sub(r"\s+", "", l)) for l in other_lines)

    if not has_further_line or other_nonws < MIN_RESUME_EXTRA_NONWS_CHARS:
        raise ResumeParseError("Insufficient substantive resume content")

    return Resume(name=name_val, summary=body.strip())


@dataclass
class SessionDraft:
    """In-memory active draft for a sender.

    NOTE: Sessions are stored strictly in-memory and are lost on agent restart.
    """

    draft_id: str
    letter_text: str
    applicant_name: str
    source_url: str
    created_at: float


_ACTIVE_DRAFTS: dict[str, SessionDraft] = {}


def get_session_draft(sender: str) -> SessionDraft | None:
    """Retrieve the active session draft for a sender, or None if no draft exists."""
    return _ACTIVE_DRAFTS.get(sender)


def store_session_draft(
    sender: str,
    letter_text: str,
    applicant_name: str,
    source_url: str,
    draft_id: str | None = None,
) -> SessionDraft:
    """Store or replace the active draft for a sender, evicting oldest if capacity reached."""
    import time

    now = time.monotonic()
    # Enforce capacity bound: evict oldest entry if at capacity
    if len(_ACTIVE_DRAFTS) >= MAX_SESSIONS and sender not in _ACTIVE_DRAFTS:
        oldest_sender = min(
            _ACTIVE_DRAFTS.keys(), key=lambda s: _ACTIVE_DRAFTS[s].created_at
        )
        _ACTIVE_DRAFTS.pop(oldest_sender, None)

    assigned_id = draft_id if draft_id is not None else uuid4().hex
    draft = SessionDraft(
        draft_id=assigned_id,
        letter_text=letter_text,
        applicant_name=applicant_name,
        source_url=source_url,
        created_at=now,
    )
    _ACTIVE_DRAFTS[sender] = draft
    return draft


def clear_session_drafts() -> None:
    """Clear all active session drafts and resumes (test teardown helper)."""
    _ACTIVE_DRAFTS.clear()
    clear_all_resumes()


# ---------------------------------------------------------------------------
# URL Extraction & Normalization
# ---------------------------------------------------------------------------
_URL_REGEX = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_TRAILING_PUNCTUATION = ".,;:!?)]>\"'”’"


def extract_candidate_urls(text: str) -> list[str]:
    """Extract candidate http/https URLs from text, stripping trailing punctuation.

    Deduplicates distinct URLs while preserving discovery order.
    Bare domains (e.g. 'example.com') are deliberately NOT matched.
    """
    if not text:
        return []
    matches = _URL_REGEX.findall(text)
    cleaned_urls: list[str] = []
    seen: set[str] = set()
    for raw in matches:
        cleaned = raw.rstrip(_TRAILING_PUNCTUATION)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            cleaned_urls.append(cleaned)
    return cleaned_urls


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


_UNICODE_CHAR_MAP = {
    "\u2018": "'",
    "\u2019": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u2013": "-",
    "\u2014": "-",
    "\u2026": "...",
    "\xa0": " ",
}


def render_cover_letter_pdf(
    letter_text: str,
    applicant_name: str | None = None,
    _keep_together: bool = True,
) -> bytes:
    """Render cover letter bytes using fpdf2 with safe character sanitization and unbreakable closing."""
    sanitized = letter_text
    for char, replacement in _UNICODE_CHAR_MAP.items():
        sanitized = sanitized.replace(char, replacement)

    # Core fonts (Helvetica) in fpdf2 require latin-1 encoding
    safe_text = sanitized.encode("latin-1", "replace").decode("latin-1")

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    line_height: float = 8.0

    if not _keep_together:
        # Legacy path for regression testing (vulnerable to page-boundary splitting)
        pdf.multi_cell(0, line_height, safe_text)
        return bytes(pdf.output())

    # Phase 6: Keep the closing ("Sincerely," + applicant name) together as ONE
    # unbreakable block to prevent orphaned sign-offs across page breaks.
    # Deterministic rule: the closing block starts at the second-to-last non-empty line.
    lines = safe_text.rstrip().split("\n")
    non_empty_indices = [i for i, line in enumerate(lines) if line.strip()]

    if len(non_empty_indices) >= 2:
        split_idx = non_empty_indices[-2]
        body_text = "\n".join(lines[:split_idx]).rstrip()
        closing_text = "\n".join(lines[split_idx:]).strip()
    else:
        body_text = ""
        closing_text = safe_text.strip()

    if body_text:
        pdf.multi_cell(0, line_height, body_text)
        # Compute closing block height: 1 blank line gap (line_height) + closing lines
        closing_lines = closing_text.splitlines()
        closing_height = (len(closing_lines) + 1) * line_height
        if pdf.will_page_break(closing_height):
            pdf.add_page()
        else:
            pdf.ln(line_height)

    with pdf.unbreakable() as doc:
        doc.multi_cell(0, line_height, closing_text)

    return bytes(pdf.output())


def render_dummy_pdf(letter_text: str = DUMMY_LETTER_TEXT) -> bytes:
    """Render dummy cover letter bytes using fpdf2 (Phase 2 compatibility wrapper)."""
    return render_cover_letter_pdf(letter_text=letter_text)


MAX_CARD_FIELD_CHARS: int = 100


def _clean_card_text(text: str, max_len: int = MAX_CARD_FIELD_CHARS) -> str:
    """Strip control characters and truncate text for safe card rendering."""
    cleaned = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_len]


def build_review_card(
    draft_id: str = "dummy-draft-001",
    applicant_name: str = "Alex Example",
    position_title: str = "Tailored cover letter",
    status: str = "Draft Ready",
) -> MetadataContent:
    """Build ReviewCardPayload as MetadataContent with Generate PDF action."""
    safe_name = _clean_card_text(applicant_name) or "Applicant"
    safe_title = _clean_card_text(position_title) or "Tailored cover letter"
    safe_status = _clean_card_text(status) or "Draft Ready"

    card = ReviewCardPayload(
        title="Cover Letter Review",
        summary_rows=[
            ReviewSummaryRow(label="Applicant", value=safe_name),
            ReviewSummaryRow(label="Position", value=safe_title),
            ReviewSummaryRow(label="Status", value=safe_status),
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
    """Remove consecutive leading @mention tokens from a string.

    ASI:One can include both the agent address and its display name when a
    user selects the agent from the chat UI.  Treat those routing markers as
    transport metadata so the user payload begins with its actual content.
    """
    candidate = text
    while match := _MENTION_RE.match(candidate):
        candidate = candidate[match.end():].lstrip()
    return candidate


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


def is_unsupported_cta_text(text: str) -> bool:
    """Return True if text appears to be an unrecognized card action or control button."""
    normalized = _normalize_cta_text(text)
    candidate = _strip_agent_mention(normalized)
    if candidate == "unexpected card control" or "card control" in candidate:
        return True
    if any(k in candidate for k in ("card action", "card_action", "card_control")):
        return True
    return False


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


async def send_with_retry(ctx: Context, destination: str, message: Any) -> Any:
    """Send an outbound chat message, retrying transient delivery failures.

    The uAgents Context API returns a failed ``MsgStatus`` for a webhook 504
    instead of raising. Retrying a bounded number of times lets the agent
    recover from a temporary ASI:One delivery timeout without blocking the
    event loop or exposing user content in logs.
    """
    last_result: Any = None
    for attempt in range(OUTBOUND_SEND_ATTEMPTS):
        try:
            last_result = await ctx.send(destination, message)
        except Exception as exc:
            ctx.logger.warning(
                "Outbound chat delivery raised %s (attempt %s/%s)",
                type(exc).__name__,
                attempt + 1,
                OUTBOUND_SEND_ATTEMPTS,
            )
        else:
            if getattr(last_result, "status", None) != DeliveryStatus.FAILED:
                return last_result
            ctx.logger.warning(
                "Outbound chat delivery failed (attempt %s/%s)",
                attempt + 1,
                OUTBOUND_SEND_ATTEMPTS,
            )

        if attempt < len(OUTBOUND_RETRY_DELAYS_SECONDS):
            await asyncio.sleep(OUTBOUND_RETRY_DELAYS_SECONDS[attempt])

    ctx.logger.error("Outbound chat delivery failed after bounded retries")
    return last_result


class _RetryingContext:
    """Delegate a Context while applying bounded retries to outbound sends."""

    def __init__(self, context: Context):
        self._context = context

    def __getattr__(self, name: str) -> Any:
        return getattr(self._context, name)

    async def send(self, destination: str, message: Any, **kwargs: Any) -> Any:
        if kwargs:
            return await self._context.send(destination, message, **kwargs)
        return await send_with_retry(self._context, destination, message)


def _with_delivery_retries(ctx: Context) -> Context:
    """Return one retrying wrapper for a handler context."""
    if isinstance(ctx, _RetryingContext):
        return ctx
    return _RetryingContext(ctx)  # type: ignore[return-value]


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
    ctx = _with_delivery_retries(ctx)
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

            # Keep a local copy even after a successful upload. Some ASI:One
            # clients do not render PDF ResourceContent, so this gives the
            # local operator a concrete, user-visible recovery path.
            output_dir.mkdir(parents=True, exist_ok=True)
            safe_id = re.sub(r"[^a-zA-Z0-9-]", "-", draft_id)[:32]
            filename = f"cover_letter_{safe_id}.pdf"
            file_path = output_dir / filename
            await asyncio.to_thread(file_path.write_bytes, pdf_bytes)
            ctx.logger.info(f"Saved local PDF mirror to {file_path}")

            # Fetch.ai's storage example sends ResourceContent as its own
            # message. Keep that wire shape for clients that render it.
            await ctx.send(
                sender,
                ChatMessage(
                    timestamp=datetime.now(timezone.utc),
                    msg_id=uuid4(),
                    content=[
                        ResourceContent(
                            type="resource",
                            resource_id=res_uuid,
                            resource=Resource(
                                uri=asset_uri,
                                metadata={
                                    "mime_type": "application/pdf",
                                    "role": "document",
                                },
                            ),
                        ),
                    ],
                ),
            )
            # Send a clear, actionable notification so the user understands that
            # the PDF was generated and knows how to access it on Windows, since
            # the ASI:One chat web client does not render or download PDF attachments.
            await ctx.send(
                sender,
                ChatMessage(
                    timestamp=datetime.now(timezone.utc),
                    msg_id=uuid4(),
                    content=[
                        TextContent(
                            text=(
                                "✅ **Cover Letter PDF Generated!**\n\n"
                                f"📁 **File Location:** `{file_path}`\n\n"
                                "ℹ️ **Why no in-browser download?** The ASI:One chat web client currently "
                                "only renders image attachments and does not download PDFs directly through the browser. "
                                "Your PDF is stored safely on your machine at the path above.\n\n"
                                "🚀 **How to open it now:**\n"
                                f"• In PowerShell: `Invoke-Item '{file_path}'`\n"
                                "• Or in File Explorer: navigate to `backend\\output`"
                            )
                        )
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
                        "✅ **Cover Letter PDF Generated!**\n\n"
                        f"📁 **File Location:** `{file_path}`\n\n"
                        "ℹ️ **Notice:** Saved locally to your project's output directory "
                        "(local file fallback, not a chat attachment).\n\n"
                        "🚀 **How to open it now:**\n"
                        f"• In PowerShell: `Invoke-Item '{file_path}'`\n"
                        "• Or in File Explorer: navigate to `backend\\output`"
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
    ctx = _with_delivery_retries(ctx)
    # Truncate sender address for privacy-safe logging
    sender_tag = f"{sender[:16]}..." if len(sender) > 16 else sender
    ctx.logger.info(f"Received ChatMessage from {sender_tag} (msg_id: {msg.msg_id})")

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

    # 2. Existing standard metadata/JSON action parsing runs FIRST
    selection = parse_action_selection(msg)

    # 3. Handle card actions
    if selection and selection.get("action") == "generate_pdf":
        active_draft = get_session_draft(sender)
        if active_draft is None:
            ctx.logger.info(
                f"Generate PDF requested by {sender_tag} but no active draft exists."
            )
            await ctx.send(
                sender,
                ChatMessage(
                    timestamp=datetime.now(timezone.utc),
                    msg_id=uuid4(),
                    content=[
                        TextContent(
                            text="I don't have an active draft for you. Please send a job-posting URL first."
                        )
                    ],
                ),
            )
            return

        action_draft_id = str(selection.get("draft_id", ""))
        # Check draft_id mismatch when provided (ignore legacy default dummy-draft-001 from plain CTA)
        if (
            action_draft_id
            and action_draft_id != "dummy-draft-001"
            and action_draft_id != active_draft.draft_id
        ):
            ctx.logger.warning(
                f"Draft ID mismatch for {sender_tag} (expected {active_draft.draft_id}, got {action_draft_id})"
            )
            await ctx.send(
                sender,
                ChatMessage(
                    timestamp=datetime.now(timezone.utc),
                    msg_id=uuid4(),
                    content=[
                        TextContent(
                            text="I don't have an active draft matching that request. Please send a job-posting URL to create a new draft."
                        )
                    ],
                ),
            )
            return

        draft_id = active_draft.draft_id
        # Suppress concurrent generate_pdf for the same sender and draft
        if _is_duplicate_request(sender, draft_id, "generate_pdf"):
            ctx.logger.info(
                f"Duplicate generate_pdf for draft_id={draft_id!r} from sender (already in progress)."
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

        sanitized = sanitize_payload(selection)
        ctx.logger.info(f"Card action received: {sanitized}")

        generation_key = (sender, draft_id, "generate_pdf")
        try:
            pdf_bytes = render_cover_letter_pdf(
                letter_text=active_draft.letter_text,
                applicant_name=active_draft.applicant_name,
            )
            await deliver_pdf(
                ctx=ctx,
                sender=sender,
                draft_id=draft_id,
                pdf_bytes=pdf_bytes,
                pdf_storage=pdf_storage,
                agentverse_api_key=agentverse_api_key,
                output_dir=output_dir,
            )
        except Exception as exc:
            ctx.logger.warning(f"PDF generation or delivery failed: {type(exc).__name__}")
            await ctx.send(
                sender,
                ChatMessage(
                    timestamp=datetime.now(timezone.utc),
                    msg_id=uuid4(),
                    content=[
                        TextContent(
                            text="Something went wrong generating your PDF. Please try again in a moment."
                        )
                    ],
                ),
            )
        finally:
            _SESSION_LOCKS.pop(generation_key, None)
        return

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
                        text=(
                            "Revising cover letters in chat is not yet supported. "
                            "Please send a new job-posting URL to generate an updated draft."
                        )
                    )
                ],
            ),
        )
        return

    elif selection:
        ctx.logger.warning(f"Received unsupported card action: {selection.get('action')}")
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

    # Check for invalid or unsupported JSON action payload
    text_blocks = [block for block in msg.content if isinstance(block, TextContent)]
    if any(is_json_like_text(b.text) for b in text_blocks):
        ctx.logger.warning(
            "Received invalid or unsupported JSON action payload; sending error reply."
        )
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

    # Check for unrecognized card control / CTA fallback
    if any(is_unsupported_cta_text(b.text) for b in text_blocks):
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

    raw_text = "\n".join(b.text for b in text_blocks)
    candidate_raw = _strip_agent_mention(raw_text.strip()).strip()

    # 4. clear_resume command (3.3)
    if candidate_raw.lower() == "clear_resume":
        _clear_session(sender)
        ctx.logger.info(f"Resume and active draft cleared for {sender_tag}")
        await ctx.send(
            sender,
            ChatMessage(
                timestamp=datetime.now(timezone.utc),
                msg_id=uuid4(),
                content=[
                    TextContent(
                        text="Your resume and active draft were cleared from this chat session."
                    )
                ],
            ),
        )
        return

    # 5. Resource / attachment intake (Phase 8)
    # Protocol supports ResourceContent (uri + metadata only). Official ASI:One
    # attachment docs forward HTTPS URLs and state Chat Interface storage is
    # opaque; Resource has no inline bytes field. Without a verified PDF-bytes
    # retrieval path, do not download arbitrary URIs or invent parsers.
    if message_contains_resource_content(msg):
        resource_count = count_resource_content_blocks(msg)
        ctx.logger.info(
            f"Inbound ResourceContent detected for {sender_tag} "
            f"(resource_blocks={resource_count}); PDF bytes path unsupported"
        )
        await ctx.send(
            sender,
            ChatMessage(
                timestamp=datetime.now(timezone.utc),
                msg_id=uuid4(),
                content=[TextContent(text=PDF_UPLOAD_UNSUPPORTED_MESSAGE)],
            ),
        )
        return

    # 6. resume: intake (Phase 7)
    if candidate_raw.startswith("resume:"):
        try:
            parsed_resume = parse_resume_text(candidate_raw)
        except ResumeOversizedError:
            ctx.logger.warning("Resume intake rejected: ResumeOversizedError")
            await ctx.send(
                sender,
                ChatMessage(
                    timestamp=datetime.now(timezone.utc),
                    msg_id=uuid4(),
                    content=[TextContent(text=RESUME_OVERSIZED_INSTRUCTIONS)],
                ),
            )
            return
        except Exception as exc:
            ctx.logger.warning(f"Resume intake rejected: {type(exc).__name__}")
            await ctx.send(
                sender,
                ChatMessage(
                    timestamp=datetime.now(timezone.utc),
                    msg_id=uuid4(),
                    content=[TextContent(text=RESUME_FORMAT_INSTRUCTIONS)],
                ),
            )
            return

        store_user_resume(sender, parsed_resume)
        ctx.logger.info(
            f"Resume stored for {sender_tag} (chars={len(parsed_resume.summary or '')})"
        )
        await ctx.send(
            sender,
            ChatMessage(
                timestamp=datetime.now(timezone.utc),
                msg_id=uuid4(),
                content=[
                    TextContent(
                        text="Your resume is saved for this chat only. Now send one public job-posting URL."
                    )
                ],
            ),
        )
        return

    # 7. Resume-presence gate (Phase 7)
    sender_resume = get_stored_resume(sender)
    if sender_resume is None:
        ctx.logger.info(f"Sender {sender_tag} has no stored resume; gated.")
        await ctx.send(
            sender,
            ChatMessage(
                timestamp=datetime.now(timezone.utc),
                msg_id=uuid4(),
                content=[
                    TextContent(
                        text="Please paste your resume first, beginning with `resume:`."
                    )
                ],
            ),
        )
        return

    # 8. Job URL handling (Phase 5/7)
    all_text = " ".join(b.text for b in text_blocks)
    candidate_urls = extract_candidate_urls(all_text)

    if len(candidate_urls) > 1:
        await ctx.send(
            sender,
            ChatMessage(
                timestamp=datetime.now(timezone.utc),
                msg_id=uuid4(),
                content=[
                    TextContent(
                        text="Please send only one job-posting URL at a time."
                    )
                ],
            ),
        )
        return

    if len(candidate_urls) == 0:
        await ctx.send(
            sender,
            ChatMessage(
                timestamp=datetime.now(timezone.utc),
                msg_id=uuid4(),
                content=[
                    TextContent(
                        text="Please send one public job-posting URL to create a tailored cover letter."
                    )
                ],
            ),
        )
        return

    # Exactly one candidate URL -> generation pipeline
    target_url = candidate_urls[0]
    parsed_target = urllib.parse.urlsplit(target_url)
    target_host = f"{parsed_target.scheme}://{parsed_target.hostname or '<unknown>'}"
    ctx.logger.info(f"Processing job posting URL from {target_host} for {sender_tag}")

    resume_version = get_resume_version(sender)

    try:
        job = await fetch_job_posting(target_url)
    except JobFetchError as exc:
        ctx.logger.warning(f"Job fetch error: {type(exc).__name__}")
        await ctx.send(
            sender,
            ChatMessage(
                timestamp=datetime.now(timezone.utc),
                msg_id=uuid4(),
                content=[TextContent(text=exc.user_message)],
            ),
        )
        return
    except Exception as exc:
        ctx.logger.warning(f"Unexpected job fetch failure: {type(exc).__name__}")
        await ctx.send(
            sender,
            ChatMessage(
                timestamp=datetime.now(timezone.utc),
                msg_id=uuid4(),
                content=[
                    TextContent(
                        text="Unable to fetch the job posting. Please paste the job description text directly."
                    )
                ],
            ),
        )
        return

    try:
        draft = await generate_cover_letter(job_posting=job, resume=sender_resume)
    except CoverLetterError as exc:
        ctx.logger.warning(f"Cover letter generation error: {type(exc).__name__}")
        await ctx.send(
            sender,
            ChatMessage(
                timestamp=datetime.now(timezone.utc),
                msg_id=uuid4(),
                content=[TextContent(text=exc.user_message)],
            ),
        )
        return
    except Exception as exc:
        ctx.logger.warning(f"Unexpected generator error: {type(exc).__name__}")
        await ctx.send(
            sender,
            ChatMessage(
                timestamp=datetime.now(timezone.utc),
                msg_id=uuid4(),
                content=[
                    TextContent(
                        text="Something went wrong creating your cover letter. Please try again."
                    )
                ],
            ),
        )
        return

    # Race guard (3.6): ensure sender's resume was not replaced or cleared during in-flight generation
    if get_resume_version(sender) != resume_version or get_stored_resume(sender) is not sender_resume:
        ctx.logger.info(f"Resume changed during generation for {sender_tag}; discarding draft.")
        await ctx.send(
            sender,
            ChatMessage(
                timestamp=datetime.now(timezone.utc),
                msg_id=uuid4(),
                content=[
                    TextContent(
                        text="Your resume changed while I was working. Please send the job URL again."
                    )
                ],
            ),
        )
        return

    # Store new active session draft (replaces previous draft for this sender)
    session_draft = store_session_draft(
        sender=sender,
        letter_text=draft.text,
        applicant_name=sender_resume.name,
        source_url=job.source_url,
    )
    ctx.logger.info(
        f"Cover letter draft stored for {sender_tag} (draft_id={session_draft.draft_id}, chars={len(draft.text)})"
    )

    # Send (1) generated letter as plain chat text
    # TODO: verify against docs if ASI:One specifies a maximum per-message character limit
    await ctx.send(
        sender,
        ChatMessage(
            timestamp=datetime.now(timezone.utc),
            msg_id=uuid4(),
            content=[TextContent(text=draft.text)],
        ),
    )

    # Send (2) exactly ONE review card
    card_content = build_review_card(
        draft_id=session_draft.draft_id,
        applicant_name=sender_resume.name,
        position_title="Tailored cover letter",
        status="Draft Ready",
    )
    await ctx.send(
        sender,
        ChatMessage(
            timestamp=datetime.now(timezone.utc),
            msg_id=uuid4(),
            content=[card_content],
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
