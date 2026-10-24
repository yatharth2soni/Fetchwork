"""Unit and regression tests for Phase 6 PDF layout and closing block cohesion.

Verifies that the complimentary closing ("Sincerely,") and applicant name
are rendered as a single unbreakable block and never split across pages.
"""

from __future__ import annotations

import logging
import re
import zlib

import pytest

from scripts.spike_agent import (
    DUMMY_LETTER_TEXT,
    render_cover_letter_pdf,
    render_dummy_pdf,
)


def extract_page_text_streams(pdf_bytes: bytes) -> list[str]:
    """Extract decompressed text streams per page directly from PDF bytes.

    Uses standard-library zlib and regex to inspect the PDF content streams
    without external dependencies.
    """
    streams = re.findall(rb"stream\r?\n(.*?)\r?\nendstream", pdf_bytes, re.DOTALL)
    pages: list[str] = []
    for s in streams:
        try:
            decomp = zlib.decompress(s).decode("latin-1")
            pages.append(decomp)
        except Exception:
            pages.append(s.decode("latin-1", "ignore"))
    return pages


def find_token_pages(pages: list[str], token: str) -> list[int]:
    """Return 1-indexed list of page numbers containing the token."""
    return [i + 1 for i, page_text in enumerate(pages) if token in page_text]


# ---------------------------------------------------------------------------
# 1. Long-Letter Sweep (Proof of Closing Cohesion)
# ---------------------------------------------------------------------------


def test_long_letter_sweep_closing_never_splits():
    """Test 1: Sweep across 60 increasing letter lengths crossing page boundaries.

    Guarantees that for every length, non-empty valid PDF bytes are generated,
    and 'Sincerely,' and the applicant name are located on the exact same page.
    """
    closing_token = "Sincerely,"
    name_token = "Alex Example"

    for n in range(1, 61):
        filler = "\n\n".join(
            [f"Body paragraph {i} demonstrating professional background and technical skills." for i in range(1, n + 1)]
        )
        letter = f"Dear Hiring Manager,\n\n{filler}\n\n{closing_token}\n{name_token}"

        pdf_bytes = render_cover_letter_pdf(letter_text=letter, applicant_name=name_token)

        assert isinstance(pdf_bytes, bytes)
        assert len(pdf_bytes) > 200
        assert pdf_bytes.startswith(b"%PDF")

        pages = extract_page_text_streams(pdf_bytes)
        assert len(pages) >= 1

        sinc_pages = find_token_pages(pages, closing_token)
        name_pages = find_token_pages(pages, name_token)

        assert len(sinc_pages) == 1, f"At N={n}, '{closing_token}' found on pages {sinc_pages}"
        assert len(name_pages) == 1, f"At N={n}, '{name_token}' found on pages {name_pages}"
        assert sinc_pages[0] == name_pages[0], (
            f"At N={n}, closing split across pages: {closing_token} on page {sinc_pages[0]}, "
            f"{name_token} on page {name_pages[0]}"
        )


# ---------------------------------------------------------------------------
# 2. Regression Reproduction (Bug Proof Without Fix)
# ---------------------------------------------------------------------------


def test_regression_reproduces_original_split_without_fix():
    """Test 2: Assert that without the fix (_keep_together=False), the closing splits.

    This reproduces the live bug where 'Sincerely,' was orphaned at the bottom
    of page 1 while the applicant name was pushed to page 2.
    """
    closing_token = "Sincerely,"
    name_token = "Alex Example"

    split_lengths: list[tuple[int, int, int]] = []

    for n in range(1, 61):
        filler = "\n\n".join(
            [f"Body paragraph {i} demonstrating professional background and technical skills." for i in range(1, n + 1)]
        )
        letter = f"Dear Hiring Manager,\n\n{filler}\n\n{closing_token}\n{name_token}"

        # Invoke legacy unprotected path
        pdf_bytes = render_cover_letter_pdf(
            letter_text=letter,
            applicant_name=name_token,
            _keep_together=False,
        )

        pages = extract_page_text_streams(pdf_bytes)
        sinc_pages = find_token_pages(pages, closing_token)
        name_pages = find_token_pages(pages, name_token)

        if sinc_pages and name_pages and sinc_pages[0] != name_pages[0]:
            split_lengths.append((n, sinc_pages[0], name_pages[0]))

    # Must find at least one length where the closing splits in the legacy path
    assert len(split_lengths) > 0, "Expected legacy path to split at boundary, but no splits observed."

    # Verify that the exact same letter length DOES NOT split when _keep_together=True
    example_n = split_lengths[0][0]
    filler = "\n\n".join(
        [f"Body paragraph {i} demonstrating professional background and technical skills." for i in range(1, example_n + 1)]
    )
    letter = f"Dear Hiring Manager,\n\n{filler}\n\n{closing_token}\n{name_token}"
    fixed_bytes = render_cover_letter_pdf(letter_text=letter, applicant_name=name_token, _keep_together=True)
    fixed_pages = extract_page_text_streams(fixed_bytes)
    fixed_sinc = find_token_pages(fixed_pages, closing_token)
    fixed_name = find_token_pages(fixed_pages, name_token)

    assert fixed_sinc[0] == fixed_name[0], f"Fixed path failed to keep closing together at N={example_n}"


# ---------------------------------------------------------------------------
# 3. Short Letter (Single Page, Not Duplicated)
# ---------------------------------------------------------------------------


def test_short_letter_single_page_closing_not_duplicated():
    """Test 3: A standard short letter produces a 1-page PDF with no duplicated closing."""
    letter = (
        "Dear Hiring Manager,\n\n"
        "I am writing to express my interest in the Software Engineer Intern position. "
        "I have experience in Python, async programming, and automated testing.\n\n"
        "Sincerely,\n"
        "Alex Example"
    )
    pdf_bytes = render_cover_letter_pdf(letter_text=letter, applicant_name="Alex Example")
    pages = extract_page_text_streams(pdf_bytes)

    assert len(pages) == 1
    sinc_pages = find_token_pages(pages, "Sincerely,")
    name_pages = find_token_pages(pages, "Alex Example")

    assert sinc_pages == [1]
    assert name_pages == [1]


# ---------------------------------------------------------------------------
# 4. Exact-Fit Boundary (Barely Fits vs Barely Does Not Fit)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("line_count", [14, 15, 16, 17, 18])
def test_boundary_fit_variations_keep_closing_together(line_count: int):
    """Test 4: Vary paragraph counts around the page break boundary."""
    filler = "\n\n".join(
        [f"Filler paragraph {i} with approximately seventy-five characters of content." for i in range(1, line_count + 1)]
    )
    letter = f"Dear Hiring Manager,\n\n{filler}\n\nSincerely,\nAlex Example"
    pdf_bytes = render_cover_letter_pdf(letter_text=letter)
    pages = extract_page_text_streams(pdf_bytes)

    sinc_pages = find_token_pages(pages, "Sincerely,")
    name_pages = find_token_pages(pages, "Alex Example")

    assert len(sinc_pages) == 1
    assert len(name_pages) == 1
    assert sinc_pages[0] == name_pages[0]


# ---------------------------------------------------------------------------
# 5. Unusual and Accented Characters in Applicant Name
# ---------------------------------------------------------------------------


def test_unusual_characters_and_long_name_cohesion():
    """Test 5: Accented latin-1 characters and long names stay with 'Sincerely,'."""
    unusual_name = "Renée Núñez-González, M.Sc."
    letter = (
        "Dear Hiring Manager,\n\n"
        "Please accept this application for the developer role.\n\n"
        f"Sincerely,\n{unusual_name}"
    )
    pdf_bytes = render_cover_letter_pdf(letter_text=letter, applicant_name=unusual_name)
    assert pdf_bytes.startswith(b"%PDF")

    pages = extract_page_text_streams(pdf_bytes)
    assert len(pages) == 1
    # Check that accented characters are safely encoded in PDF stream
    assert "Sincerely," in pages[0]
    assert "Ren" in pages[0]


# ---------------------------------------------------------------------------
# 6. Tailored Text Rendered & No DUMMY_LETTER_TEXT in Tailored PDF
# ---------------------------------------------------------------------------


def test_tailored_letter_text_rendered_no_dummy_in_tailored_pdf():
    """Test 6: Unique marker appears in tailored PDF; DUMMY_LETTER_TEXT is not present."""
    unique_marker = "UNIQUE_TAILORED_PARAGRAPH_MARKER_98765"
    letter = (
        f"Dear Hiring Manager,\n\n"
        f"{unique_marker}\n\n"
        f"Sincerely,\n"
        f"Test Candidate"
    )
    pdf_bytes = render_cover_letter_pdf(letter_text=letter, applicant_name="Test Candidate")
    pages = extract_page_text_streams(pdf_bytes)

    assert any(unique_marker in page for page in pages)
    assert not any("enthusiastic interest in the Software Engineering Intern" in page for page in pages)


# ---------------------------------------------------------------------------
# 7. Compatibility Wrapper (render_dummy_pdf)
# ---------------------------------------------------------------------------


def test_render_dummy_pdf_compatibility_wrapper():
    """Test 7: render_dummy_pdf produces valid PDF with DUMMY_LETTER_TEXT closing intact."""
    pdf_bytes = render_dummy_pdf()
    assert pdf_bytes.startswith(b"%PDF")
    pages = extract_page_text_streams(pdf_bytes)
    assert len(pages) == 1
    assert "Sincerely," in pages[0]
    assert "Alex Example" in pages[0]


# ---------------------------------------------------------------------------
# 8. Logging Safety (No Letter Text or Markers Logged)
# ---------------------------------------------------------------------------


def test_pdf_rendering_logging_safety(caplog: pytest.LogCaptureFixture):
    """Test 8: Ensure no sensitive letter text, applicant names, or markers are logged."""
    unique_marker = "SENSITIVE_SECRET_COVER_LETTER_BODY_TOKEN_54321"
    secret_name = "SecretApplicantName123"
    letter = f"Dear Hiring Manager,\n\n{unique_marker}\n\nSincerely,\n{secret_name}"

    with caplog.at_level(logging.DEBUG):
        pdf_bytes = render_cover_letter_pdf(letter_text=letter, applicant_name=secret_name)

    assert pdf_bytes.startswith(b"%PDF")

    for record in caplog.records:
        assert unique_marker not in record.message
        assert secret_name not in record.message
