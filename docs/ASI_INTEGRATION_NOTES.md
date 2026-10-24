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

### 3. Local Fallback
When `PDF_STORAGE=local` or when Agentverse API key is absent:
1. Save PDF bytes to `backend/output/<filename>.pdf`.
2. Send `TextContent` clearly stating that the file is saved locally to the machine and not attached in the chat:
   `"Cover letter PDF generated and saved locally to backend/output/<filename>.pdf (local file fallback, not a chat attachment)."`

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
