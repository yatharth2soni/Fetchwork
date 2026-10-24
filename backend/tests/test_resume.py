"""Tests for Resume model validation and loader fallback."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.data.resume import load_resume
from app.models import Resume


MINIMAL_RESUME = {
    "name": "Alex Example",
    "skills": ["Python"],
}


def test_resume_model_requires_name() -> None:
    with pytest.raises(ValidationError):
        Resume.model_validate({"skills": ["Python"]})


def test_resume_model_accepts_optional_contact_and_sections() -> None:
    resume = Resume.model_validate(
        {
            "name": "Alex Example",
            "email": "alex.example@example.com",
            "phone": "+1-555-0100",
            "location": "Austin, TX",
            "linkedin": "https://www.linkedin.com/in/alex-example",
            "github": "https://github.com/alex-example",
            "summary": "Student developer.",
            "education": [
                {
                    "institution": "Example State University",
                    "degree": "B.S.",
                    "field": "Computer Science",
                }
            ],
            "experience": [
                {
                    "company": "Campus Tech Lab",
                    "title": "Intern",
                    "bullets": ["Wrote tests"],
                }
            ],
            "projects": [
                {
                    "name": "Course Planner CLI",
                    "technologies": ["Python"],
                }
            ],
            "skills": ["Python", "pytest"],
        }
    )
    assert resume.name == "Alex Example"
    assert resume.email == "alex.example@example.com"
    assert len(resume.education) == 1
    assert len(resume.experience) == 1
    assert len(resume.projects) == 1
    assert "Python" in resume.skills


def test_load_resume_uses_example_when_local_absent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    example = tmp_path / "resume.example.json"
    local = tmp_path / "resume.local.json"
    example.write_text(json.dumps(MINIMAL_RESUME), encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="app.data.resume"):
        resume = load_resume(local_path=local, example_path=example)

    assert resume.name == "Alex Example"
    assert any("example" in record.message.lower() for record in caplog.records)
    # Do not log full resume payload.
    assert "alex.example@example.com" not in caplog.text


def test_load_resume_prefers_local_when_present(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    example = tmp_path / "resume.example.json"
    local = tmp_path / "resume.local.json"
    example.write_text(
        json.dumps({"name": "Alex Example", "skills": ["Python"]}),
        encoding="utf-8",
    )
    local.write_text(
        json.dumps({"name": "Local Person", "skills": ["Go"]}),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="app.data.resume"):
        resume = load_resume(local_path=local, example_path=example)

    assert resume.name == "Local Person"
    assert resume.skills == ["Go"]
    assert not any("example" in record.message.lower() for record in caplog.records)


def test_packaged_example_resume_loads() -> None:
    """Integration check against the committed Alex Example fixture."""
    from app.data.resume import EXAMPLE_RESUME_PATH, LOCAL_RESUME_PATH

    if LOCAL_RESUME_PATH.is_file():
        pytest.skip("resume.local.json present; skip example-only assert")

    resume = load_resume()
    assert resume.name == "Alex Example"
    assert EXAMPLE_RESUME_PATH.is_file()
