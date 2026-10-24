# InternAssist — Progress Ledger
## Current prompt
Phase 8 complete: ResourceContent detectable; PDF bytes path unsupported; pasted `resume:` remains the intake path.

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
- **Phase 2 — ASI:One missing PDF attachment:** Live ASI:One testing showed that a mixed `ChatMessage` containing a success `TextContent`, `ResourceContent`, and `EndSessionContent` displayed only the text. Changed Agentverse success delivery to send one `ChatMessage` with only the `ResourceContent`, matching the official Fetch.ai storage example. Added assertions that the successful resource message contains no other content. Live retest required after restart.
- **Phase 2 — PDF delivery messaging & local accessibility:** Live testing confirmed that the ASI:One web client (`asi1.ai`) renders only image attachments and does not download or render PDF `ResourceContent` documents in the browser. In addition, web browsers block navigation to local file paths from HTTPS pages. Updated `deliver_pdf()` in `backend/scripts/spike_agent.py` to send clear status confirmation, explain ASI:One's in-browser attachment limitation, and provide copy-pasteable Windows commands (`Invoke-Item`) for immediate opening. Verified tests: 44 passed.
- **Phase 3 — Jina Reader integration & job fetcher:** Implemented `backend/app/job_fetcher.py` with `JobPostingContent` dataclass, `validate_job_url()` (pure synchronous URL validator with private/loopback/cloud-metadata SSRF guard), and `fetch_job_posting()` (non-blocking HTTP fetch via `asyncio.to_thread(_http_get)` with timeout, safe exception mapping, and response sanitization/truncation). Verified Jina Reader API shapes in `docs/ASI_INTEGRATION_NOTES.md` §F. Comprehensive unit tests added in `backend/tests/test_job_fetcher.py` covering URL validation, SSRF guards, authentication headers, error status mapping, response decode/type safety, logging sanitization, prompt injection safety, and thread pool dispatch.
- **Idempotency test alignment:** Fixed key mismatch in `backend/tests/test_spike_agent.py::test_concurrent_generate_pdf_does_not_call_deliver_pdf_twice`. The test previously locked `draft_id="idem-draft-001"` while sending a plain text `TextContent("generate_pdf")` message, which canonically resolves to `draft_id="dummy-draft-001"`. Aligned the test lock setup to `"dummy-draft-001"`. Full suite: **108 passed, 0 failed**.
- **Phase 4 — Cover letter generation (LLM):** Implemented `backend/app/cover_letter.py` with `CoverLetterDraft` dataclass, safe exception hierarchy (`CoverLetterError`, `InvalidJobContentError`, `InvalidResumeError`, `LLMProviderError`, `InvalidLLMResponseError`), pure prompt builder `build_cover_letter_messages()` with delimiter injection defense and strict factuality rules, synchronous `_call_llm()` executed via `asyncio.to_thread()`, output validation (code fence stripping, length checks), and privacy-safe logging (no prompts, job texts, resume data, or keys). Implemented minimal OpenAI-compatible client in `backend/app/llm_client.py`. Documented API shapes and Phase 5 handoff in `docs/ASI_INTEGRATION_NOTES.md` §G. Comprehensive test suite in `backend/tests/test_cover_letter.py` (28 passed). Full test suite: `python -m pytest backend/tests -q` → **136 passed**.
- **Phase 5 — Full URL to Draft to PDF pipeline:**
  1. Single URL detection (`extract_candidate_urls`) with trailing punctuation stripping (`.,;:!?)]>"'”’`), deduplication, and zero/multiple URL guidance messages.
  2. Generation pipeline: fetches job via `fetch_job_posting`, then generates tailored cover letter via `generate_cover_letter` (exactly one LLM call), and sends letter text in plain chat followed by one interactive review card. *(Phase 5 originally used shared `load_resume`; Phase 7+ uses only the sender's in-memory pasted resume — handler never calls `load_resume`.)*
  3. Bounded in-memory session store (`_ACTIVE_DRAFTS`, `MAX_SESSIONS = 1000`) with oldest-first eviction and strict sender isolation.
  4. Parameterized review card (`build_review_card`) with applicant name, neutral title `"Tailored cover letter"`, status, and opaque UUID `draft_id`.
  5. Parameterized safe PDF rendering (`render_cover_letter_pdf`) with Unicode smart quote/dash normalization and latin-1 sanitization.
  6. Idempotency guard and lock release on delivery error; draft_id mismatch rejection; missing active draft guidance; revise-in-chat guidance.
  7. Added 22 new comprehensive flow tests in [backend/tests/test_phase5_flow.py](../backend/tests/test_phase5_flow.py). Full test suite: `python -m pytest backend/tests -q` → **158 passed**.
- **Phase 6 — PDF layout & closing block cohesion:**
  1. Identified root cause of orphaned signature: `pdf.multi_cell(0, 8, safe_text)` triggered auto page breaks across the sign-off ("Sincerely,") and applicant name when the closing fell near `pdf.page_break_trigger`.
  2. Updated `render_cover_letter_pdf` in [backend/scripts/spike_agent.py](../backend/scripts/spike_agent.py): splits text at the second-to-last non-empty line into body and closing, computes closing height, uses `pdf.will_page_break(closing_height)` to advance pages cleanly before the closing, and wraps the closing block in `with pdf.unbreakable() as doc:`.
  3. Preserved signature and return type; added private `_keep_together` test hook to allow regression verification.
  4. Created [backend/tests/test_pdf_layout.py](../backend/tests/test_pdf_layout.py) with 12 unit/regression tests: 60-length sweep, deterministic bug reproduction without fix, single-page short letter, exact-fit boundaries, unusual and accented name support, unique marker rendering, compatibility wrapper, and logging safety.
  5. Full test suite: `python -m pytest backend/tests -q` → **170 passed** (158 existing + 12 new Phase 6 tests).
- **Phase 7 — Public-user resume intake, session isolation, deployment readiness, and final documentation:**
  1. Implemented user-specific in-memory resume intake triggered by lowercase literal `resume:`. Normalizes line endings, strips control characters, validates `Name:` line and sane name length cap (`MAX_NAME_CHARS = 100`), validates substantive content (`MIN_RESUME_EXTRA_NONWS_CHARS = 40`), and enforces Phase 4 size limit (`MAX_RESUME_CHARS = 15_000`) by rejection (never truncated), with cheap pre-parse message length check.
  2. Decided model mapping without modifying `models.py`: raw user-submitted resume text is held in existing `Resume.summary` field and applicant name in `Resume.name`, ensuring facts reach the Phase 4 prompt unaltered under `Professional Summary:`.
  3. Bounded in-memory resume store (`_RESUME_STORE`, `MAX_RESUME_SESSIONS = 1000`) with oldest-first eviction. Single operation `_clear_session(sender)` cleanly evicts a sender's resume and active draft together, preventing orphan drafts. All resume data lives strictly in process memory and is lost on restart (no files, no database, no cloud storage).
  4. Added `clear_resume` command: case-insensitive, whitespace-trimmed, clears only the requester's resume and draft idempotently.
  5. Resume-presence gate: senders with no stored resume receive `"Please paste your resume first, beginning with \`resume:\`."` on job URLs and plain text alike, executing before URL parsing and making zero Jina, LLM, PDF, or storage calls.
  6. Shared-resume behavior completely removed from chat path: `load_resume` is never called by the handler.
  7. Multi-user session isolation: Sender A and Sender B maintain completely isolated resumes, drafts, and PDFs; generator receives only the requester's stored resume; review card and PDF render only the requester's applicant name; foreign draft IDs are rejected.
  8. Race guard: per-sender resume version tracking invalidates in-flight generation if resume is replaced or cleared while fetch/LLM is working, discarding the draft and notifying the user.
  9. Privacy & logging: strictly no resume text, names, emails, phones, job text, prompts, letters, keys, seeds, or full URL paths logged. Only counts, error classes, and hostnames.
  10. Added comprehensive test suite in [backend/tests/test_phase7_resume_sessions.py](../backend/tests/test_phase7_resume_sessions.py) covering all 22 required Phase 7 cases.
  11. Updated [docs/ASI_INTEGRATION_NOTES.md](ASI_INTEGRATION_NOTES.md) with verified data flow and provider privacy notes.
  12. Created [docs/DEPLOYMENT.md](DEPLOYMENT.md) with complete Windows PowerShell operational setup and manual acceptance flows.
  13. Deployment status: **deployment-ready pending user-managed configuration and hosting** (never "deployed").
  14. Full test suite: `python -m pytest backend/tests -q` → **202 passed** (170 baseline + 32 Phase 7 tests). 3 repeat runs verified 100% deterministic (0 failures).
- **Phase 8 — Secure PDF-resume intake (unsupported bytes path; pasted `resume:` remains):**
  1. Verified from installed `uagents-core` 0.4.12 and Innovation Lab Image Analysis Agent docs (recorded in [docs/ASI_INTEGRATION_NOTES.md](ASI_INTEGRATION_NOTES.md) §K): inbound `ResourceContent` is detectable; `Resource` has only `uri` + `metadata` (no inline bytes); Chat Interface storage is opaque; no verified safe PDF-bytes retrieval path for this product.
  2. Product decision: do **not** invent HTTPS URI download, Agentverse asset-id assumptions, or PDF parsing (`pypdf` not added). On any inbound `ResourceContent` (after CTA/action routing and `clear_resume`), reply with fixed `PDF_UPLOAD_UNSUPPORTED_MESSAGE` and make zero Jina / LLM / PDF-writer / storage-upload / `load_resume` calls.
  3. Pasted `resume:` intake (Phase 7) remains the only supported applicant-data path. Session memory only (`_RESUME_STORE` / `_ACTIVE_DRAFTS`); lost on restart.
  4. Privacy: `serialize_resume()` omits email, phone, LinkedIn, and GitHub from the LLM prompt (name and qualifications kept). Contact fields are never invented.
  5. **Stale-claim correction:** Phase 5 historically loaded the shared fixture via `load_resume` in the chat pipeline. Phase 7 unhooked that; the chat handler still never calls `load_resume`. Phase 8 attachment path also never calls it. Fixture `load_resume` remains for offline tests / non-chat utilities only.
  6. Tests: [backend/tests/test_phase8_pdf_intake.py](../backend/tests/test_phase8_pdf_intake.py) (ResourceContent detection, zero external calls, job-URL+attachment skip, CTA priority over attachment, pasted `resume:` still works) plus contact-exclusion coverage in [backend/tests/test_cover_letter.py](../backend/tests/test_cover_letter.py).
  7. Full test suite after Phase 8: `python -m pytest backend/tests -q` → **208 passed**; `python -m compileall -q backend` → exit 0. (202 Phase 7 baseline + Phase 8 / contact-exclusion tests.)

## Done but not verified live
- Live ASI:One chat interaction (`asi1.ai`) with user login (NOT TESTED).
- Live Agentverse Mailbox registration via Agent Inspector (NOT TESTED).
- Live Agentverse file delivery with real API key (NOT TESTED).
- Live Jina Reader API network calls with real job posting URLs (NOT TESTED).
- Live ASI:One LLM API chat completion network calls with real API keys / models (NOT TESTED).
- Live end-to-end URL submission, draft generation, and PDF download in ASI:One (NOT TESTED).
- Live multi-user concurrency and session isolation in ASI:One production chat (NOT TESTED).
- Live ASI:One PDF / file attachment upload to this agent (NOT TESTED).
- Live confirmation that ASI:One ever delivers inbound PDF bytes or `agent-storage://` asset IDs readable by this agent's token (NOT TESTED; `# TODO: verify against docs / live experiment`).
- **OWNER-REPORTED live evidence (not re-run by this agent):** "A public Nintendo job URL was accepted in ASI:One; the agent fetched the posting; it generated a tailored letter from the fictional Alex Example resume; Generate PDF created and opened a local PDF containing the Nintendo-tailored content." Date: `# TODO: date from owner`.

## Remaining manual limitations
- ASI:One browser PDF attachment rendering may vary (the ASI:One chat web client only previews images; PDF document attachments require downloading or opening from local mirror).
- **Inbound PDF resume upload is not supported:** attaching a PDF in ASI:One currently yields the unsupported-upload guidance; users must paste `resume:` text. No PDF byte download/parsing is implemented.
- Live Agentverse PDF delivery is not verified with a production user key (falls back to local `backend/output/`).
- Letter quality and invented-fact verification is a manual inspection step.
- Position/title extraction is not implemented (safe neutral fallback `"Tailored cover letter"` is used).
- Revision in chat is not implemented (instructs user to provide a new job URL).
- Sessions are strictly in-memory and lost on agent restart.

## Decisions made
- Pin only the eight requested top-level packages in `requirements.txt`.
- Implementation order locks Phase 2 risk spike before scrape/LLM/full flow.
- Config reads env via `dotenv_values(path)` from explicit file path (default `backend/.env`).
- **Editable textarea decision:** Because prefilled editable textarea is NOT supported in the card schema, do NOT invent fields. Fallback implemented: draft cover letter is sent as formatted markdown in `TextContent`, revision instructions are accepted via chat messages, and the card is used strictly for action triggers (`Generate PDF`, `Revise in Chat`).
- **ResourceContent resource_id:** Pydantic v1 `UUID4` requires strict UUID version 4. Safely check / generate UUIDv4 for `resource_id` while preserving `asset_id` in `asset_uri`.
- **ExternalStorage calls:** Wrap synchronous `requests` calls in `asyncio.to_thread` to avoid blocking async event loop.
- **Phase 3 HTTP client:** Used existing pinned project dependency `httpx` (`httpx==0.28.1`) via synchronous `_http_get()` dispatched through `asyncio.to_thread()`.
- **Phase 3 URL normalization and length limits:** `MAX_URL_LENGTH = 2048`, `MIN_CONTENT_LENGTH = 50`, `MAX_CONTENT_LENGTH = 50_000` (oversize content is truncated to 50k chars).
- **Phase 4 LLM client:** Used existing pinned project dependency `openai` (`openai==3.37.0`) via synchronous client dispatched through `asyncio.to_thread()`.
- **Phase 4 prompt security:** Job posting and resume are enclosed in explicit XML-like delimiters (`<untrusted_job_posting>`, `<resume_data>`). Any occurrences of delimiter tags inside untrusted text are escaped to prevent prompt breakout. System message explicitly instructs model to treat job text as untrusted data, follow only resume facts, and forbid invented facts.
- **Phase 4 length limits:** `MAX_JOB_CHARS = 15_000` (oversize job postings truncated with warning), `MAX_RESUME_CHARS = 15_000` (oversize resume rejected to prevent silent omission of applicant facts), `MIN_LETTER_CHARS = 100`, `MAX_LETTER_CHARS = 5_000`.
- **Phase 5 session storage:** In-memory dictionary `_ACTIVE_DRAFTS` keyed by sender address. Lost on agent process restart (no database or external session storage per simplicity rule). Bounded at `MAX_SESSIONS = 1000` with oldest-first eviction.
- **Phase 5 position/title extraction:** `JobPostingContent` does not contain a job title field (only `source_url` and `content`). Rather than guessing or making an unverified/extra LLM call, a neutral label `"Tailored cover letter"` is used in the review card. Marked as future work in docs.
- **Phase 5 PDF character sanitization:** `fpdf2` built-in Helvetica core font expects latin-1 encodable characters. Model text containing Unicode smart quotes (`\u2018`, `\u2019`, `\u201c`, `\u201d`) and dashes (`\u2013`, `\u2014`) are normalized to ASCII equivalents, and any remaining non-latin1 characters are safely replaced before PDF generation.
- **Phase 5 pipeline sequencing (superseded for resume source by Phase 7):** Fetch job first, then generate. Phase 5 originally used `load_resume` via `asyncio.to_thread`; Phase 7+ chat path uses only the sender's in-memory `_RESUME_STORE` entry (never `load_resume` in the handler). If fetch fails, generator is not called. Exactly one LLM call per URL request.
- **Phase 6 closing block cohesion:** Closing block is deterministically identified as the last two non-empty lines (complimentary close and applicant name). Uses `pdf.will_page_break(closing_height)` to trigger clean page advance and `with pdf.unbreakable() as doc:` to prevent split across pages.
- **Phase 6 PDF test stream extraction:** Uses standard-library `zlib.decompress` on PDF content streams to verify page contents directly without introducing external dependencies (such as `pypdf`).
- **Phase 7 resume model mapping (0c decision):** Stored raw resume body in existing `Resume.summary` field and extracted name in `Resume.name`. No change to `models.py` required.
- **Phase 7 session storage & bounds:** Process memory stores `_RESUME_STORE`, `_RESUME_VERSIONS`, and `_ACTIVE_DRAFTS`. Eviction bounds both stores to 1000 sessions with oldest-first eviction. `_clear_session(sender)` drops both resume and draft in one atomic operation.
- **Phase 7 removal of shared resume fallback:** `load_resume` completely unhooked from chat handler. Only user-submitted in-memory resumes are used.
- **Phase 8 PDF bytes path:** No verified official path for this agent to obtain inbound PDF file bytes without inventing HTTPS downloads of sender URIs (SSRF) or unverified Agentverse asset-id assumptions. Detect `ResourceContent`, guide to paste `resume:`, do not parse PDFs.
- **Phase 8 LLM contact omission:** `serialize_resume()` deliberately omits email/phone/LinkedIn/GitHub from prompts while keeping name and qualifications.

## Noticed (out of scope, not fixed)
- Upstream `uagents_core.storage.ExternalStorage.__init__` bug: uses `AgentverseConfig().storage_endpoint` instead of `storage_api`. Handled in caller via explicit `storage_url=AgentverseConfig().storage_api`.
- Avoiding leaving closing alone on an otherwise empty new page: when letter body fills page 1 right up to margin, the closing block moves to page 2, leaving the closing alone on page 2. Keeping the last body line with the closing is deferred as a known layout optimization.
- No rate limiting or per-sender quota: a public agent can be spammed by an arbitrary sender and incur Jina and LLM costs without restriction.
- Eviction capacity risk: when `MAX_RESUME_SESSIONS` is reached, an active user who has not recently sent a message can have their resume silently evicted.
- ASI:One client/browser PDF rendering may vary: the ASI:One web chat client (`asi1.ai`) renders only image previews inline and does not preview PDF `ResourceContent` documents.
- Live ASI:One multi-user behavior, live Jina, live LLM, and live Agentverse PDF delivery are NOT TESTED unless the owner reports them.
- Official inbound PDF-bytes / opaque Chat Interface storage remains a platform blocker for auto-parsing uploaded resumes; pasted `resume:` is the supported workaround.
- Resource URI values must never be logged (privacy); Phase 8 helpers/tests assert count-only detection without reading URI strings into logs.

## Blockers and manual steps needed from owner
- Configure `.env` with private API credentials (`AGENT_SEED`, `AGENT_NAME`, `AGENT_PORT`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `JINA_API_KEY`, `AGENTVERSE_API_KEY`).
- Run the agent locally or on a persistent host using PowerShell commands documented in [docs/DEPLOYMENT.md](DEPLOYMENT.md).
- Perform post-start manual acceptance flow across two distinct ASI:One user accounts.



