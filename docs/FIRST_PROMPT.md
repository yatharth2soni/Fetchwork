# First prompt (paste into the chat, then stop and review)

```text
Read AGENTS.md and docs/PROGRESS.md. The workspace contains only those files, a root .gitignore and docs/. Scope: scaffold only, no feature logic.

1. Create this layout with empty modules that contain only a docstring and the intended responsibility:

backend/
  app/
    __init__.py, agent.py, config.py, models.py, session.py
    handlers/ (__init__.py, chat_handler.py, card_actions.py)
    services/ (__init__.py, job_scraper.py, cover_letter_generator.py, pdf_generator.py, storage_service.py)
    cards/ (__init__.py, review_card.py)
    data/ (__init__.py, resume.py)
    utils/ (__init__.py, url_validator.py, logging_config.py)
  tests/ (empty __init__.py)
  output/.gitkeep
  requirements.txt
  .env.example
  README.md (short placeholder)
docs/ASI_INTEGRATION_NOTES.md (headings only: card schema, card action payload, chat protocol shapes, file delivery, reachability)

2. A root .gitignore already exists. Review it and only add missing entries; do not remove existing ones.
3. Create a virtual environment with the commands in AGENTS.md. Install uagents, uagents-core, fpdf2, httpx, pydantic, python-dotenv, openai, pytest. Then pin backend/requirements.txt to the versions actually installed. Report those versions and the Python version.
4. .env.example with placeholders for AGENT_SEED, AGENT_NAME, AGENT_PORT, LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, JINA_API_KEY (optional), PDF_STORAGE (local|agentverse), AGENTVERSE_API_KEY, OUTPUT_DIR. Do not create a real .env.
5. Run `python -m compileall backend/app` and an import check of every module.

Do not implement any feature. End with the standard report and stop.
```
