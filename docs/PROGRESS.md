# InternAssist — Progress Ledger

## Current prompt
Phase 2 risk spike: prove ASI:One interactive cards and PDF delivery before scraper / LLM / full flow.

## Done and verified
- Scaffold layout under `backend/app/`, `backend/tests/`, `backend/output/.gitkeep`, `backend/README.md`, `docs/ASI_INTEGRATION_NOTES.md`. Verified: files present.
- Virtualenv `.venv` packages pinned in `backend/requirements.txt` (Python 3.11.9, uagents 0.26.1, uagents-core 0.4.12, fpdf2 2.8.9, etc.). Verified: `pip show` versions match.
- **Phase 1 — config + resume privacy:** `backend/app/config.py` loads typed settings from `backend/.env`; safe logging; Alex Example fixture fallback. Verified: pytest 14 passed.
- **Phase 2 — Research & Integration Notes:** Researched questions A–E from official docs and installed package source (`.venv/Lib/site-packages/uagents_core/`). Recorded in [docs/ASI_INTEGRATION_NOTES.md](ASI_INTEGRATION_NOTES.md) with dates and citations.
- **Phase 2 — Schema verification on editable textarea:** Inspected `FormField` and `InputNode` in `uagents_core.contrib.protocols.chat.cards`. Both use `_StrictBase` (`extra="forbid"`), allow only `text`, `number`, `email`, `select`, `checkbox` (no `textarea` / `multiline`), and lack `value` or `default` fields. Confirmed prefilled editable textarea is **NOT supported**.
- **Phase 2 — Upstream bug identified in uagents-core:** `ExternalStorage.__init__` references `AgentverseConfig().storage_endpoint` which does not exist (`storage_api` exists); verified calling without explicit `storage_url` raises `AttributeError`. Verified explicit `storage_url=AgentverseConfig().storage_api` succeeds.
- **Phase 2 — Spike agent script:** Implemented [backend/scripts/spike_agent.py](../backend/scripts/spike_agent.py) with:
  1. Inbound chat message acknowledgement (`ChatAcknowledgement`).
  2. Draft delivery via `TextContent` + `ReviewCardPayload` with `Generate PDF` and `Revise in Chat` CTAs.
  3. Action extraction from `MetadataContent` (and JSON text fallback) with sanitized logging (no secrets).
- **Phase 2 — Unit tests:** Added [backend/tests/test_spike_agent.py](../backend/tests/test_spike_agent.py) covering PDF generation, card creation, sanitized payload filtering, action response parsing, local storage fallback, and mocked Agentverse delivery. Verified: `python -m pytest backend/tests -q` → **22 passed**.
- **Phase 2 — Live Card Action Loop Fix & Diagnostic Logging:** Added safe diagnostic logging of inbound `ChatMessage` blocks (classes, types, metadata keys only; no values or secrets). Updated `parse_action_selection()` to resolve card actions across standard `extract_card_response()`, direct metadata `selection`/`action` JSON parsing, and strict JSON `TextContent` fallback (`action`, `draft_id`, `card_id`). Factored `handle_chat_message()`. Verified `generate_pdf` triggers `deliver_pdf()` exactly once and ordinary text replies with Review Card without triggering PDF delivery. Test suite expanded: `python -m pytest backend/tests -q` → **27 passed**.
- **Phase 2 — Plain-text CTA fallback (confirmed ASI:One behavior):** Live evidence confirmed ASI:One sends a bare `TextContent` (not `MetadataContent`) when a card CTA button is clicked. Added path 4 to `parse_action_selection()`: normalize `TextContent.text` (trim + collapse whitespace + case-fold) and match against a fixed allow-list of CTA phrases (`"generate pdf"`, `"generate_pdf"`, `"revise in chat"`, `"revise"`). All other text produces `None`. Updated `docs/ASI_INTEGRATION_NOTES.md` §B.6 with verified behavior. Test suite: `python -m pytest backend/tests -q` → **29 passed**.
- **Phase 2 — ASI Review Card CTA compatibility spike:** Replaced whitespace-only matching with Unicode NFKC normalization, zero-width-character removal, compact separator-insensitive comparison, and a fixed CTA allow-list. Added privacy-safe `TextContent` diagnostics (length, first 80 Unicode code points, and action category only; never text). Added a temporary non-looping reply for an unrecognized one-block short CTA payload; it instructs the user to type `generate_pdf` without resending a card or generating a PDF. Verified synchronously: `27 passed, 5 deselected` (CTA parsing, diagnostics, card schema, config, and resume tests). A full-suite attempt previously reached 26 passing tests and then hung when Windows created an `asyncio` loop; five async tests could not complete in this execution environment.
- **Phase 2 — @mention prefix command extractor:** ASI:One prefixes direct messages with the agent's @address (e.g. `@agent1q07xd... generate_pdf`). Added `_MENTION_RE` regex and `_strip_agent_mention()` helper; updated `classify_text_cta()` to strip a single leading @mention before exact-CTA matching. Also added `revise_in_chat` / `revise_in_chat` variants. Commands embedded in normal sentences remain rejected. Added 3 new test functions covering: agent-address mention, display-name mention, embedded-command rejection, and end-to-end `handle_chat_message` routing. Test suite: `python -m pytest backend/tests -q` → **35 passed**.
- **Phase 2 — Unified canonical parser & loop elimination:**
  1. Replaced duplicated parsing logic in `log_inbound_diagnostics` and `parse_action_selection` with a single canonical function `parse_canonical_text_action()`. Both the logger and the action router now evaluate text through the exact same logic.
  2. Strips leading `@mention` (address or display name), parses full JSON action objects (supporting extra client fields such as `primary: true`), and resolves plain CTA commands (`generate_pdf`, `generate pdf`, `revise`).
  3. Added `is_json_like_text()` error path in `handle_chat_message()`: if an inbound payload is JSON-like but invalid or unsupported, it returns a single concise error message and never resends a review card.
  4. Added `_is_duplicate_msg_id()` to prevent repeated processing of identical message IDs alongside the existing sender+draft+action idempotency guard.
  5. Expanded end-to-end handler unit tests in `backend/tests/test_spike_agent.py` proving that mention-prefixed JSON action payloads invoke `deliver_pdf()` once, send no review card, duplicate actions and duplicate message IDs are suppressed, and unique asset names are generated.
  Verified: `python -m pytest backend/tests -q` → **44 passed**.
- **Phase 2 — PDF delivery lock release:** The Generate PDF guard was retaining a completed request for 120 seconds, causing subsequent ASI:One clicks to receive "already being generated" without running delivery. It now protects only active generation and is always released in a `finally` block after delivery finishes or fails. The local fallback PDF is written to `backend/output/`. Added tests for concurrent duplicate suppression and lock release after a completed delivery. `py_compile` passed. Full-suite execution in the Codex runner stalled after 28 progress dots during Windows `asyncio` initialization; re-run locally is required.



## Done but not verified live
- Live ASI:One chat interaction (`asi1.ai`) with user login (NOT TESTED).
- Live Agentverse Mailbox registration via Agent Inspector (NOT TESTED).
- Live Agentverse file delivery with real API key (NOT TESTED).

## Decisions made
- Pin only the eight requested top-level packages in `requirements.txt`.
- Implementation order locks Phase 2 risk spike before scrape/LLM/full flow.
- Config reads env via `dotenv_values(path)` from explicit file path (default `backend/.env`).
- **Editable textarea decision:** Because prefilled editable textarea is NOT supported in the card schema, do NOT invent fields. Fallback implemented: draft cover letter is sent as formatted markdown in `TextContent`, revision instructions are accepted via chat messages, and the card is used strictly for action triggers (`Generate PDF`, `Revise in Chat`).
- **ResourceContent resource_id:** Pydantic v1 `UUID4` requires strict UUID version 4. Safely check / generate UUIDv4 for `resource_id` while preserving `asset_id` in `asset_uri`.
- **ExternalStorage calls:** Wrap synchronous `requests` calls in `asyncio.to_thread` to avoid blocking async event loop.

## Noticed (out of scope, not fixed)
- Upstream `uagents_core.storage.ExternalStorage.__init__` bug: uses `AgentverseConfig().storage_endpoint` instead of `storage_api`. Handled in caller via explicit `storage_url=AgentverseConfig().storage_api`.
- Test failure in `backend/tests/test_spike_agent.py::test_concurrent_generate_pdf_does_not_call_deliver_pdf_twice`: `deliver_pdf` awaited 1 time instead of 0 during concurrent duplicate suppression check.

## Blockers
- Manual: User to run spike agent and execute the manual verification checklist in ASI:One.
