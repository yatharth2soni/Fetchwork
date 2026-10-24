"""Tests for typed settings loading and startup validation."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.config import (
    EXAMPLE_AGENT_SEED,
    EXAMPLE_LLM_API_KEY,
    ConfigError,
    default_env_path,
    load_settings,
)


def _write_env(path: Path, **overrides: str) -> Path:
    base = {
        "AGENT_SEED": "test-seed-not-for-production",
        "AGENT_NAME": "InternAssistTest",
        "AGENT_PORT": "8000",
        "LLM_BASE_URL": "https://api.example.com/v1",
        "LLM_API_KEY": "test-llm-key",
        "LLM_MODEL": "test-model",
        "JINA_API_KEY": "",
        "PDF_STORAGE": "local",
        "AGENTVERSE_API_KEY": "",
        "OUTPUT_DIR": "output",
    }
    base.update(overrides)
    lines = [f"{key}={value}" for key, value in base.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_default_env_path_is_backend_dotenv() -> None:
    path = default_env_path()
    assert path.name == ".env"
    assert path.parent.name == "backend"
    assert path.is_absolute()


def test_load_settings_from_explicit_path_ignores_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = _write_env(tmp_path / "custom.env", AGENT_NAME="FromTempFile")
    other = tmp_path / "other_cwd"
    other.mkdir()
    monkeypatch.chdir(other)

    settings = load_settings(env_file)

    assert settings.agent_name == "FromTempFile"
    assert settings.agent_port == 8000
    assert settings.pdf_storage == "local"
    assert settings.llm_base_url == "https://api.example.com/v1"
    assert settings.llm_model == "test-model"


def test_missing_required_key_fails_fast(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path / ".env")
    text = env_file.read_text(encoding="utf-8")
    env_file.write_text(
        text.replace("AGENT_NAME=InternAssistTest\n", "AGENT_NAME=\n"),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="AGENT_NAME"):
        load_settings(env_file)


def test_agentverse_mode_requires_api_key(tmp_path: Path) -> None:
    env_file = _write_env(
        tmp_path / ".env",
        PDF_STORAGE="agentverse",
        AGENTVERSE_API_KEY="",
    )

    with pytest.raises(ConfigError, match="AGENTVERSE_API_KEY"):
        load_settings(env_file)


def test_invalid_pdf_storage_fails(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path / ".env", PDF_STORAGE="s3")

    with pytest.raises(ConfigError, match="PDF_STORAGE"):
        load_settings(env_file)


def test_missing_env_file_fails(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.env"
    with pytest.raises(ConfigError, match="not found"):
        load_settings(missing)


def test_example_seed_emits_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    env_file = _write_env(tmp_path / ".env", AGENT_SEED=EXAMPLE_AGENT_SEED)

    with caplog.at_level(logging.WARNING, logger="app.config"):
        settings = load_settings(env_file)

    assert settings.agent_seed == EXAMPLE_AGENT_SEED
    assert any("AGENT_SEED" in record.message for record in caplog.records)
    # Never leak the seed value into log messages.
    assert EXAMPLE_AGENT_SEED not in caplog.text
    assert "test-seed" not in caplog.text


def test_missing_llm_key_emits_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    env_file = _write_env(tmp_path / ".env", LLM_API_KEY="")

    with caplog.at_level(logging.WARNING, logger="app.config"):
        load_settings(env_file)

    assert any("LLM_API_KEY" in record.message for record in caplog.records)
    assert EXAMPLE_LLM_API_KEY not in caplog.text


def test_example_llm_key_placeholder_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    env_file = _write_env(tmp_path / ".env", LLM_API_KEY=EXAMPLE_LLM_API_KEY)

    with caplog.at_level(logging.WARNING, logger="app.config"):
        load_settings(env_file)

    assert any("LLM_API_KEY" in record.message for record in caplog.records)
