"""Load and validate runtime settings from environment variables."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import BaseModel, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

# backend/app/config.py -> backend/
BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = BACKEND_DIR / ".env"

# Must match the safe placeholder in backend/.env.example (never a real seed).
EXAMPLE_AGENT_SEED = "change-me-agent-seed"
EXAMPLE_LLM_API_KEY = "change-me-llm-api-key"

PdfStorageMode = Literal["local", "agentverse"]


class ConfigError(ValueError):
    """Raised when required settings are missing or invalid for the selected mode."""


class Settings(BaseModel):
    """Typed runtime settings. LLM fields are OpenAI-compatible (base URL, key, model)."""

    agent_seed: str = ""
    agent_name: str
    agent_port: int = Field(ge=1, le=65535)
    llm_base_url: str
    llm_api_key: str = ""
    llm_model: str
    jina_api_key: str | None = None
    pdf_storage: PdfStorageMode
    agentverse_api_key: str | None = None
    output_dir: Path

    @field_validator("jina_api_key", "agentverse_api_key", mode="before")
    @classmethod
    def _empty_optional_to_none(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value


def default_env_path() -> Path:
    """Return ``backend/.env`` resolved from this module's location (not cwd)."""
    return DEFAULT_ENV_PATH


def _require_nonempty(raw: dict[str, str | None], key: str) -> str:
    value = (raw.get(key) or "").strip()
    if not value:
        raise ConfigError(f"Missing required setting: {key}")
    return value


def _parse_raw(raw: dict[str, str | None]) -> Settings:
    agent_name = _require_nonempty(raw, "AGENT_NAME")
    port_str = _require_nonempty(raw, "AGENT_PORT")
    try:
        agent_port = int(port_str)
    except ValueError as exc:
        raise ConfigError(
            f"Invalid AGENT_PORT {port_str!r}: expected an integer"
        ) from exc

    llm_base_url = _require_nonempty(raw, "LLM_BASE_URL")
    llm_model = _require_nonempty(raw, "LLM_MODEL")
    pdf_storage = _require_nonempty(raw, "PDF_STORAGE").lower()
    if pdf_storage not in ("local", "agentverse"):
        raise ConfigError(
            "Invalid PDF_STORAGE: expected 'local' or 'agentverse', "
            f"got {pdf_storage!r}"
        )

    output_dir_str = _require_nonempty(raw, "OUTPUT_DIR")
    output_dir = Path(output_dir_str)
    if not output_dir.is_absolute():
        output_dir = (BACKEND_DIR / output_dir).resolve()

    agentverse_api_key = (raw.get("AGENTVERSE_API_KEY") or "").strip() or None
    if pdf_storage == "agentverse" and not agentverse_api_key:
        raise ConfigError(
            "PDF_STORAGE=agentverse requires AGENTVERSE_API_KEY to be set"
        )

    try:
        return Settings(
            agent_seed=(raw.get("AGENT_SEED") or "").strip(),
            agent_name=agent_name,
            agent_port=agent_port,
            llm_base_url=llm_base_url,
            llm_api_key=(raw.get("LLM_API_KEY") or "").strip(),
            llm_model=llm_model,
            jina_api_key=(raw.get("JINA_API_KEY") or "").strip() or None,
            pdf_storage=pdf_storage,  # type: ignore[arg-type]
            agentverse_api_key=agentverse_api_key,
            output_dir=output_dir,
        )
    except ValidationError as exc:
        raise ConfigError(f"Invalid settings: {exc}") from exc


def _emit_startup_warnings(settings: Settings) -> None:
    """Warn on example seed or missing LLM key. Never log secret values."""
    seed = settings.agent_seed
    if not seed or seed == EXAMPLE_AGENT_SEED:
        logger.warning(
            "AGENT_SEED is missing or still the example placeholder; "
            "replace it before deploying"
        )
    key = settings.llm_api_key
    if not key or key == EXAMPLE_LLM_API_KEY:
        logger.warning(
            "LLM_API_KEY is missing or still the example placeholder; "
            "LLM calls will fail until a real key is configured"
        )


def load_settings(env_file: Path | None = None) -> Settings:
    """
    Load settings from an env file path.

    Default path is ``backend/.env`` next to this package (independent of cwd).
    Pass ``env_file`` in tests to point at a temporary file.
    """
    path = Path(env_file) if env_file is not None else default_env_path()
    if not path.is_file():
        raise ConfigError(f"Env file not found: {path}")

    raw = dotenv_values(path)
    settings = _parse_raw(raw)
    _emit_startup_warnings(settings)
    return settings
