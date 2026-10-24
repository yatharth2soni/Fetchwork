"""Load the applicant resume used for cover-letter generation."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.models import Resume

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent
LOCAL_RESUME_PATH = DATA_DIR / "resume.local.json"
EXAMPLE_RESUME_PATH = DATA_DIR / "resume.example.json"


def resume_paths() -> tuple[Path, Path]:
    """Return (local_path, example_path) under ``backend/app/data/``."""
    return LOCAL_RESUME_PATH, EXAMPLE_RESUME_PATH


def load_resume(
    *,
    local_path: Path | None = None,
    example_path: Path | None = None,
) -> Resume:
    """
    Load resume JSON.

    Prefer ``resume.local.json`` when present; otherwise ``resume.example.json``.
    Logs a warning when the example file is used. Does not log resume contents.
    """
    local = local_path if local_path is not None else LOCAL_RESUME_PATH
    example = example_path if example_path is not None else EXAMPLE_RESUME_PATH

    if local.is_file():
        path = local
        using_example = False
    elif example.is_file():
        path = example
        using_example = True
        logger.warning(
            "resume.local.json not found; using resume.example.json "
            "(fictional Alex Example data)"
        )
    else:
        raise FileNotFoundError(
            f"No resume file found at {local} or {example}"
        )

    data = json.loads(path.read_text(encoding="utf-8"))
    resume = Resume.model_validate(data)
    if using_example:
        logger.debug("Loaded example resume for name=%s", resume.name)
    else:
        logger.debug("Loaded local resume (contents omitted)")
    return resume
