"""Thin GitHub REST client: pagination, rate-limit backoff, retries."""

from __future__ import annotations

import logging
import os
import random
import subprocess
import time
from typing import Any, Iterator
from urllib.parse import parse_qs, urlparse

import httpx

log = logging.getLogger("github_to_linkedin.github")

API_VERSION = "2022-11-28"
USER_AGENT = "github-to-linkedin-drafts/1.0"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 5


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GitHubClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.github.com",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if not token:
            raise GitHubError(
                "No GitHub token. Set GH_PAT, GITHUB_TOKEN, or GH_TOKEN."
            )
        self._base = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self._base,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": USER_AGENT,
            },
            timeout=timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self._request("GET", path, params=params)
        return response.json()

    def paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        max_pages: int = 3,
        per_page: int = 100,
    ) -> Iterator[Any]:
        query = dict(params or {})
        query.setdefault("per_page", per_page)
        url: str | None = path
        page = 0
        first = True
        while url and page < max_pages:
            response = self._request("GET", url, params=query if first else None)
            first = False
            payload = response.json()
            if isinstance(payload, list):
                yield from payload
            else:
                yield payload
                break
            url = _next_link(response.headers.get("link"))
            page += 1

    def create_issue(
        self,
        repo_full_name: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        response = self._request("POST", f"/repos/{repo_full_name}/issues", json=payload)
        return response.json()

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self._client.request(method, url, params=params, json=json)
            except httpx.HTTPError as exc:
                last_error = exc
                sleep = _backoff(attempt)
                log.warning("GitHub transport error (%s), retrying in %.1fs", exc, sleep)
                time.sleep(sleep)
                continue

            self._respect_rate_limit(response)

            if response.status_code == 304:
                return response
            if response.status_code in {429, 502, 503, 504}:
                sleep = _retry_after(response, attempt)
                log.warning(
                    "GitHub %s on %s %s, retrying in %.1fs (attempt %s/%s)",
                    response.status_code,
                    method,
                    url,
                    sleep,
                    attempt,
                    MAX_RETRIES,
                )
                time.sleep(sleep)
                continue
            if response.status_code == 403 and _is_rate_limited(response):
                sleep = _retry_after(response, attempt)
                log.warning("GitHub rate-limited, sleeping %.1fs", sleep)
                time.sleep(sleep)
                continue
            if response.status_code >= 400:
                raise GitHubError(
                    f"GitHub API {response.status_code} {method} {url}: {response.text[:500]}",
                    status=response.status_code,
                )
            return response

        raise GitHubError(f"GitHub API failed after {MAX_RETRIES} retries: {last_error}")

    def _respect_rate_limit(self, response: httpx.Response) -> None:
        remaining = response.headers.get("x-ratelimit-remaining")
        if remaining is None:
            return
        try:
            left = int(remaining)
        except ValueError:
            return
        if left <= 8:
            reset = response.headers.get("x-ratelimit-reset")
            wait = 0.0
            if reset:
                try:
                    wait = max(0.0, int(reset) - time.time())
                except ValueError:
                    wait = 5.0
            # Cap so a daily job cannot stall for a full hour.
            wait = min(wait + 1.0, 90.0)
            log.warning("GitHub rate limit remaining=%s, pausing %.1fs", left, wait)
            time.sleep(wait)


def read_token() -> str:
    """Token used to read Events / repos / compares (prefer a user PAT).

    Order: GH_PAT → GITHUB_TOKEN → GH_TOKEN → `gh auth token` (local CLI login).
    """
    for name in ("GH_PAT", "GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    token = _gh_auth_token()
    if token:
        log.info("Using GitHub token from `gh auth token` (no env token set)")
        os.environ["GITHUB_TOKEN"] = token
        return token
    return ""


def gh_username() -> str:
    """Current `gh` login, if the CLI is authenticated."""
    try:
        result = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _gh_auth_token() -> str:
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def write_token() -> str:
    """Token used to open issues in the current repo (GITHUB_TOKEN is enough in Actions)."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return (os.environ.get("GITHUB_TOKEN") or read_token()).strip()
    return read_token()


def _next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        section = part.strip()
        if 'rel="next"' not in section:
            continue
        start = section.find("<")
        end = section.find(">")
        if start != -1 and end != -1:
            url = section[start + 1 : end]
            parsed = urlparse(url)
            # httpx client expects path + query relative to base, or absolute.
            if parsed.netloc:
                return url
            return parsed.path + (f"?{parsed.query}" if parsed.query else "")
    return None


def _is_rate_limited(response: httpx.Response) -> bool:
    remaining = response.headers.get("x-ratelimit-remaining")
    if remaining == "0":
        return True
    body = response.text.lower()
    return "rate limit" in body or "secondary rate" in body


def _retry_after(response: httpx.Response, attempt: int) -> float:
    header = response.headers.get("retry-after")
    if header:
        try:
            return min(float(header), 90.0)
        except ValueError:
            pass
    reset = response.headers.get("x-ratelimit-reset")
    if reset:
        try:
            return min(max(0.0, int(reset) - time.time()) + 1.0, 90.0)
        except ValueError:
            pass
    return _backoff(attempt)


def _backoff(attempt: int) -> float:
    return min(2 ** attempt + random.random(), 30.0)


def parse_repo_full_name_from_url(url: str) -> str:
    """Extract owner/repo from https://api.github.com/repos/owner/repo."""
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if "repos" in parts:
        i = parts.index("repos")
        if len(parts) >= i + 3:
            return f"{parts[i + 1]}/{parts[i + 2]}"
    qs = parse_qs(parsed.query)
    if "repo" in qs:
        return qs["repo"][0]
    return url.rstrip("/").split("/")[-2] + "/" + url.rstrip("/").split("/")[-1]
