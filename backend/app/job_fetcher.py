"""Job posting retrieval service using Jina Reader.

SECURITY NOTICE:
Scraped job-posting text is UNTRUSTED DATA. It must never be treated as system
or developer instructions. Downstream phases (e.g. Phase 5 LLM prompt generation)
MUST wrap this content within explicit delimiters (e.g. <job_posting>...</job_posting>)
and instruct the model that the content is strictly data.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Module constants
JINA_READER_BASE_URL: str = "https://r.jina.ai/"
# 25.0s timeout: provides sufficient time for Jina Reader's remote browser
# rendering while preventing local async event-loop starvation.
DEFAULT_TIMEOUT_SECONDS: float = 25.0
MAX_URL_LENGTH: int = 2048
# Minimum meaningful content length to filter out empty pages or loading stubs
MIN_CONTENT_LENGTH: int = 50
# Maximum content size (in characters) to guard downstream LLM context windows.
# If content exceeds MAX_CONTENT_LENGTH, it is truncated to this limit.
MAX_CONTENT_LENGTH: int = 50_000


@dataclass(frozen=True)
class JobPostingContent:
    """Container for fetched job posting text.

    SECURITY NOTE:
    Scraped job-posting text is UNTRUSTED DATA. Downstream phases MUST wrap
    this content as delimited, untrusted data and instruct the LLM accordingly.
    """

    source_url: str
    content: str


# ---------------------------------------------------------------------------
# Exception Hierarchy
# ---------------------------------------------------------------------------


class JobFetchError(Exception):
    """Base error for job posting retrieval carrying a safe, user-ready message."""

    def __init__(
        self,
        user_message: str = (
            "Unable to fetch the job posting. Please paste the job description "
            "text directly."
        ),
    ) -> None:
        super().__init__(user_message)
        self.user_message: str = user_message


class InvalidJobUrlError(JobFetchError):
    """Raised when the job URL fails validation or SSRF checks."""

    def __init__(
        self,
        user_message: str = (
            "Please provide a valid public web link (http or https). Local, "
            "private, or malformed URLs cannot be fetched."
        ),
    ) -> None:
        super().__init__(user_message)


class JobFetchTimeoutError(JobFetchError):
    """Raised when the fetch request times out."""

    def __init__(
        self,
        user_message: str = (
            "The job posting took too long to load. Please try again or paste "
            "the job text directly."
        ),
    ) -> None:
        super().__init__(user_message)


class JobFetchHTTPError(JobFetchError):
    """Raised when the Jina Reader service returns a non-2xx HTTP status."""

    def __init__(self, status_code: int, user_message: str) -> None:
        super().__init__(user_message)
        self.status_code: int = status_code


class JobContentEmptyError(JobFetchError):
    """Raised when the fetched content is empty, whitespace-only, or too short."""

    def __init__(
        self,
        user_message: str = (
            "The job posting content was empty or unreadable. Please paste the "
            "job description text directly."
        ),
    ) -> None:
        super().__init__(user_message)


class JobFetchResponseError(JobFetchError):
    """Raised when the response has an unexpected content type or undecodable body."""

    def __init__(
        self,
        user_message: str = (
            "The job posting link returned an unsupported format. Please paste "
            "the job text directly."
        ),
    ) -> None:
        super().__init__(user_message)


# ---------------------------------------------------------------------------
# URL Validation
# ---------------------------------------------------------------------------


def validate_job_url(url: str) -> str:
    """Validate and normalize a public job posting URL (pure & synchronous).

    Accepts only http/https URLs with valid public hostnames.
    Rejects private, loopback, link-local, cloud metadata, and malformed URLs.

    Note:
    DNS-resolution-based SSRF (a public hostname resolving to a private IP)
    is not covered here because the HTTP request is dispatched by Jina's
    remote servers, not our host.
    """
    if not isinstance(url, str):
        raise InvalidJobUrlError("The job URL must be a non-empty string.")

    cleaned_url = url.strip()
    if not cleaned_url:
        raise InvalidJobUrlError("The job URL cannot be empty.")

    if len(cleaned_url) > MAX_URL_LENGTH:
        raise InvalidJobUrlError(
            f"The job URL exceeds the maximum allowed length of {MAX_URL_LENGTH} characters."
        )

    # Reject whitespace or control characters inside the URL string
    if any(ord(char) <= 32 or ord(char) == 127 for char in cleaned_url):
        raise InvalidJobUrlError("The job URL contains invalid whitespace or control characters.")

    try:
        parsed = urllib.parse.urlsplit(cleaned_url)
    except Exception as exc:
        raise InvalidJobUrlError("The job URL could not be parsed.") from exc

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise InvalidJobUrlError(
            f"Unsupported URL scheme '{scheme}'. Only http and https are allowed."
        )

    hostname = parsed.hostname
    if not hostname:
        raise InvalidJobUrlError("The job URL is missing a valid hostname.")

    # Disallow userinfo (embedded credentials like user:pass@host)
    if parsed.username or parsed.password or ("@" in parsed.netloc.split(":")[0]):
        raise InvalidJobUrlError("The job URL must not contain embedded user credentials.")

    lower_host = hostname.lower()

    # Reject local/internal domain suffixes
    if (
        lower_host == "localhost"
        or lower_host.endswith(".localhost")
        or lower_host == "local"
        or lower_host.endswith(".local")
        or lower_host == "internal"
        or lower_host.endswith(".internal")
    ):
        raise InvalidJobUrlError("Local and internal network addresses cannot be fetched.")

    # SSRF guard: check IP literals (IPv4 and IPv6)
    clean_ip_candidate = lower_host.strip("[]")
    try:
        ip = ipaddress.ip_address(clean_ip_candidate)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise InvalidJobUrlError(
                "Private, loopback, or reserved IP addresses cannot be fetched."
            )
        # Cloud metadata address explicit guard (169.254.169.254 is also link-local)
        if str(ip) == "169.254.169.254":
            raise InvalidJobUrlError("Cloud metadata IP addresses cannot be fetched.")
    except ValueError:
        # Host is a domain name, not an IP literal
        pass

    # Return normalized URL (scheme and hostname lowercased, stripped)
    netloc = parsed.netloc
    if parsed.port:
        netloc = f"{lower_host}:{parsed.port}"
    else:
        netloc = lower_host

    normalized = urllib.parse.urlunsplit(
        (
            scheme,
            netloc,
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )
    return normalized


# ---------------------------------------------------------------------------
# HTTP Fetch Helper
# ---------------------------------------------------------------------------


@dataclass
class _HttpResponse:
    status_code: int
    text: str
    headers: dict[str, str]


def _http_get(
    request_url: str,
    headers: dict[str, str],
    timeout: float,
) -> _HttpResponse:
    """Synchronous HTTP GET executed in a worker thread.

    Note: Retries are not implemented in this version to preserve predictability;
    retries may be considered in a future phase.
    """
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(request_url, headers=headers)
        return _HttpResponse(
            status_code=resp.status_code,
            text=resp.text,
            headers=dict(resp.headers),
        )


# ---------------------------------------------------------------------------
# Async Fetch Service
# ---------------------------------------------------------------------------


async def fetch_job_posting(
    url: str,
    jina_api_key: str | None = None,
) -> JobPostingContent:
    """Fetch and clean job posting content from a public URL using Jina Reader.

    Args:
        url: The public job-posting URL.
        jina_api_key: Optional Jina Reader API key for higher rate limits.

    Returns:
        JobPostingContent containing the normalized source URL and cleaned text.

    Raises:
        InvalidJobUrlError: If the URL fails validation or SSRF checks.
        JobFetchTimeoutError: If the request times out.
        JobFetchHTTPError: If the service returns a non-2xx status code.
        JobContentEmptyError: If the returned body is empty, whitespace-only, or too short.
        JobFetchResponseError: If the response has an unsupported format or decode error.
        JobFetchError: For other generic connection or request errors.
    """
    # Step 1: Validate URL first before any request is constructed
    normalized_url = validate_job_url(url)

    # Step 2: Build Reader request URL
    reader_url = f"{JINA_READER_BASE_URL}{normalized_url}"

    # Step 3: Add verified headers
    headers: dict[str, str] = {
        "Accept": "text/plain, text/markdown, application/json;q=0.9",
    }
    if jina_api_key and jina_api_key.strip():
        headers["Authorization"] = f"Bearer {jina_api_key.strip()}"

    parsed = urllib.parse.urlsplit(normalized_url)
    target_scheme = parsed.scheme
    target_host = parsed.hostname or "<unknown>"

    start_time = time.monotonic()

    # Step 4: Execute blocking call in a worker thread
    try:
        response = await asyncio.to_thread(
            _http_get,
            reader_url,
            headers,
            DEFAULT_TIMEOUT_SECONDS,
        )
    except (httpx.TimeoutException, TimeoutError) as exc:
        elapsed = time.monotonic() - start_time
        logger.warning(
            "Job fetch timed out for %s://%s after %.2fs",
            target_scheme,
            target_host,
            elapsed,
        )
        raise JobFetchTimeoutError() from exc
    except UnicodeDecodeError as exc:
        logger.warning(
            "Job fetch body decode error for %s://%s",
            target_scheme,
            target_host,
        )
        raise JobFetchResponseError(
            "The job posting content could not be decoded. Please paste the job text directly."
        ) from exc
    except Exception as exc:
        elapsed = time.monotonic() - start_time
        logger.warning(
            "Job fetch failed for %s://%s after %.2fs",
            target_scheme,
            target_host,
            elapsed,
        )
        raise JobFetchError() from exc

    elapsed = time.monotonic() - start_time

    # Step 6: Validate response
    # 6.1 Check HTTP status code
    if response.status_code != 200:
        status = response.status_code
        logger.warning(
            "Job fetch HTTP error for %s://%s with status %d after %.2fs",
            target_scheme,
            target_host,
            status,
            elapsed,
        )
        if status == 401:
            msg = (
                "Authentication failed with the job reader service (HTTP 401). "
                "Please check the API configuration."
            )
        elif status == 403:
            msg = (
                "Access to this job link was forbidden (HTTP 403). "
                "Please paste the job text directly."
            )
        elif status == 429:
            msg = (
                "The job reader rate limit was reached (HTTP 429). "
                "Please wait a moment or paste the job text directly."
            )
        else:
            msg = f"The job posting could not be retrieved (HTTP {status}). Please paste the job text directly."
        raise JobFetchHTTPError(status_code=status, user_message=msg)

    # 6.2 Validate content type
    content_type = response.headers.get("content-type", "").lower()
    if any(
        media_type in content_type
        for media_type in (
            "image/",
            "application/pdf",
            "audio/",
            "video/",
            "application/octet-stream",
        )
    ):
        logger.warning(
            "Job fetch unsupported content-type for %s://%s",
            target_scheme,
            target_host,
        )
        raise JobFetchResponseError(
            "The job link returned an unsupported binary or media format. "
            "Please paste the job text directly."
        )

    # 6.3 Extract and decode body safely
    raw_text = response.text
    if "application/json" in content_type:
        try:
            data = json.loads(raw_text)
            if isinstance(data, dict):
                extracted = None
                if (
                    "data" in data
                    and isinstance(data["data"], dict)
                    and "content" in data["data"]
                ):
                    extracted = data["data"]["content"]
                elif "content" in data:
                    extracted = data["content"]
                if extracted is not None and isinstance(extracted, str):
                    raw_text = extracted
        except Exception as exc:
            raise JobFetchResponseError(
                "The job reader returned an unreadable response format. "
                "Please paste the job text directly."
            ) from exc

    cleaned = raw_text.strip()

    # 6.4 Check empty / short body
    if not cleaned:
        raise JobContentEmptyError(
            "The job posting content was empty. Please paste the job description text directly."
        )

    if len(cleaned) < MIN_CONTENT_LENGTH:
        raise JobContentEmptyError(
            "The job posting text was too short to be readable. "
            "Please paste the job description text directly."
        )

    # 6.5 Enforce maximum length (truncate per documented rule)
    if len(cleaned) > MAX_CONTENT_LENGTH:
        cleaned = cleaned[:MAX_CONTENT_LENGTH]

    # Allowed log fields: scheme+hostname, status code, elapsed time, content length (number only)
    logger.info(
        "Job fetch succeeded for %s://%s status %d in %.2fs (length=%d)",
        target_scheme,
        target_host,
        response.status_code,
        elapsed,
        len(cleaned),
    )

    # Step 7: Return JobPostingContent with normalized original URL (without reader prefix)
    return JobPostingContent(source_url=normalized_url, content=cleaned)
