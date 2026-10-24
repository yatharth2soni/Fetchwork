# ASI / Fetch.ai Integration Notes

Date recorded: 2026-10-23
Sources referenced:
- Installed package source: `uagents_core.contrib.protocols.chat` and `uagents_core.contrib.protocols.chat.cards` (`.venv/Lib/site-packages/uagents_core/`)
- Installed package source: `uagents_core.storage` (`.venv/Lib/site-packages/uagents_core/storage.py`)
- Installed package source: `uagents.agent` and `uagents.mailbox` (`.venv/Lib/site-packages/uagents/`)
- Fetch.ai Innovation Lab Examples: `https://github.com/fetchai/innovation-lab-examples` (specifically `image-agent-payment-protocol/payment_proto.py` and `interactive-cards`)
- Fetch.ai Documentation: `https://docs.fetch.ai/` and `https://innovationlab.fetch.ai/resources/docs/interactive-cards/asi-interactive-cards`
- ASI:One Chat Platform: `https://asi1.ai`

---

## A. Card Schema (ASI:One Interactive Cards)
*Source: `.venv/Lib/site-packages/uagents_core/contrib/protocols/chat/cards/__init__.py` and `README.md`; Innovation Lab Interactive Cards docs (2026-10-23)*

### 1. Card Types Supported
There are 5 registered card kinds (`card_kind`):
1. **`carousel` (`CarouselCardPayload`)**: A horizontal carousel of items.
   - Required fields: `items` (list with min length 1). Each item requires `id`, `title`, and `primary_cta` (`CtaAction`).
   - Optional fields: `title`, `subtitle`, item `image`, item `subtitle`, item `badges`, item `secondary_text`.
2. **`detail` (`DetailCardPayload`)**: A detailed view for inspecting an item.
   - Required fields: `title` (str), `ctas` (list of `CtaAction`, min length 1).
   - Optional fields: `hero_image` (str), `summary_rows` (list of `DetailSummaryRow` with `label`, `value`), `sub_options` (`DetailSubOptions` with `name`, `kind`: `"radio" | "select"`, `label`, `choices`).
3. **`form` (`FormCardPayload`)**: An interactive input form.
   - Required fields: `fields` (list of `FormField`, min length 1), `submit_cta` (`CtaAction`).
   - Optional fields: `title` (str).
4. **`review` (`ReviewCardPayload`)**: A review / approval card (e.g. for reviewing summaries or orders).
   - Required fields: `title` (str), `summary_rows` (list of `ReviewSummaryRow` with `label`, `value`, min length 1), `approve_cta` (`CtaAction`).
   - Optional fields: `reject_cta` (`CtaAction`).
5. **`custom` (`CustomCardPayload`)**: An arbitrary element-tree UI.
   - Required fields: `root` (`ElementTreeNode`, max depth 8).
   - Supported node types: `text`, `heading`, `image`, `badge`, `divider`, `section`, `group`, `button`, `input`, `list`, `choice_grid`.

### 2. Prefilled Editable Multiline Text Field Support
- **VERIFIED: NOT SUPPORTED**.
- Inspection of `FormField` and `InputNode` in `uagents_core.contrib.protocols.chat.cards` reveals:
  ```python
  class FormField(_StrictBase):
      name: str
      kind: Literal["text", "number", "email", "select", "checkbox"]
      label: str
      required: bool = False
      options: list[FormFieldOption] | None = None
      placeholder: str | None = None
  ```
  - Both `FormField` and `InputNode` inherit from `_StrictBase` which configures `extra="forbid"`.
  - Allowed `kind` values are strictly `"text"`, `"number"`, `"email"`, `"select"`, `"checkbox"`. There is no `"textarea"` or `"multiline"` kind.
  - The models have `placeholder`, but **no `value` or `default` field**.
  - Any attempt to pass `value="something"` or `default="something"` or an invalid kind causes an immediate Pydantic `ValidationError`.
- **Decision Tree Result**: Because prefilled editable textarea is not supported, we must **NOT** invent schema fields. The fallback is to send the draft cover letter as standard `TextContent` in the chat, invite natural-language revisions via regular chat messages, and use the interactive card purely for structured action buttons (`Generate PDF` / `Revise in Chat`).

### 3. Exact Accepted JSON Shape & Wire Format
Cards are transmitted within a `ChatMessage` as a `MetadataContent` block (`type="metadata"`).
The `metadata` dictionary contains:
- `card_protocol_version`: `"1"` (required)
- `requires_card_interaction`: `"true"` (required)
- `card_kind`: `"carousel" | "detail" | "form" | "review" | "custom"` (required)
- `card_payload`: Serialized JSON string of the corresponding card payload model (required)
- `card_id`: UUID string (optional, recommended for correlation)
- `is_terminal`: `"true"` (optional)
- `preferred_drawer_width_px`: Integer string, e.g. `"480"` (optional)

Helpers in `uagents_core.contrib.protocols.chat.cards`:
- `create_card_content(payload, card_id=None, is_terminal=False, preferred_drawer_width_px=None) -> MetadataContent`
- `extract_card(content: MetadataContent) -> Card | None`

---

## B. Card Action Payload (Client -> Agent)
*Source: `.venv/Lib/site-packages/uagents_core/contrib/protocols/chat/cards/README.md` and `__init__.py` (2026-10-23)*

### 1. Inbound Shape on Button Click
When a user clicks a card action button (such as `approve_cta` or `CtaAction`), the response reaches the agent in an inbound `ChatMessage`.
The standard wire representation is a `MetadataContent` block (`type="metadata"`) where `metadata` contains:
- `card_protocol_version`: `"1"`
- `card_id`: UUID string (if correlated with request)
- `selection`: JSON-encoded string representing a dictionary of primitives (`dict[str, str | int | float | bool]`).
- `text`: Optional string (if provided by client)
- `cancelled`: `"true"` (if user cancelled / dismissed)

Notice: Unlike request metadata, response metadata does **not** include `card_kind`.

### 2. Helper Method
- `extract_card_response(content: MetadataContent) -> CardResponse | None`:
  Returns a `CardResponse` instance with attributes:
  - `response.card_id` (`UUID4 | None`)
  - `response.text` (`str | None`)
  - `response.selection` (`dict[str, SelectionValue] | None`)
  - `response.cancelled` (`bool`)

### 3. Backwards Compatibility
Some legacy clients send the `selection` dictionary as JSON text directly in `TextContent`. Robust agents parse `extract_card_response(block)` across all `MetadataContent` blocks and fall back to parsing JSON from `msg.text()`.

### 4. Inclusion of Input / Form Values
For form submissions, input values are placed into `selection` keyed by each field's `name`. For button actions (`CtaAction`), `selection` contains the dictionary defined on the button (e.g. `{"action": "generate_pdf", "draft_id": "..."}`).

### 5. Multi-Path Action Resolution Pattern
Because different client versions may omit `card_protocol_version`, echo `card_kind`, or place `selection` in legacy text blocks, robust agents parse in three steps:
1. Standard `extract_card_response(block)` on `MetadataContent`.
2. Direct JSON decode of `block.metadata.get("selection")` or direct check for `block.metadata.get("action")`.
3. Narrowly-scoped JSON fallback on `TextContent` matching `{...}` where all keys are within `{"action", "draft_id", "card_id"}`.

### 6. Confirmed ASI:One Live Behavior (2026-10-23)
**VERIFIED (live run)**: When a user clicks a card CTA button (`Generate PDF` / `Revise in Chat`) in ASI:One, the agent receives:
```
[Block 0] class=TextContent, type='text'
```
No `MetadataContent` is present. The `TextContent.text` value matches the CTA button label exactly (e.g. `"Generate PDF"`).

This is not covered by paths 1–3 above. A **4th path** is required:

4. **Plain-text CTA fallback (spike compatibility update, implementation verified 2026-10-23; live behavior not re-tested):** Classify `TextContent.text` with Unicode NFKC normalization, zero-width-character removal, trimming, case-folding, and whitespace collapsing. A second comparison removes non-alphanumeric separators. Recognize only CTA-equivalent values and map them to action dicts:
   | Normalized phrase | Mapped action |
   |---|---|
   | `generate pdf` | `{"action": "generate_pdf", "draft_id": "dummy-draft-001"}` |
   | `generate_pdf` | `{"action": "generate_pdf", "draft_id": "dummy-draft-001"}` |
   | `generatepdf`, `create pdf`, `createpdf`, `download pdf`, `downloadpdf` | `{"action": "generate_pdf", "draft_id": "dummy-draft-001"}` |
   | `revise in chat` | `{"action": "revise", "draft_id": "dummy-draft-001"}` |
   | `revise`, `reviseinchat` | `{"action": "revise", "draft_id": "dummy-draft-001"}` |

   All other text (including normal sentences) produces `None` — no action is triggered. The spike handler also has a temporary, non-looping final fallback for a one-block, nonempty 1–80-character text message: it asks the user to type `generate_pdf`; it neither sends another Review Card nor generates a PDF.

   **Scope**: This fallback uses the spike's static `dummy-draft-001` draft ID. In the full agent, `draft_id` should be resolved from session state instead.

---


## C. Chat Protocol Shapes (uagents-core 0.4.12)
*Source: `.venv/Lib/site-packages/uagents_core/contrib/protocols/chat/__init__.py` (2026-10-23)*

### 1. Protocol Specification
- Name: `"AgentChatProtocol"`
- Version: `"0.3.0"`
- Spec object: `chat_protocol_spec = ProtocolSpecification(name="AgentChatProtocol", version="0.3.0", interactions={ChatMessage: {ChatAcknowledgement}, ChatAcknowledgement: set()})`

### 2. Message Types
1. **`ChatMessage`**:
   - `timestamp`: `datetime` (UTC)
   - `msg_id`: `UUID4` (unique message identifier)
   - `content`: `list[AgentContent]`
   - Helper: `msg.text() -> str` returns concatenated text across all `TextContent` items.
2. **`ChatAcknowledgement`**:
   - `timestamp`: `datetime` (UTC)
   - `acknowledged_msg_id`: `UUID4`
   - `metadata`: `dict[str, str] | None`

### 3. Supported Content Types (`AgentContent` Union)
- `TextContent`: `type="text"`, `text: str` (UTF-8, Markdown supported).
- `ResourceContent`: `type="resource"`, `resource_id: UUID4`, `resource: Resource | list[Resource]` (where `Resource` has `uri: str`, `metadata: dict[str, str]`).
- `MetadataContent`: `type="metadata"`, `metadata: dict[str, str]` (key-value strings, used for cards).
- `StartSessionContent`: `type="start-session"`
- `EndSessionContent`: `type="end-session"`
- `StartStreamContent`: `type="start-stream"`, `stream_id: UUID4`
- `EndStreamContent`: `type="end-stream"`, `stream_id: UUID4`

---

## D. File Delivery via Chat & Storage API
*Source: `.venv/Lib/site-packages/uagents_core/storage.py`, `.venv/Lib/site-packages/uagents_core/config.py`, and `fetchai/innovation-lab-examples/image-agent-payment-protocol/payment_proto.py` (2026-10-23)*

### 1. Storage API: `ExternalStorage`
Located in `uagents_core.storage.ExternalStorage`:
- **Execution model**: Synchronous / blocking HTTP calls using the `requests` library. Must be invoked via `asyncio.to_thread` inside async uAgents handlers.
- **Upstream Bug Note**: In `uagents_core` 0.4.12, `ExternalStorage.__init__` defaults `self.storage_url = storage_url or AgentverseConfig().storage_endpoint`. However, `AgentverseConfig` defines `storage_api` (`https://agentverse.ai/v1/storage`), not `storage_endpoint` (`AttributeError`). Therefore, `storage_url` **must be passed explicitly**:
  ```python
  from uagents_core.config import AgentverseConfig
  from uagents_core.storage import ExternalStorage

  storage = ExternalStorage(
      api_token=api_token,
      storage_url=AgentverseConfig().storage_api, # "https://agentverse.ai/v1/storage"
  )
  ```
- **Asset Creation**:
  ```python
  asset_id = await asyncio.to_thread(
      storage.create_asset,
      name=f"cover_letter_{draft_id}.pdf",
      content=pdf_bytes,
      mime_type="application/pdf",
      lifetime_hours=24,
  )
  ```
  Returns `asset_id` (str, formatted as UUID).
- **Access Control / Permissions**:
  ```python
  await asyncio.to_thread(
      storage.set_permissions,
      asset_id=asset_id,
      agent_address=sender_address, # the user's agent address in ASI:One
      read=True,
      write=False,
  )
  ```

### 2. Building the `ResourceContent` Message
Asset URI scheme: `f"agent-storage://{storage.storage_url}/{asset_id}"`
```python
from uuid import UUID, uuid4
from datetime import datetime, timezone
from uagents_core.contrib.protocols.chat import ChatMessage, Resource, ResourceContent

msg = ChatMessage(
    timestamp=datetime.now(timezone.utc),
    msg_id=uuid4(),
    content=[
        ResourceContent(
            type="resource",
            resource_id=UUID(asset_id),
            resource=Resource(
                uri=f"agent-storage://{storage.storage_url}/{asset_id}",
                metadata={"mime_type": "application/pdf", "role": "document"},
            ),
        )
    ],
)
```
- **Verified delivery shape (2026-10-24):** Fetch.ai's [Google Gemini Image Generation example](https://innovationlab.fetch.ai/resources/docs/examples/integrations/google/google-gemini-image) and its linked `image-agent-payment-protocol` source send a `ChatMessage` containing `ResourceContent` by itself. For ASI:One file delivery, do not combine a status `TextContent`, `ResourceContent`, and `EndSessionContent` in the same response. A live ASI:One test showed that the mixed response displayed the text but did not display the attachment.

### 3. Local Fallback
When `PDF_STORAGE=local` or when Agentverse API key is absent:
1. Save PDF bytes to `backend/output/<filename>.pdf`.
2. Send `TextContent` clearly stating that the file is saved locally to the machine and not attached in the chat:
   `"Cover letter PDF generated and saved locally to backend/output/<filename>.pdf (local file fallback, not a chat attachment)."`

**ASI:One PDF display recovery (2026-10-24):** A live ASI:One test created an Agentverse PDF asset successfully and sent a standalone `ResourceContent` block, but ASI:One (`asi1.ai`) does not render the PDF resource or trigger a browser download (ASI:One's chat UI only handles image MIME types such as `image/png`). In addition, browser security models prevent HTTPS web pages from opening local filesystem paths (`file://` or `C:\...`). The spike saves a local mirror to `backend/output/` on every generation and sends structured `TextContent` containing the file path along with Windows copy-paste commands (`Invoke-Item`) so the local user can access the file immediately.

---

## E. Reachability and Discoverability in ASI:One
*Source: `.venv/Lib/site-packages/uagents/agent.py`, `uagents/mailbox.py`, and Fetch.ai Agentverse / ASI:One guides (2026-10-23)*

### 1. Network Configuration: Mailbox vs Endpoint
- **Local / NAT Agents (Mailbox)**:
  Local development agents behind NAT, corporate firewalls, or dynamic home IPs cannot receive inbound HTTP webhooks directly.
  They are configured with `mailbox=True`:
  ```python
  agent = Agent(name="internassist", seed=seed, mailbox=True)
  ```
  On startup, the agent logs an Agent Inspector URL (`https://agentverse.ai/inspect/?uri=...&address=...`).
  The developer opens the link in a browser, connects to Agentverse, and selects **Mailbox**.
  Agentverse provisions a mailbox, and the agent's background `MailboxClient` polls `https://agentverse.ai/v2/agents/{address}/mailbox` using cryptographic attestation (`Authorization: Agent <attestation>`).
- **Hosted / Public Endpoint**:
  If hosted on a server with a public domain and valid SSL cert, the agent can be configured with:
  ```python
  agent = Agent(name="internassist", seed=seed, endpoint=["https://my-domain.com/submit"], port=8000)
  ```
  The endpoint is registered with the Almanac directory smart contract so counterparties can POST messages directly.

### 2. Discoverability on ASI:One
To appear and be routed to in ASI:One:
1. **Include Chat Protocol with Manifest**:
   ```python
   chat_proto = Protocol(spec=chat_protocol_spec)
   # register handlers ...
   agent.include(chat_proto, publish_manifest=True)
   ```
   `publish_manifest=True` publishes the agent's support for `AgentChatProtocol` (version 0.3.0) to the Almanac registry.
2. **Agentverse Profile & Metadata**:
   In Agentverse (`agentverse.ai`):
   - Set agent Name and public Handle (e.g. `@internassist`).
   - Fill out Description and README detailing what the agent does (job posting intake, cover letter generation). The ASI:One LLM router uses semantic search against these profiles to route user prompts.
3. **User Interaction**:
   Users on `asi1.ai` can start a chat directly with `@internassist` or select the agent from the Agentverse catalog.

---

## F. Phase 3: Jina Reader Integration
*Date verified: 2026-10-24*
*Sources: Official Jina Reader documentation `https://jina.ai/reader/` and open-source repository `https://github.com/jina-ai/reader`*

### 1. Request URL Format
- **Format:** The target webpage URL is directly prefixed with the Reader base URL: `https://r.jina.ai/{target_url}`.
- **Example:** `https://r.jina.ai/https://example.com/jobs/senior-python-dev`.
- **Status:** VERIFIED (Source: `https://github.com/jina-ai/reader/blob/main/README.md`, verified 2026-10-24).

### 2. Authentication & API Key
- **Optional:** An API key is optional for standard usage. Unauthenticated requests are allowed up to 20 Requests Per Minute (RPM), tracked by IP address.
- **Header:** When an API key is provided, authentication is passed via the standard HTTP header:
  `Authorization: Bearer <jina_api_key>`
- **Tier limits:** Free/paid API keys increase rate limits to 500 RPM, tracked by key.
- **Status:** VERIFIED (Source: `https://jina.ai/reader#pricing` and `https://github.com/jina-ai/reader`, verified 2026-10-24).

### 3. Request Headers
- `Accept`: `text/plain` or `text/markdown` requests plain markdown text directly; `application/json` returns a JSON envelope containing `{ "code": 200, "data": { "title": "...", "url": "...", "content": "..." } }`.
- `X-Timeout`: Optional integer seconds (max 180s) specifying how long Reader waits for client-side JavaScript rendering.
- `X-Respond-With`: Optional format selection (`markdown`, `html`, `text`, `frontmatter`).
- **Status:** VERIFIED (Source: `https://github.com/jina-ai/reader/blob/main/README.md`, verified 2026-10-24).

### 4. Response Shape & Status Codes
- **Success (HTTP 200):** Clean markdown page text in body with `Content-Type: text/plain` (or structured JSON if requested).
- **HTTP 401 Unauthorized:** Invalid or malformed API key in `Authorization` header.
- **HTTP 403 Forbidden:** Access forbidden by network, anti-bot, or proxy block.
- **HTTP 429 Too Many Requests:** Rate limit exceeded (20 RPM unauthenticated or tier RPM limit).
- **HTTP 404 / 500:** Target URL not found, or upstream/Reader rendering failure.
- **Status:** VERIFIED (Source: `https://github.com/jina-ai/reader`, verified 2026-10-24).

### 5. Client Safeguards & Limits
- **Client Timeout:** Enforced at 25.0 seconds (`DEFAULT_TIMEOUT_SECONDS`) via `httpx`.
- **Response Size Cap:** Maximum 50,000 characters (`MAX_CONTENT_LENGTH`). Content exceeding this cap is truncated to ensure predictable LLM prompt sizing.
- **Minimum Content Length:** Minimum 50 characters (`MIN_CONTENT_LENGTH`) to reject empty pages, JS loading stubs, or trivial error messages.
- **Status:** VERIFIED (Configured via client constants, 2026-10-24).

### 6. Security Note for Downstream Phases (Phase 5)
- **Untrusted Input:** Scraped job-posting text is strictly UNTRUSTED DATA. It must never be executed or treated as system or developer instructions.
- Phase 5 prompt generation MUST wrap this text within explicit XML-style delimiters (e.g. `<job_posting>...</job_posting>`) and instruct the LLM that the content is strictly data to be analyzed, never commands to be executed.

---

## G. Phase 4: Cover letter generation (LLM)
*Date verified: 2026-10-24*
*Sources: Official ASI:One API documentation (`https://api.asi1.ai/v1`, `https://asi1.ai`), OpenAI Python SDK (`openai==3.37.0`), and `backend/app/config.py`*

### 1. SDK / Client Class and Sync/Async Nature
- **Client Class:** `openai.OpenAI` from installed package `openai==3.37.0`.
- **Initialization:** `client = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key, timeout=timeout)`.
- **Execution Nature:** Synchronous / blocking HTTP calls using `httpx`.
- **Async Threading Rule:** To avoid blocking the uAgents async event loop, blocking LLM calls MUST be dispatched to worker threads using `await asyncio.to_thread(...)`.
- **Status:** VERIFIED (Source: installed package `.venv/Lib/site-packages/openai/`, verified 2026-10-24).

### 2. Request Shape
- **Endpoint:** `POST https://api.asi1.ai/v1/chat/completions` (OpenAI-compatible).
- **Method Call:** `client.chat.completions.create(model=model, messages=messages, temperature=0.7, timeout=timeout)`.
- **Roles:** Standard chat roles: `"system"` (fixed instructions, grounding, factuality constraints) and `"user"` (delimited data: `<untrusted_job_posting>` and `<resume_data>`).
- **Status:** VERIFIED (Source: `https://api.asi1.ai/v1`, verified 2026-10-24).

### 3. Response Shape
- **Success:** `response.choices[0].message.content` (string containing generated cover letter text).
- **Refusal / Empty:** `response.choices[0].message.refusal` (if content is filtered or refused) or empty string `""` / `None`.
- **Error Types:** `openai.APIError`, `openai.APITimeoutError`, `openai.APIConnectionError`, `openai.RateLimitError`, `openai.AuthenticationError`.
- **Status:** VERIFIED (Source: `openai.types.chat.ChatCompletion`, verified 2026-10-24).

### 4. Timeout & Size Safeguards
- **Timeout:** Enforced at 30.0s (`DEFAULT_LLM_TIMEOUT_SECONDS`).
- **Input Caps:** `MAX_JOB_CHARS = 15_000` (oversize job postings truncated), `MAX_RESUME_CHARS = 15_000` (oversize resume rejected to prevent fact loss).
- **Output Length Bounds:** Minimum 100 characters (`MIN_LETTER_CHARS`), maximum 5,000 characters (`MAX_LETTER_CHARS`).
- **Status:** VERIFIED (Configured via client constants, 2026-10-24).

### 5. How Phase 5 Should Use This
- In Phase 5, the chat message handler will fetch the job posting via `await fetch_job_posting(url)`, load applicant resume via `load_resume()`, and call `await generate_cover_letter(job_posting, resume)`.
- If a `CoverLetterError` occurs, catch it and send `err.user_message` directly to the user in chat.
- The returned `CoverLetterDraft.text` is model output and must be treated as untrusted text before rendering to interactive review cards or PDF.

---

## H. Phase 5: URL to draft to PDF flow
*Date verified: 2026-10-24*
*Sources: `scripts/spike_agent.py`, `app/job_fetcher.py`, `app/cover_letter.py`, `app/data/resume.py`, and `uagents_core.contrib.protocols.chat`*

### 1. Final Message Routing Flow
1. **Acknowledgement:** Inbound `ChatMessage` is immediately acknowledged with a `ChatAcknowledgement(acknowledged_msg_id=msg.msg_id)`.
2. **Message ID Deduplication:** Repeated message IDs within the idempotency window (120s) are ignored.
3. **Card & JSON Action Parsing:**
   - Evaluated first before URL or natural language parsing.
   - `action == "generate_pdf"`:
     - Requires an active session draft for the sender (`get_session_draft(sender)`).
     - Validates that the action's `draft_id` matches the stored draft (or uses active draft if omitted in plain CTA).
     - Idempotency guard (`_is_duplicate_request`) suppresses concurrent generation clicks for the same sender and draft.
     - Renders sanitized PDF bytes via `render_cover_letter_pdf(letter_text, applicant_name)` using `fpdf2` and delivers via `deliver_pdf()`.
     - Generation lock is released in a `finally` block on completion or failure.
   - `action == "revise"`:
     - Replies with a concise message: `"Revising cover letters in chat is not yet supported. Please send a new job-posting URL to generate an updated draft."` (no LLM call, no card).
   - Unsupported actions / JSON-like payloads:
     - Handled safely with actionable text replies without triggering card generation or unhandled exceptions.
4. **URL Extraction & Candidate Evaluation:**
   - Candidate HTTP/HTTPS URLs are extracted via conservative regex (`https?://[^\s<>\"']+`) with trailing sentence punctuation (`.,;:!?)]>\"'”’`) stripped.
   - Bare domains (e.g. `example.com`) are deliberately treated as natural text (0 candidates).
   - If candidate URLs == 0: Replies with `"Please send one public job-posting URL to create a tailored cover letter."` (no card, no fetch).
   - If candidate URLs > 1: Replies with `"Please send only one job-posting URL at a time."` (no card, no fetch).
   - If candidate URLs == 1: Proceeds to single URL generation pipeline.
5. **Generation Pipeline:**
   - Step A: `job = await fetch_job_posting(target_url)` (fetches public posting via Jina Reader).
   - Step B: `resume = await asyncio.to_thread(load_resume)` (loads candidate resume off event loop).
   - Step C: `draft = await generate_cover_letter(job_posting=job, resume=resume)` (generates tailored letter using factual resume data).
   - Step D: `store_session_draft(sender, draft.text, resume.name, job.source_url)` (saves active session draft).
   - Step E: Sends letter text as plain chat `TextContent`.
   - Step F: Sends interactive `ReviewCardPayload` with `Applicant`, `Position`, `Status: Draft Ready`, and `Generate PDF` CTA.

### 2. Session-State Design & Limitations
- **In-Memory Store:** `_ACTIVE_DRAFTS` is a module-level dictionary mapping `sender` address to `SessionDraft` (`draft_id`, `letter_text`, `applicant_name`, `source_url`, `created_at`).
- **Restart Limitation:** Sessions are strictly in-memory. All session drafts are lost when the agent process restarts; no database or disk persistence is used.
- **Sender Isolation:** Lookup is strictly keyed by the inbound sender address. Sender B cannot access or deliver Sender A's draft, even if supplying Sender A's `draft_id`.
- **Capacity Bound:** Store is bounded by `MAX_SESSIONS = 1000`. When capacity is reached, the oldest session by `created_at` timestamp is evicted.

### 3. Error-to-Message Mapping
- `JobFetchError` (and subclasses: invalid URL, timeout, HTTP error, empty content): returns `exc.user_message`.
- `CoverLetterError` (and subclasses: missing job/resume content, LLM timeout/error, malformed response): returns `exc.user_message`.
- Resume load failure: returns `"Applicant resume data is missing or incomplete. Please verify your resume configuration."`.
- Unexpected exceptions: logs error class name only and returns `"Something went wrong creating your cover letter. Please try again."`.
- Exception messages, full prompts, resume facts, raw job text, and API keys are NEVER sent to the user or logged.

### 4. Card Presentation & Position Title
- **Fields Shown:** `Applicant` (applicant name from `Resume.name`), `Position` (safe neutral fallback `"Tailored cover letter"`), and `Status` (`"Draft Ready"`).
- **Why Position Title is Not Extracted:** `JobPostingContent` contains only `source_url` and `content`; it does not have a structured job title field. Extracting a title would require an extra LLM call or brittle heuristic parsing, which is out of scope. Automatic position title extraction is deferred to future work.
- Field text is sanitized with `_clean_card_text` to strip control characters and cap at 100 characters.

### 5. Verified Facts
- `uagents_core` chat protocol classes (`ChatMessage`, `TextContent`, `MetadataContent`, `ChatAcknowledgement`, `ResourceContent`) verified from installed package source (`.venv/Lib/site-packages/uagents_core/`).
- `fpdf2` (2.8.9) core font encoding: standard Helvetica requires latin-1. Unicode smart quotes and dashes are mapped to ASCII equivalents and sanitized with `.encode("latin-1", "replace")` before PDF rendering.
- `asyncio.to_thread` usage: synchronous functions `load_resume` and `_http_get` are dispatched to worker threads to prevent event loop blocking.
- # TODO: verify against docs if ASI:One specifies a maximum per-message character limit for plain text chat bubbles.

### 6. Verification Status (Not Tested Live)
- **NOT TESTED LIVE:** Live ASI:One chat interaction on `asi1.ai` with user account.
- **NOT TESTED LIVE:** Live Jina Reader API network calls with public URLs.
- **NOT TESTED LIVE:** Live ASI:One LLM API completion calls with real credentials.
- **NOT TESTED LIVE:** Live Agentverse PDF upload and delivery with real Agentverse API key.

---

## I. Phase 6: PDF Layout & Closing Block Cohesion

### 1. Root Cause & Observed Issue
- **Observed Live Bug:** In multi-page letters or letters near the page boundary, the closing would split across pages, leaving `"Sincerely,"` at the bottom of page 1 and the applicant's name at the top of page 2.
- **Root Cause:** In Phase 5, the entire letter was passed to a single `pdf.multi_cell(0, 8, safe_text)` call in `render_cover_letter_pdf`. Because `multi_cell` triggers automatic page breaks whenever `pdf.y + line_height > page_break_trigger`, a page break occurring between the last two lines orphaned the signature.

### 2. Verified fpdf2 API & Documentation
- **Installed Package:** `fpdf2==2.8.9` (verified via `pip show fpdf2` and inspecting `.venv/Lib/site-packages/fpdf/`).
- **Official Documentation URL:** https://py-pdf.github.io/fpdf2/PageBreaks.html (accessed 2026-10-24).
- **Verified API Signatures:**
  - `FPDF.will_page_break(self, height: float) -> bool`: returns `True` if adding `height` at the current `pdf.y` would exceed `self.page_break_trigger`.
  - `FPDF.unbreakable(self) -> Generator[FPDFRecorder, None, None]`: context manager that records rendering operations to `FPDFRecorder` and rewinds and calls `_perform_page_break()` if content would exceed the page boundary or trigger a break.
  - Verified that operations inside `with pdf.unbreakable() as doc:` must be invoked on the yielded recorder `doc` (not the outer `pdf`) so calls are properly captured and replayed if a page jump occurs.

### 3. Implementation Design
- **Closing Identification Rule:** A simple deterministic rule splits the sanitized text into body text and closing text. The closing block begins at the second-to-last non-empty line (the sign-off) and includes the applicant name and any spacing between them.
- **Page Jump Check:** If body text is present, closing height is calculated as `(len(closing_lines) + 1) * line_height`. If `pdf.will_page_break(closing_height)` is true, `pdf.add_page()` is called before rendering the closing block. Otherwise, `pdf.ln(line_height)` is called to preserve the paragraph gap on the current page.
- **Unbreakable Guard:** The closing block is rendered inside `with pdf.unbreakable() as doc:` as an absolute guarantee against splitting across pages, even if applicant names or titles wrap onto multiple lines.

---

## J. Phase 7: Public-User Resume Intake, Session Isolation, and Data Flow

### 1. Data Flow (Code-Verified)
- **Resume Text to LLM:** Resume text submitted via `resume:` is stored in `Resume.summary`. During job URL processing, this text is serialized via `serialize_resume()` and placed into the `<resume_data>` prompt block sent directly to the configured OpenAI-compatible LLM endpoint (`LLM_BASE_URL`).
- **Job URL to Jina Reader:** The extracted job-posting URL is forwarded over HTTPS to Jina Reader (`https://r.jina.ai/{url}`) to extract clean markdown job content.
- **Messages via ASI:One / Agentverse Mailbox:** Inbound and outbound `ChatMessage`, `ChatAcknowledgement`, and `MetadataContent` review cards transit the Fetch.ai Agentverse mailbox service when `USE_MAILBOX=true` is enabled.
- **Third-Party Provider Policies:**
  - Jina Reader API retention and privacy: `# TODO: verify against docs`
  - LLM Provider (e.g. OpenAI/Anthropic/ASI API) data retention and prompt logging: `# TODO: verify against docs`
  - Fetch.ai Agentverse mailbox transit encryption and message retention: `# TODO: verify against docs`

### 2. Verified In-Memory Store & Session Isolation
- **Process Memory Only:** Resumes and active drafts are maintained strictly in process memory (`_RESUME_STORE` and `_ACTIVE_DRAFTS`). No files, SQLite, or cloud databases are used. All user session data is lost immediately on agent process restart.
- **Bounded Eviction:** Both stores are capped at 1,000 sessions (`MAX_RESUME_SESSIONS = 1000`, `MAX_SESSIONS = 1000`). Eviction drops the oldest entry by monotonic timestamp. `_clear_session(sender)` guarantees that evicting a resume also evicts that sender's draft, preventing orphan drafts.
- **Strict Isolation:** Lookups are keyed exclusively by the sender's address string (`sender`). Foreign draft IDs provided by a different sender are rejected with a safe message.
- **Race Guard:** A monotonic version integer tracks updates to a sender's stored resume. If a sender submits a new resume or issues `clear_resume` while an async LLM or Jina request is in flight, the completed generation is discarded silently and the user is requested to resend the job URL.

### 3. Removal of Shared-Resume Fallback
- `load_resume` is completely unhooked from the chat handler (`handle_chat_message`). The agent never reads `resume.local.json` or `resume.example.json` during chat execution. If a sender has not submitted a resume via `resume:`, all job URLs and chat messages are gated by the resume-presence prompt.

### 4. Verification Status (Not Tested Live)
- **NOT TESTED LIVE:** Multi-user live concurrency in ASI:One production chat.
- **NOT TESTED LIVE:** Real-world provider data retention and privacy policies (`# TODO: verify against docs`).

---

## K. Phase 8: Inbound Attachments / PDF Resume Bytes (2026-10-24)

### 1. Verified chat protocol shapes (package source)
*Source: `.venv/Lib/site-packages/uagents_core/contrib/protocols/chat/__init__.py` (uagents-core 0.4.12), accessed 2026-10-24.*

- Inbound `ChatMessage.content` may include `ResourceContent`.
- `ResourceContent`: `type="resource"`, `resource_id: UUID4`, `resource: Resource | list[Resource]`.
- `Resource`: **`uri: str`** and **`metadata: dict[str, str]` only**. There is **no** inline bytes / base64 content field on `Resource` or `ResourceContent`.
- Conventional metadata keys documented on `Metadata` TypedDict: `mime_type`, `role` (e.g. `"attachment"`). Full mime list referenced as package `docs/mime_types.md` (not re-verified here).

### 2. Verified outbound Agentverse storage API (package source)
*Source: `.venv/Lib/site-packages/uagents_core/storage.py`, accessed 2026-10-24.*

- `ExternalStorage.download(self, asset_id: str) -> dict` — **synchronous** `requests.get` to `{storage_url}/assets/{asset_id}/contents/` with Bearer/Agent auth. Timeout 10s in package source.
- `ExternalStorage.create_asset` / `upload` / `set_permissions` remain the outbound PDF delivery APIs used by this project (Phase 2/5).
- This download API is keyed by **`asset_id`**, not by an arbitrary HTTPS URL.

### 3. Verified ASI:One inbound attachment behavior (official docs)
*Source: [Image Analysis Agent](https://innovationlab.fetch.ai/resources/docs/examples/chat-protocol/image-analysis-agent) (Innovation Lab), accessed 2026-10-24.*

- ASI:One Chat may forward user attachments as `ResourceContent` carrying an **`http`/`https` URL** (URI or metadata keys such as `url` / `uri` / `source` / `image_url` in the sample).
- Quote from that page: the agent **“does not download the file from Agent Storage; Chat Interface storage is opaque to the agent.”**
- The official sample passes the HTTPS URL to a multimodal LLM; it does **not** use `ExternalStorage.download` for inbound user uploads.
- Official sample advertises `"attachments": "true"` via session metadata for image agents.

### 4. Platform limitation for this product (decision)
- **VERIFIED:** Protocol can *detect* inbound `ResourceContent`.
- **NOT VERIFIED:** A safe, documented way for this agent to obtain **PDF file bytes** for local text extraction without either (a) inventing a generic HTTPS download of sender-supplied URIs (SSRF risk; contradicts opaque-storage guidance) or (b) assuming inbound URIs are Agentverse `asset_id`s that `ExternalStorage.download` can read (not stated in official inbound attachment docs).
- **ASSUMED / unverified:** Whether ASI:One ever sends PDF attachments to arbitrary mailbox agents as `agent-storage://…` URIs readable with the agent’s API token. `# TODO: verify against docs / live ASI:One PDF attach experiment`.
- **Product decision for Phase 8:** Do **not** implement PDF parsing or URI fetching. On any inbound `ResourceContent`, reply with the fixed guidance string and require pasted `resume:` text. Make zero Jina/LLM/PDF-writer/storage-upload calls on that path.

### 5. Privacy note for LLM prompts (Phase 8)
- `serialize_resume()` omits email, phone, LinkedIn, and GitHub from the LLM prompt. Name and qualifications remain. Resume data still lives only in process memory until restart (Phase 7).

### 6. Data flow (updated)
- Pasted `resume:` text → in-memory `_RESUME_STORE` → (on later job URL) `serialize_resume` → LLM provider.
- PDF file bytes are **not** ingested by this agent in Phase 8.
- Generated cover-letter PDFs may still be uploaded outbound via `ExternalStorage` when configured (unchanged Phase 2/5 delivery).

### 7. Outbound delivery retry (2026-10-24)

- **Official docs:** [uAgents communication guide](https://uagents.fetch.ai/docs/guides/communication), accessed 2026-10-24. It documents `await ctx.send(...)` for agent-to-agent replies.
- **Installed source verified:** `uagents==0.26.1` `Context.send()` returns a `MsgStatus`; its delivery status can be `FAILED`. The internal dispenser reports an HTTP 504 as a failed delivery status rather than raising it into the handler.
- **Product handling:** outbound chat sends retry up to three times with asynchronous 1- and 2-second backoff. Only delivery status and exception class are logged; no resume, letter, or message content is logged.

