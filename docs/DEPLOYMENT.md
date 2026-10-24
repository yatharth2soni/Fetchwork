# InternAssist — Deployment & Operational Guide

Deployment readiness status: **deployment-ready pending user-managed configuration and hosting** (not deployed to production).

---

## 1. Prerequisites & Environment Setup

Run the following commands in Windows PowerShell:

```powershell
# 1. Navigate to project root
Set-Location C:\Users\soniy\Downloads\InternAssistClaude\InternAssisthello

# 2. Create Python 3.11 virtual environment if not already present
python -m venv .venv

# 3. Activate virtual environment
. .\.venv\Scripts\Activate.ps1

# 4. Install pinned project dependencies
pip install -r backend/requirements.txt
```

---

## 2. Pre-Flight Verification

Before running or deploying the agent, always run the offline deterministic test suite to verify all components:

```powershell
python -m pytest backend/tests -q
```

Expected result: **208 passed**. Ensure there are 0 failures before starting the agent.

---

## 3. Runtime Configuration (Private Environment Settings)

Configure credentials privately and manually in `backend/.env`. **Never paste API keys or agent seeds into public chats or commit them to source control.**

Only setting **names** are listed below. Set their values privately in your local environment file:

| Setting Name | Description | Example / Required Format |
|---|---|---|
| `AGENT_NAME` | Identifier for the uAgent process | `internassist_spike` |
| `AGENT_PORT` | Local port for the agent HTTP server | `8000` |
| `AGENT_SEED` | Private seed phrase for the agent identity (never commit) | Secret user-managed phrase |
| `LLM_BASE_URL` | OpenAI-compatible endpoint URL | `https://api.openai.com/v1` or provider URL |
| `LLM_API_KEY` | API key for the LLM provider | Secret user-managed key |
| `LLM_MODEL` | Target language model name | e.g. `gpt-4o-mini` |
| `JINA_API_KEY` | Optional bearer token for Jina Reader API | Jina API key or leave blank for free tier |
| `PDF_STORAGE` | Storage backend for PDF delivery | `agentverse` or `local` |
| `AGENTVERSE_API_KEY` | Fetch.ai Agentverse API key (required if `PDF_STORAGE=agentverse`) | Secret user-managed key |
| `OUTPUT_DIR` | Local directory for output PDF fallbacks | `output` (relative to `backend/`) |
| `USE_MAILBOX` | Connect via Agentverse Mailbox for ASI:One chat | `true` |

---

## 4. Hosting & Process Persistence

The agent runs as an event-driven uAgent connected to the Agentverse mailbox. To stay online and responsive to ASI:One users, it requires a persistent running process:

- **Terminal lifetime:** Running the agent in an interactive terminal window keeps it online only as long as that window remains open. Closing the terminal or logging off terminates the process and takes the agent offline.
- **System sleep/standby:** If the host machine goes into sleep or standby mode, the agent drops its mailbox connection.
- **Production hosting:** For continuous availability, run the agent process on a dedicated, persistent Windows or Linux host or container with process supervision (e.g. systemd or Windows Service wrapper).

---

## 5. Starting the Agent

Start the agent using the exact PowerShell sequence:

```powershell
Set-Location C:\Users\soniy\Downloads\InternAssistClaude\InternAssisthello
. .\.venv\Scripts\Activate.ps1
$env:PYTHONPATH = "$PWD\backend"
$env:USE_MAILBOX = "true"
python -m scripts.spike_agent
```

---

## 6. Post-Start Manual Acceptance Flow

Once the agent starts and connects to its mailbox, verify end-to-end functionality in ASI:One chat (`https://asi1.ai`):

**Resume intake note (Phase 8):** Attaching a PDF (or any file) in ASI:One is **not** supported for this agent. The agent detects inbound `ResourceContent` and replies that PDF upload is unavailable, directing the user to paste text beginning with `resume:`. Do **not** expect PDF upload/parsing to work. Pasted `resume:` text is the only supported applicant-data path.

1. **Submit Resume (paste text — required path):**
   Send the exact message:
   ```text
   resume:
   Name: Jane Example
   Skills: Python, SQL, Git
   Experience:
   - Built and tested a Python application.
   ```
2. **Confirm Acknowledgement:**
   Confirm the agent replies with:
   `"Your resume is saved for this chat only. Now send one public job-posting URL."`
   Optionally attach a PDF in the same chat and confirm the agent replies with the unsupported-upload guidance (users must still paste `resume:`); do not treat attachment success as a product feature.
3. **Send Job Posting URL:**
   Send a public job posting URL, for example:
   `https://careers.nintendo.com/jobs/4421632009/`
4. **Inspect Draft and Card:**
   - Confirm the letter text is displayed in chat and incorporates Jane Example's facts (Python, SQL, Git).
   - Confirm no "Alex Example" placeholder facts appear.
   - Confirm an interactive Review Card appears displaying Applicant: "Jane Example" and a "Generate PDF" button.
   - Click "Generate PDF" and verify the generated PDF delivers cleanly with Jane Example's tailored letter and cohesive closing block.
5. **Session Isolation Verification:**
   From a **second, different ASI:One user account/chat session** (a distinct sender address):
   - Submit a different resume for a different applicant.
   - Submit a job posting URL and confirm none of Jane Example's details or letters appear.
6. **Clear Resume:**
   In Jane's chat, send:
   `clear_resume`
   Confirm the agent replies:
   `"Your resume and active draft were cleared from this chat session."`
7. **Verify Resume Gate:**
   Send the job posting URL again. Confirm the agent replies:
   `"Please paste your resume first, beginning with \`resume:\`."`
8. **SSRF Guard Verification:**
   *Important ordering note:* Because the agent requires a resume before inspecting any URL, first submit a valid `resume:` again.
   Then send:
   `http://127.0.0.1/`
   Confirm the agent safely rejects the loopback address with a safe error message without generating a letter, review card, or PDF.

---

## 7. Data Privacy & Provider Notice

- **Session-Only Memory:** All applicant resume text and active drafts are maintained strictly in volatile process memory (`_RESUME_STORE` and `_ACTIVE_DRAFTS`). No resume data survives an agent restart, and no permanent database or file storage exists for resumes. PDF file attachments are not ingested; pasted `resume:` text is the only intake path.
- **LLM prompt privacy:** Direct contact channels (email, phone, LinkedIn, GitHub) are omitted from the serialized resume sent to the LLM; name and qualifications remain.
- **Third-Party Data Flow:**
  - Resume text is sent directly to the configured LLM provider (`LLM_BASE_URL`) within the system-delimited prompt to generate the tailored cover letter.
  - Job posting URLs are sent to Jina Reader (`https://r.jina.ai/`) to extract public job descriptions.
  - Chat messages and interactive card payloads transit the Fetch.ai Agentverse mailbox infrastructure.
- **User Advisory:** Users should be advised not to submit sensitive personal identifiers (such as government IDs, financial accounts, or private phone numbers) that they do not wish to share with third-party model providers.
- **Provider Retention Policies:**
  - Jina Reader data retention and privacy policies: `# TODO: verify against docs`
  - LLM provider data retention and model training policies: `# TODO: verify against docs`
  - Fetch.ai Agentverse message transit and retention policies: `# TODO: verify against docs`
