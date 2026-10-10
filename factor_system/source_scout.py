"""Daily, research-only discovery of potentially relevant public papers.

Crossref metadata may suggest a source to review.  It never creates a factor,
reads returns, changes ``source_catalog.json``, or grants production access.
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import fcntl
import hashlib
import json
import os
import re
import urllib.parse
from pathlib import Path
from typing import Callable

import certifi
import requests

from . import config


STATE_PATH = config.STATE_DIR / "public_source_scout.json"
INTERVAL = dt.timedelta(hours=24)
ROWS_PER_QUERY = 12
LEGACY_QUERIES = (
    "China A-share stock return predictor",
    "cross-section stock returns China asset pricing",
    "China equity anomalies factor investing",
)
FRONTIER_QUERIES = (
    "China A-share institutional ownership stock returns",
    "China A-share corporate governance stock returns",
    "China A-share customer supplier stock returns",
    "China A-share textual disclosure stock returns",
    "China A-share change of control announcement stock returns",
    "China A-share merger restructuring termination announcement stock returns",
    "China A-share government subsidy announcement stock returns",
    "China A-share major litigation arbitration announcement stock returns",
    "China A-share audit opinion auditor change stock returns",
    "China A-share ETF creation redemption constituent stock returns",
)
QUERIES = LEGACY_QUERIES + FRONTIER_QUERIES
_CROSSREF_WORKS = "https://api.crossref.org/works"
_OPENALEX_WORKS = "https://api.openalex.org/works"
_GITHUB_API = "https://api.github.com"
REPOSITORY_TARGETS = (
    {
        "repository_id": "opensourceap_signal_catalog",
        "repository": "OpenSourceAP/CrossSection",
        "path": "SignalDoc.csv",
    },
    {
        "repository_id": "opensourceap_predictor_formulas",
        "repository": "OpenSourceAP/CrossSection",
        "path": "Signals/pyCode/Predictors",
    },
    {
        "repository_id": "qlib_alpha158_loader",
        "repository": "microsoft/qlib",
        "path": "qlib/contrib/data/loader.py",
    },
)
REPOSITORY_DISCOVERY_QUERIES = (
    '"A-share" factor in:name,description language:Python archived:false',
    '"A股" 因子 in:name,description language:Python archived:false',
)
REPOSITORY_DISCOVERY_ROWS = 10
RETRY_BASE_SECONDS = 30 * 60
RETRY_MAX_SECONDS = 6 * 60 * 60
RETRY_DAILY_ATTEMPT_LIMIT = 3
_EXPECTED_SHA_UNSET = object()
_PAPER_DISCOVERY = "paper_discovery"
_REPOSITORY_DISCOVERY = "repository_discovery"
_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
_CHINA_TERMS = (
    "china", "chinese", "a share", "a shares", "a股", "中国股票", "中国股市",
    "shanghai stock", "shenzhen stock",
)
_MARKET_TERMS = (
    "stock", "return", "asset pricing", "portfolio", "a share",
    "股票", "股市", "a股", "收益", "资产定价", "投资组合",
)
_FACTOR_OUTCOME_TERMS = (
    "stock return", "stock returns", "equity return", "equity returns",
    "expected return", "expected returns", "return predict",
    "abnormal return", "abnormal returns", "asset pricing",
    "portfolio return", "portfolio returns", "market reaction",
    "market reactions", "price reaction", "price reactions",
    "factor investing", "stock anomaly", "stock anomalies",
    "equity anomaly", "equity anomalies", "a share anomaly",
    "a share anomalies", "股票收益", "股票回报", "预期收益",
    "异常收益", "资产定价", "市场反应", "价格反应", "因子投资",
)
_TITLE_MARKET_SUBJECT_TERMS = (
    "stock", "equity", "a share", "asset pricing", "portfolio",
    "investor", "securities", "market reaction", "股票", "股市", "a股",
    "投资者", "证券", "资产定价", "投资组合", "市场反应",
)
_NON_A_SHARE_TERMS = ("b share", "h share", "hong kong stock", "taiwan stock")
_NON_PUBLIC_EQUITY_TERMS = ("private equity", "venture capital")


def _now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def _stamp(value: dt.datetime) -> str:
    return value.isoformat(timespec="seconds")


def _parse_stamp(value: object) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_now().tzinfo)
    return parsed


class SourceHTTPError(RuntimeError):
    """HTTP failure carrying the provider's bounded Retry-After delay."""

    def __init__(self, status_code: int, retry_after_seconds: int | None = None):
        super().__init__(f"source HTTP {status_code}")
        self.status_code = int(status_code)
        self.retry_after_seconds = retry_after_seconds


def _retry_after_seconds(value: object, now: dt.datetime | None = None) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        return max(0, int(text))
    try:
        deadline = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=dt.timezone.utc)
    reference = now or dt.datetime.now(dt.timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=dt.timezone.utc)
    return max(0, int((deadline - reference).total_seconds()))


def _retry_key(provider: str, query: str, operation: str) -> str:
    return "\x1f".join((provider, operation, query))


def _retry_rows(state: dict) -> dict[str, dict]:
    rows = {}
    for item in state.get("retry_queue") or []:
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider") or "")
        query = str(item.get("query") or "")
        operation = str(item.get("operation") or "")
        if provider and query and operation:
            rows[_retry_key(provider, query, operation)] = dict(item)
    # Migrate the prior schema lazily.  Older state files persisted structured
    # provider/query errors but had no retry queue; they should not wait for the
    # next full daily scan after this reliability upgrade is loaded.
    attempted = _parse_stamp(state.get("last_attempt_at"))
    if attempted is not None:
        for item in state.get("errors") or []:
            if not isinstance(item, dict):
                continue
            provider = str(item.get("provider") or "")
            query = str(item.get("query") or "")
            operation = str(item.get("operation") or _PAPER_DISCOVERY)
            key = _retry_key(provider, query, operation)
            if not provider or not query or key in rows:
                continue
            server_delay = int(item.get("retry_after_seconds") or 0)
            rows[key] = {
                "provider": provider,
                "query": query,
                "operation": operation,
                "attempt_date": attempted.date().isoformat(),
                "attempts_today": 1,
                "daily_attempt_limit": RETRY_DAILY_ATTEMPT_LIMIT,
                "last_attempt_at": _stamp(attempted),
                "next_due_at": _stamp(attempted + dt.timedelta(
                    seconds=max(RETRY_BASE_SECONDS, server_delay),
                )),
                "retry_after_seconds": server_delay or None,
                "last_error": str(item.get("error") or "")[:300],
            }
    return rows


def _due_retry_rows(state: dict, now: dt.datetime) -> list[dict]:
    output = []
    for row in _retry_rows(state).values():
        due_at = _parse_stamp(row.get("next_due_at"))
        if due_at is not None and now >= due_at:
            output.append(row)
    return output


def _full_scan_due(state: dict, now: dt.datetime) -> bool:
    last = _parse_stamp(
        state.get("last_full_attempt_at") or state.get("last_attempt_at")
    )
    return last is None or now - last >= INTERVAL


def _source_error(
    provider: str, query: str, operation: str, exc: Exception,
) -> dict:
    row = {
        "provider": provider,
        "query": query,
        "operation": operation,
        "error": f"{type(exc).__name__}: {exc}"[:300],
    }
    delay = getattr(exc, "retry_after_seconds", None)
    if isinstance(delay, int) and delay >= 0:
        row["retry_after_seconds"] = delay
    return row


def _without_task_error(
    errors: list[dict], provider: str, query: str, operation: str,
) -> list[dict]:
    return [
        item for item in errors
        if not (
            isinstance(item, dict)
            and item.get("provider") == provider
            and item.get("query") == query
            and item.get("operation", _PAPER_DISCOVERY) == operation
        )
    ]


def _record_retry_failure(
    queue: dict[str, dict], error: dict, now: dt.datetime,
) -> None:
    provider = str(error["provider"])
    query = str(error["query"])
    operation = str(error["operation"])
    key = _retry_key(provider, query, operation)
    prior = queue.get(key) or {}
    today = now.date().isoformat()
    attempts = (
        int(prior.get("attempts_today") or 0) + 1
        if prior.get("attempt_date") == today else 1
    )
    server_delay = int(error.get("retry_after_seconds") or 0)
    backoff = min(
        RETRY_MAX_SECONDS,
        RETRY_BASE_SECONDS * (2 ** max(0, attempts - 1)),
    )
    # The local backoff is bounded, but a provider's explicit Retry-After is a
    # lower bound: retrying earlier would violate the upstream contract.
    delay = max(backoff, server_delay)
    queue[key] = {
        "provider": provider,
        "query": query,
        "operation": operation,
        "attempt_date": today,
        "attempts_today": attempts,
        "daily_attempt_limit": RETRY_DAILY_ATTEMPT_LIMIT,
        "last_attempt_at": _stamp(now),
        "next_due_at": (
            _stamp(now + dt.timedelta(seconds=delay))
            if attempts < RETRY_DAILY_ATTEMPT_LIMIT else None
        ),
        "retry_after_seconds": server_delay or None,
        "last_error": str(error.get("error") or "")[:300],
    }


def _next_due_at(state: dict, queue: dict[str, dict], now: dt.datetime) -> str:
    last_full = _parse_stamp(
        state.get("last_full_attempt_at") or state.get("last_attempt_at")
    )
    deadlines = [last_full + INTERVAL if last_full is not None else now]
    deadlines.extend(
        due for row in queue.values()
        if (due := _parse_stamp(row.get("next_due_at"))) is not None
    )
    return _stamp(min(deadlines))


def due(
    runtime: dict, now: dt.datetime | None = None,
    state_path: Path | None = None,
) -> bool:
    """Run daily, or sooner for only the independently failed queries."""
    now = now or _now()
    state: dict = {}
    if state_path is not None:
        try:
            state = _load_existing(state_path)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            state = {}
    runtime_attempt = _parse_stamp(runtime.get("last_source_scout_attempt_at"))
    if _due_retry_rows(state, now):
        return (
            runtime_attempt is None
            or now - runtime_attempt >= dt.timedelta(seconds=RETRY_BASE_SECONDS)
        )
    attempts = [
        runtime_attempt,
        _parse_stamp(
            state.get("last_full_attempt_at") or state.get("last_attempt_at")
        ),
    ]
    attempted = max(
        (value for value in attempts if value is not None), default=None,
    )
    return attempted is None or now - attempted >= INTERVAL


def _normalize(value: object) -> str:
    text = str(value or "").casefold().replace("&", " and ")
    return " ".join(re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", text).split())


def _first_text(value: object) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "").strip()


def _relevant(title: str) -> bool:
    normalized = _normalize(title)
    if "a share" not in normalized and any(
        term in normalized for term in _NON_A_SHARE_TERMS
    ):
        return False
    if not any(term in normalized for term in ("stock", "a share", "股票", "股市", "a股")) and any(
        term in normalized for term in _NON_PUBLIC_EQUITY_TERMS
    ):
        return False
    return (
        any(term in normalized for term in _CHINA_TERMS)
        and any(term in normalized for term in _MARKET_TERMS)
    )


def _factor_outcome_relevant(text: str) -> bool:
    """Require an investable-return outcome, not merely an A-share sample."""
    normalized = _normalize(text)
    return any(term in normalized for term in _FACTOR_OUTCOME_TERMS)


def _title_has_market_subject(title: str) -> bool:
    """Keep abstract-scoped papers only when their title names a market subject."""
    normalized = _normalize(title)
    return any(term in normalized for term in _TITLE_MARKET_SUBJECT_TERMS)


def _doi(value: object) -> str:
    match = _DOI_RE.search(str(value or ""))
    return match.group(0).rstrip(".,;)").casefold() if match else ""


def _year(item: dict) -> int | None:
    for key in ("published", "published-print", "published-online", "issued"):
        parts = (item.get(key) or {}).get("date-parts") if isinstance(item.get(key), dict) else None
        try:
            return int(parts[0][0])
        except (IndexError, TypeError, ValueError):
            continue
    return None


def _authors(item: dict) -> list[str]:
    output = []
    for author in item.get("author") or []:
        if not isinstance(author, dict):
            continue
        name = " ".join(
            part for part in (str(author.get("given") or "").strip(),
                              str(author.get("family") or "").strip()) if part
        )
        if name:
            output.append(name)
    return output[:8]


def _identity(doi: str, title: str) -> str:
    return f"doi:{doi}" if doi else f"title:{_normalize(title)}"


def _candidate(item: dict, query: str, discovered_at: str) -> dict | None:
    title = _first_text(item.get("title"))
    if not title or not _relevant(title) or not _factor_outcome_relevant(title):
        return None
    doi = _doi(item.get("DOI"))
    identity = _identity(doi, title)
    if identity == "title:":
        return None
    return {
        "scout_id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20],
        "identity": identity,
        "doi": doi or None,
        "title": title,
        "authors": _authors(item),
        "year": _year(item),
        "container_title": _first_text(item.get("container-title")) or None,
        "url": (
            f"https://doi.org/{urllib.parse.quote(doi, safe='/')}"
            if doi else _first_text(item.get("URL")) or None
        ),
        "citation_count": int(item.get("is-referenced-by-count") or 0),
        "matched_queries": [query],
        "scope_evidence": "title",
        "source_providers": ["Crossref"],
        "first_seen_at": discovered_at,
        "status": "unreviewed_research_source",
        "license_audit_status": "not_reviewed",
        "pit_feasibility_status": "not_reviewed",
        "research_only": True,
        "runnable": False,
        "factor_values_computed": False,
        "outcomes_read": False,
        "recommendation_eligible": False,
        "production_eligible": False,
        "source_provider": "Crossref",
    }


def _merge_discovery(existing: dict, incoming: dict, query: str) -> None:
    """Keep one identity while retaining useful metadata from both providers."""
    matched = existing.setdefault("matched_queries", [])
    if query not in matched:
        matched.append(query)
    providers = existing.setdefault("source_providers", [])
    prior_provider = str(existing.get("source_provider") or "")
    if prior_provider and prior_provider not in providers:
        providers.append(prior_provider)
    for provider in incoming.get("source_providers") or [incoming.get("source_provider")]:
        if provider and provider not in providers:
            providers.append(provider)
    for key in ("openalex_id", "open_access_url"):
        if incoming.get(key) and not existing.get(key):
            existing[key] = incoming[key]
    if incoming.get("abstract_available") is True:
        existing["abstract_available"] = True
    if incoming.get("scope_evidence") == "title":
        existing["scope_evidence"] = "title"
    elif not existing.get("scope_evidence"):
        existing["scope_evidence"] = incoming.get("scope_evidence")
    existing["citation_count"] = max(
        int(existing.get("citation_count") or 0),
        int(incoming.get("citation_count") or 0),
    )


def _candidate_sort_key(item: dict) -> tuple:
    """Put unresolved research ahead of closed archive rows; never auto-admit it."""
    status = str(item.get("status") or "unreviewed_research_source")
    status_order = {
        "unreviewed_research_source": 0,
        "formula_pending": 1,
        "source_contract_pending": 2,
    }
    parsed = _parse_stamp(item.get("first_seen_at"))
    timestamp = parsed.timestamp() if parsed is not None else 0.0
    if status in status_order:
        return (
            0,
            status_order[status],
            0 if item.get("scope_evidence") == "title" else 1,
            0 if item.get("open_access_url") else 1,
            -int(item.get("citation_count") or 0),
            -timestamp,
            str(item.get("identity") or ""),
        )
    return (1, 0, 0, 0, 0, -timestamp, str(item.get("identity") or ""))


def _ordered_candidates(candidates: list[dict]) -> list[dict]:
    return sorted(candidates, key=_candidate_sort_key)


def _openalex_abstract(item: dict) -> str:
    """Rebuild an abstract only for scope screening; callers never persist it."""
    inverted = item.get("abstract_inverted_index")
    if not isinstance(inverted, dict):
        return ""
    positioned = []
    for word, positions in inverted.items():
        if not isinstance(positions, list):
            continue
        for position in positions:
            if isinstance(position, int) and position >= 0:
                positioned.append((position, str(word)))
    return " ".join(word for _, word in sorted(positioned))


def _openalex_authors(item: dict) -> list[str]:
    names = []
    for authorship in item.get("authorships") or []:
        author = authorship.get("author") if isinstance(authorship, dict) else None
        name = str((author or {}).get("display_name") or "").strip()
        if name:
            names.append(name)
    return names[:8]


def _openalex_candidate(item: dict, query: str, discovered_at: str) -> dict | None:
    title = str(item.get("display_name") or item.get("title") or "").strip()
    abstract = _openalex_abstract(item)
    title_relevant = _relevant(title) and _factor_outcome_relevant(title)
    source_text = f"{title} {abstract}"
    if (
        not title
        or (not title_relevant and not _title_has_market_subject(title))
        or not _relevant(source_text)
        or not _factor_outcome_relevant(source_text)
    ):
        return None
    doi = _doi(item.get("doi"))
    identity = _identity(doi, title)
    if identity == "title:":
        return None
    primary = item.get("primary_location") if isinstance(item.get("primary_location"), dict) else {}
    source = primary.get("source") if isinstance(primary.get("source"), dict) else {}
    open_access = item.get("open_access") if isinstance(item.get("open_access"), dict) else {}
    openalex_id = str(item.get("id") or "").strip() or None
    landing_url = str(primary.get("landing_page_url") or "").strip() or None
    return {
        "scout_id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20],
        "identity": identity,
        "doi": doi or None,
        "openalex_id": openalex_id,
        "title": title,
        "authors": _openalex_authors(item),
        "year": item.get("publication_year") if isinstance(item.get("publication_year"), int) else None,
        "container_title": str(source.get("display_name") or "").strip() or None,
        "url": (
            f"https://doi.org/{urllib.parse.quote(doi, safe='/')}"
            if doi else landing_url or openalex_id
        ),
        "open_access_url": str(open_access.get("oa_url") or "").strip() or None,
        "citation_count": int(item.get("cited_by_count") or 0),
        "matched_queries": [query],
        "scope_evidence": "title" if title_relevant else "abstract_metadata",
        "source_providers": ["OpenAlex"],
        "first_seen_at": discovered_at,
        "status": "unreviewed_research_source",
        "license_audit_status": "not_reviewed",
        "pit_feasibility_status": "not_reviewed",
        "abstract_available": bool(abstract),
        "source_provider": "OpenAlex",
        "research_only": True,
        "runnable": False,
        "factor_values_computed": False,
        "outcomes_read": False,
        "recommendation_eligible": False,
        "production_eligible": False,
    }


def _default_fetch(url: str) -> object:
    session = requests.Session()
    session.trust_env = False
    response = session.get(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "RegisOracle-ResearchSourceScout/1.0 (local research-only client)",
        },
        timeout=12,
        verify=certifi.where(),
    )
    if response.status_code != 200:
        headers = getattr(response, "headers", {}) or {}
        raise SourceHTTPError(
            response.status_code,
            _retry_after_seconds(headers.get("Retry-After")),
        )
    return response.json()


def _default_openalex_fetch(url: str) -> object:
    """Use the same verified TLS boundary for the bounded OpenAlex pass."""
    return _default_fetch(url)


def _repository_url(target: dict) -> str:
    params = urllib.parse.urlencode({
        "path": target["path"],
        "per_page": 1,
    })
    return f"{_GITHUB_API}/repos/{target['repository']}/commits?{params}"


def _repository_snapshots(
    existing: dict, *, now: dt.datetime,
    fetcher: Callable[[str], object],
) -> tuple[list[dict], list[dict], int]:
    """Check formula sources by commit identity without cloning or computing factors."""
    prior = {
        str(item.get("repository_id")): item
        for item in existing.get("repository_snapshot_results") or []
        if isinstance(item, dict) and item.get("repository_id")
    }
    checked_at = _stamp(now)
    results: list[dict] = []
    errors: list[dict] = []
    successful = 0
    for target in REPOSITORY_TARGETS:
        repository_id = str(target["repository_id"])
        previous = prior.get(repository_id) or {}
        try:
            payload = fetcher(_repository_url(target))
            if not isinstance(payload, list) or not payload:
                raise ValueError("GitHub commits response is empty")
            commit = payload[0]
            if not isinstance(commit, dict):
                raise ValueError("GitHub commit is invalid")
            sha = str(commit.get("sha") or "").strip().casefold()
            if not re.fullmatch(r"[0-9a-f]{40}", sha):
                raise ValueError("GitHub commit SHA is invalid")
            metadata = commit.get("commit") or {}
            author = metadata.get("author") or {}
            prior_sha = str(previous.get("current_commit_sha") or "")
            changed = bool(prior_sha and prior_sha != sha)
            results.append({
                "repository_id": repository_id,
                "repository": target["repository"],
                "path": target["path"],
                "source_url": f"https://github.com/{target['repository']}",
                "commit_url": str(commit.get("html_url") or "") or None,
                "current_commit_sha": sha,
                "prior_commit_sha": prior_sha or None,
                "committed_at": author.get("date"),
                "checked_at": checked_at,
                "change_status": (
                    "upstream_changed_review_required" if changed else
                    "unchanged" if prior_sha else
                    "baseline_established_not_candidate"
                ),
                "review_required": changed,
                "research_only": True,
                "runnable": False,
                "candidate_registration_allowed": False,
                "factor_values_computed": False,
                "outcomes_read": False,
                "production_eligible": False,
            })
            successful += 1
        except Exception as exc:
            results.append({
                **previous,
                "repository_id": repository_id,
                "repository": target["repository"],
                "path": target["path"],
                "checked_at": checked_at,
                "change_status": "check_unavailable",
                "review_required": bool(previous.get("review_required")),
                "research_only": True,
                "runnable": False,
                "candidate_registration_allowed": False,
                "factor_values_computed": False,
                "outcomes_read": False,
                "production_eligible": False,
            })
            errors.append({
                "provider": "GitHub",
                "repository": target["repository"],
                "path": target["path"],
                "error": f"{type(exc).__name__}: {exc}"[:300],
            })
    return results, errors, successful


def _github_source() -> dict:
    return {
        "provider": "GitHub",
        "endpoint": _GITHUB_API,
        "official_documentation": "https://docs.github.com/rest/search/search#search-repositories",
        "api_key_required_for_bounded_trial": False,
        "search_scope": "licensed public A-share factor repositories plus pinned formula-source identities",
    }


def _repository_discovery_url(query: str) -> str:
    params = urllib.parse.urlencode({
        "q": query,
        "sort": "updated",
        "order": "desc",
        "per_page": REPOSITORY_DISCOVERY_ROWS,
    })
    return f"{_GITHUB_API}/search/repositories?{params}"


def _repository_discovery_candidate(
    item: dict, query: str, discovered_at: str,
) -> dict | None:
    """Keep only licensed, public A-share factor/research repositories."""
    full_name = str(item.get("full_name") or "").strip()
    source_url = str(item.get("html_url") or "").strip()
    license_row = item.get("license") if isinstance(item.get("license"), dict) else {}
    license_spdx = str(license_row.get("spdx_id") or "").strip()
    searchable = _normalize(" ".join((
        str(item.get("name") or ""),
        str(item.get("description") or ""),
        " ".join(str(value) for value in (item.get("topics") or [])),
    )))
    has_market = any(term in searchable for term in (
        "a share", "a股", "中国股票", "china stock", "chinese stock",
    ))
    has_research = any(term in searchable for term in (
        "factor", "alpha", "因子", "quant", "量化", "asset pricing",
        "backtest", "回测",
    ))
    if (
        not full_name or "/" not in full_name or not source_url
        or item.get("fork") is True or item.get("archived") is True
        or not license_spdx or license_spdx.casefold() == "noassertion"
        or not has_market or not has_research
    ):
        return None
    identity = f"github:{full_name.casefold()}"
    return {
        "scout_id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20],
        "identity": identity,
        "repository": full_name,
        "name": str(item.get("name") or full_name.rsplit("/", 1)[-1]),
        "description": str(item.get("description") or "").strip() or None,
        "url": source_url,
        "default_branch": str(item.get("default_branch") or "").strip() or None,
        "license_spdx": license_spdx,
        "stars": int(item.get("stargazers_count") or 0),
        "forks": int(item.get("forks_count") or 0),
        "open_issues": int(item.get("open_issues_count") or 0),
        "updated_at": item.get("updated_at"),
        "pushed_at": item.get("pushed_at"),
        "topics": [str(value) for value in (item.get("topics") or [])[:12]],
        "matched_queries": [query],
        "first_seen_at": discovered_at,
        "status": "unreviewed_repository_source",
        "license_audit_status": "spdx_declared_not_yet_file_verified",
        "formula_audit_status": "not_reviewed",
        "data_provenance_status": "not_reviewed",
        "pit_feasibility_status": "not_reviewed",
        "source_provider": "GitHub",
        "research_only": True,
        "runnable": False,
        "factor_values_computed": False,
        "outcomes_read": False,
        "recommendation_eligible": False,
        "production_eligible": False,
    }


def _repository_discoveries(
    existing: dict, *, now: dt.datetime,
    fetcher: Callable[[str], object],
    queries: tuple[str, ...] | list[str] | None = None,
) -> tuple[list[dict], list[dict], int, int, int]:
    """Search a bounded licensed-repository inbox without cloning or execution."""
    prior = {
        str(item.get("identity")): dict(item)
        for item in existing.get("repository_discoveries") or []
        if isinstance(item, dict) and item.get("identity")
    }
    known_pinned = {
        str(item.get("repository") or "").casefold()
        for item in REPOSITORY_TARGETS
    }
    attempted_at = _stamp(now)
    errors: list[dict] = []
    successful = 0
    new_count = 0
    screened_out = 0
    for query in queries or REPOSITORY_DISCOVERY_QUERIES:
        try:
            payload = fetcher(_repository_discovery_url(query))
            items = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                raise ValueError("GitHub repository search items are invalid")
            successful += 1
        except Exception as exc:
            errors.append(_source_error(
                "GitHub", query, _REPOSITORY_DISCOVERY, exc,
            ))
            continue
        for item in items:
            candidate = (
                _repository_discovery_candidate(item, query, attempted_at)
                if isinstance(item, dict) else None
            )
            if candidate is None:
                screened_out += 1
                continue
            if str(candidate["repository"]).casefold() in known_pinned:
                continue
            identity = candidate["identity"]
            previous = prior.get(identity)
            if previous:
                matched = list(previous.get("matched_queries") or [])
                if query not in matched:
                    matched.append(query)
                prior[identity] = {
                    **previous,
                    **candidate,
                    "matched_queries": matched,
                    "first_seen_at": previous.get("first_seen_at") or attempted_at,
                    "status": previous.get("status") or "unreviewed_repository_source",
                }
            else:
                prior[identity] = candidate
                new_count += 1
    rows = sorted(
        prior.values(),
        key=lambda item: (
            str(item.get("status") or "") == "unreviewed_repository_source",
            int(item.get("stars") or 0),
            str(item.get("updated_at") or ""),
        ),
        reverse=True,
    )
    return rows, errors, successful, new_count, screened_out


def capture_repository_discoveries_once(
    *, state_path: Path | None = None, now: dt.datetime | None = None,
    fetcher: Callable[[str], object] | None = None,
) -> dict:
    """Refresh only the licensed GitHub inbox; never repeat paper discovery."""
    path = state_path or STATE_PATH
    now = now or _now()
    before_sha = _sha256(path) if path.exists() else None
    state = _load_existing(path)
    rows, errors, successful, new_count, screened_out = _repository_discoveries(
        state, now=now, fetcher=fetcher or _default_fetch,
    )
    if before_sha is not None and _sha256(path) != before_sha:
        raise RuntimeError("source scout state changed during repository discovery")
    sources = [
        item for item in state.get("sources") or []
        if isinstance(item, dict) and item.get("provider") != "GitHub"
    ]
    state.update({
        "repository_discoveries": rows,
        "repository_discovery_last_attempt_at": _stamp(now),
        "repository_discovery_status": (
            "ok" if successful == len(REPOSITORY_DISCOVERY_QUERIES) else
            "degraded" if successful else "unavailable"
        ),
        "repository_discovery_successful_queries": successful,
        "repository_discovery_expected_queries": len(
            REPOSITORY_DISCOVERY_QUERIES
        ),
        "repository_discovery_new_count": new_count,
        "repository_discovery_unreviewed_count": sum(
            str(item.get("status") or "") == "unreviewed_repository_source"
            for item in rows
        ),
        "repository_discovery_screened_out_count": screened_out,
        "sources": [*sources, _github_source()],
        "errors": [
            item for item in state.get("errors") or []
            if not (
                isinstance(item, dict)
                and item.get("provider") == "GitHub"
                and item.get("operation") == "repository_discovery"
            )
        ] + errors,
    })
    _write(path, state)
    return state


def capture_repository_snapshots_once(
    *, state_path: Path | None = None, now: dt.datetime | None = None,
    fetcher: Callable[[str], object] | None = None,
) -> dict:
    """Add one GitHub source snapshot without repeating paper discovery."""
    path = state_path or STATE_PATH
    now = now or _now()
    state = _load_existing(path)
    snapshots, errors, successful = _repository_snapshots(
        state, now=now, fetcher=fetcher or _default_fetch,
    )
    sources = [
        item for item in state.get("sources") or []
        if isinstance(item, dict) and item.get("provider") != "GitHub"
    ]
    state.update({
        "repository_snapshot_results": snapshots,
        "repository_last_attempt_at": _stamp(now),
        "repository_successful_checks": successful,
        "repository_expected_checks": len(REPOSITORY_TARGETS),
        "repository_status": (
            "ok" if successful == len(REPOSITORY_TARGETS) else
            "degraded" if successful else
            "unavailable"
        ),
        "repository_review_required_count": sum(
            item.get("review_required") is True for item in snapshots
        ),
        "sources": [*sources, _github_source()],
        "errors": [
            item for item in state.get("errors") or []
            if isinstance(item, dict) and item.get("provider") != "GitHub"
        ] + errors,
    })
    _write(path, state)
    return state


def _load_existing(path: Path) -> dict:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("candidates", []), list):
        raise ValueError("public source scout state is invalid")
    return value


def _known_source_identities(existing: dict) -> set[str]:
    identities = {
        str(item.get("identity")) for item in existing.get("candidates") or []
        if isinstance(item, dict) and item.get("identity")
    }
    try:
        catalog = json.loads(config.SOURCE_CATALOG.read_text(encoding="utf-8"))
        for source in catalog.get("sources") or []:
            paper = source.get("paper") if isinstance(source, dict) else None
            if not isinstance(paper, dict):
                continue
            doi = _doi(paper.get("doi") or paper.get("official_url") or paper.get("primary_url"))
            title = str(paper.get("title") or "")
            if doi or title:
                identities.add(_identity(doi, title))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    exclusion = config.PROJECT_ROOT / "research" / "FACTOR_FAMILY_EXCLUSION_AND_DATA_FRONTIER_2026-08-26.md"
    try:
        identities.update(
            f"doi:{cleaned}" for match in _DOI_RE.findall(
                exclusion.read_text(encoding="utf-8")
            ) if (cleaned := _doi(match))
        )
    except OSError:
        pass
    return identities


def _incremental_filters(existing: dict, now: dt.datetime) -> str:
    last_success = _parse_stamp(existing.get("last_success_at"))
    if last_success is None:
        return "type:journal-article,from-pub-date:1990-01-01"
    start = (last_success.date() - dt.timedelta(days=1)).isoformat()
    return f"type:journal-article,from-index-date:{start},until-index-date:{now.date().isoformat()}"


def _historical_queries_completed(existing: dict) -> set[str]:
    completed = {
        str(query) for query in existing.get("historical_queries_completed") or []
        if str(query) in QUERIES
    }
    # States written before per-query coverage existed already ran the original
    # three queries from 1990 onward.  Preserve that work instead of backfilling
    # the same metadata again.
    if "historical_queries_completed" not in existing and existing.get("last_success_at"):
        completed.update(LEGACY_QUERIES)
    return completed


def _query_filters(existing: dict, query: str, now: dt.datetime,
                   historical_completed: set[str]) -> str:
    if query not in historical_completed:
        return "type:journal-article,from-pub-date:1990-01-01"
    return _incremental_filters(existing, now)


def _url(query: str, filters: str) -> str:
    params = urllib.parse.urlencode({
        "query.bibliographic": query,
        "filter": filters,
        "rows": ROWS_PER_QUERY,
        "select": "DOI,title,author,published,container-title,URL,type,is-referenced-by-count",
    })
    return f"{_CROSSREF_WORKS}?{params}"


def _openalex_filters(existing: dict, now: dt.datetime, query: str,
                      historical_completed: set[str]) -> str:
    if query not in historical_completed:
        return "from_publication_date:1990-01-01"
    last_success = _parse_stamp(existing.get("last_success_at"))
    start = (last_success.date() - dt.timedelta(days=31)) if last_success else now.date() - dt.timedelta(days=31)
    return f"from_publication_date:{start.isoformat()},to_publication_date:{now.date().isoformat()}"


def _openalex_url(query: str, filters: str) -> str:
    params = urllib.parse.urlencode({
        "search": query,
        "filter": filters,
        "sort": "relevance_score:desc",
        "per_page": ROWS_PER_QUERY,
        "select": (
            "id,doi,display_name,publication_year,cited_by_count,"
            "abstract_inverted_index,authorships,open_access,primary_location"
        ),
    })
    return f"{_OPENALEX_WORKS}?{params}"


def _write(
    path: Path, payload: dict, *,
    expected_sha: str | None | object = _EXPECTED_SHA_UNSET,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        current_sha = _sha256(path) if path.exists() else None
        if expected_sha is not _EXPECTED_SHA_UNSET and current_sha != expected_sha:
            raise RuntimeError("source scout state changed during write")
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_TRIAGE_MUTABLE_FIELDS = {
    "status", "primary_source_url", "license_audit_status",
    "pit_feasibility_status", "review_reason", "candidate_family",
    "formula_resolution_status", "formula", "formation_window_months",
    "rebalance_frequency", "direction_status", "input_contract_gaps",
    "next_action",
}

_REPOSITORY_TRIAGE_MUTABLE_FIELDS = {
    "status", "license_audit_status", "formula_audit_status",
    "data_provenance_status", "pit_feasibility_status", "review_reason",
    "next_action",
}


def apply_manual_closed_intake(
    *, intake_path: Path, state_path: Path | None = None,
    reviewed_at: dt.datetime | None = None,
) -> dict:
    """Record one manually found primary source that already fails input feasibility."""
    path = state_path or STATE_PATH
    before_sha = _sha256(path)
    artifact = json.loads(intake_path.read_text(encoding="utf-8"))
    if artifact.get("record_type") != (
        "public_source_manual_closed_intake_without_outcome_access"
    ):
        raise ValueError("manual source intake record_type is invalid")
    if artifact.get("source_state_sha256_before_intake") != before_sha:
        raise ValueError("manual source intake state hash mismatch")
    boundary = artifact.get("audit_boundary")
    required_false = {
        "future_returns_read", "factor_values_computed", "candidate_ranks_computed",
        "ic_computed", "backtest_computed", "direction_selected_from_local_outcomes",
    }
    if not isinstance(boundary, dict) or any(
        boundary.get(key) is not False for key in required_false
    ):
        raise ValueError("manual source intake outcome-blind boundary is invalid")

    source = artifact.get("source")
    decision = artifact.get("decision")
    reasons = artifact.get("closure_reasons")
    if not isinstance(source, dict) or not isinstance(decision, dict):
        raise ValueError("manual source intake source or decision is invalid")
    title = str(source.get("title") or "").strip()
    doi = _doi(source.get("doi"))
    market_scope = str(source.get("market_scope") or "").strip()
    if not title or not doi or not _relevant(f"{title} {market_scope}"):
        raise ValueError("manual source intake identity is invalid")
    status = str(decision.get("status") or "")
    if (
        not status.startswith("closed_")
        or any(decision.get(key) is not False for key in (
            "runnable", "recommendation_eligible", "production_eligible",
        ))
        or not isinstance(reasons, list)
        or not reasons
        or not all(isinstance(item, str) and item.strip() for item in reasons)
    ):
        raise ValueError("manual source intake decision is not fail-closed")

    state = _load_existing(path)
    identity = _identity(doi, title)
    if any(
        isinstance(item, dict) and item.get("identity") == identity
        for item in state.get("candidates") or []
    ):
        raise ValueError("manual source intake candidate already exists")
    timestamp = _stamp(reviewed_at or _now())
    try:
        artifact_ref = str(
            intake_path.resolve().relative_to(config.PROJECT_ROOT.resolve())
        )
    except ValueError:
        artifact_ref = str(intake_path.resolve())
    artifact_sha = _sha256(intake_path)
    candidate = {
        "scout_id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20],
        "identity": identity,
        "doi": doi or None,
        "title": title,
        "authors": [
            str(value).strip() for value in (source.get("authors") or [])[:8]
            if str(value).strip()
        ],
        "year": source.get("year") if isinstance(source.get("year"), int) else None,
        "container_title": str(source.get("container_title") or "").strip() or None,
        "market_scope": market_scope or None,
        "url": f"https://doi.org/{urllib.parse.quote(doi, safe='/')}",
        "source_provider": "manual_primary_source_review",
        "source_providers": ["manual_primary_source_review"],
        "scope_evidence": "title",
        "source_urls": [
            str(value).strip() for value in (source.get("source_urls") or [])
            if str(value).strip()
        ],
        "matched_queries": ["manual_primary_source_review"],
        "first_seen_at": timestamp,
        "reviewed_at": timestamp,
        "status": status,
        "license_audit_status": decision.get("license_audit_status"),
        "pit_feasibility_status": decision.get("pit_feasibility_status"),
        "review_reason": str(artifact.get("review_reason") or "").strip() or None,
        "candidate_family": str(artifact.get("candidate_family") or "").strip() or None,
        "formula": artifact.get("formula"),
        "input_contract_gaps": reasons,
        "next_action": artifact.get("next_action"),
        "review_artifact": artifact_ref,
        "review_artifact_sha256": artifact_sha,
        "research_only": True,
        "runnable": False,
        "factor_values_computed": False,
        "outcomes_read": False,
        "recommendation_eligible": False,
        "production_eligible": False,
    }
    candidates = [
        item for item in state.get("candidates") or [] if isinstance(item, dict)
    ]
    candidates.append(candidate)
    candidates = _ordered_candidates(candidates)
    statuses = [str(item.get("status") or "unreviewed_research_source") for item in candidates]
    unreviewed_count = statuses.count("unreviewed_research_source")
    state.update({
        "candidates": candidates,
        "unreviewed_count": unreviewed_count,
        "reviewed_count": len(statuses) - unreviewed_count,
        "closed_count": sum(value.startswith("closed_") for value in statuses),
        "formula_pending_count": statuses.count("formula_pending"),
        "source_contract_pending_count": statuses.count("source_contract_pending"),
        "manual_intake_count": int(state.get("manual_intake_count") or 0) + 1,
        "last_manual_intake_at": timestamp,
        "last_manual_intake_artifact": artifact_ref,
        "last_manual_intake_artifact_sha256": artifact_sha,
    })
    _write(path, state)
    return state


def apply_manual_historical_closed_intake(
    *, intake_path: Path, state_path: Path | None = None,
    reviewed_at: dt.datetime | None = None,
) -> dict:
    """Record a source that completed one sealed test before scout registration."""
    path = state_path or STATE_PATH
    artifact = json.loads(intake_path.read_text(encoding="utf-8"))
    if artifact.get("record_type") != "public_source_manual_historical_closed_intake":
        raise ValueError("manual historical intake record_type is invalid")
    if artifact.get("source_state_sha256_before_intake") != _sha256(path):
        raise ValueError("manual historical intake state hash mismatch")
    source = artifact.get("source") or {}
    decision = artifact.get("decision") or {}
    binding = artifact.get("bound_historical_result") or {}
    title = str(source.get("title") or "").strip()
    doi = _doi(source.get("doi"))
    primary_source_url = str(
        source.get("primary_source_url") or source.get("url") or ""
    ).strip()
    parsed_source_url = urllib.parse.urlparse(primary_source_url)
    stable_url = bool(
        parsed_source_url.scheme == "https" and parsed_source_url.netloc
    )
    market_scope = str(source.get("market_scope") or "").strip()
    if (
        not title
        or not (doi or stable_url)
        or not _relevant(f"{title} {market_scope}")
    ):
        raise ValueError("manual historical intake identity is invalid")
    result_path = config.PROJECT_ROOT / str(binding.get("path") or "")
    if not result_path.is_file() or _sha256(result_path) != binding.get("sha256"):
        raise ValueError("manual historical intake result binding mismatch")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    candidate_id = str(artifact.get("candidate_id") or "")
    if (
        result.get("candidate_id") != candidate_id
        or result.get("status") not in {
            "permanently_rejected_historical_factor",
            "permanently_rejected_historical_risk_factor",
        }
        or result.get("future_outcome_read") is not True
        or result.get("historical_factor_gate_passed") is not False
        or any(result.get(key) is not False for key in (
            "production_allowed", "recommendation_eligible",
        ))
        or not (
            result.get("automatic_trading_allowed") is False
            or result.get("automatic_veto_or_sell_allowed") is False
        )
    ):
        raise ValueError("manual historical intake result is not a sealed rejection")
    frozen = artifact.get("frozen_artifact_bindings")
    if not isinstance(frozen, dict) or not frozen:
        raise ValueError("manual historical intake frozen bindings are missing")
    if any(
        not (config.PROJECT_ROOT / relative).is_file()
        or _sha256(config.PROJECT_ROOT / relative) != expected
        for relative, expected in frozen.items()
    ):
        raise ValueError("manual historical intake frozen binding mismatch")
    if any(frozen.get(relative) != expected for relative, expected in result.get("input_hashes", {}).items()):
        raise ValueError("manual historical intake does not bind the tested inputs")
    reasons = artifact.get("closure_reasons")
    if (
        decision.get("status") != "closed_historical_factor_rejected"
        or any(decision.get(key) is not False for key in (
            "runnable", "recommendation_eligible", "production_eligible",
        ))
        or not isinstance(reasons, list) or not reasons
    ):
        raise ValueError("manual historical intake decision is invalid")

    state = _load_existing(path)
    source_identity = _identity(doi, title) if doi else f"url:{primary_source_url}"
    identity = f"{source_identity}#candidate:{candidate_id}"
    if any(
        isinstance(item, dict) and item.get("identity") == identity
        for item in state.get("candidates") or []
    ):
        raise ValueError("manual historical intake candidate already exists")
    timestamp = _stamp(reviewed_at or _now())
    try:
        artifact_ref = str(intake_path.resolve().relative_to(config.PROJECT_ROOT.resolve()))
        result_ref = str(result_path.resolve().relative_to(config.PROJECT_ROOT.resolve()))
    except ValueError:
        artifact_ref, result_ref = str(intake_path.resolve()), str(result_path.resolve())
    candidate = {
        "scout_id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20],
        "identity": identity,
        "doi": doi or None,
        "title": title,
        "authors": [
            str(value).strip() for value in (source.get("authors") or [])[:8]
            if str(value).strip()
        ],
        "year": source.get("year") if isinstance(source.get("year"), int) else None,
        "container_title": str(source.get("container_title") or "").strip() or None,
        "market_scope": market_scope or None,
        "url": (
            f"https://doi.org/{urllib.parse.quote(doi, safe='/')}"
            if doi else primary_source_url
        ),
        "source_provider": "manual_primary_source_review",
        "source_providers": ["manual_primary_source_review"],
        "scope_evidence": "manual_market_mapping",
        "matched_queries": ["manual_primary_source_review"],
        "first_seen_at": timestamp,
        "reviewed_at": timestamp,
        "closed_at": timestamp,
        "status": "closed_historical_factor_rejected",
        "candidate_id": candidate_id,
        "candidate_family": str(artifact.get("candidate_family") or "").strip() or None,
        "formula": artifact.get("formula"),
        "input_contract_gaps": reasons,
        "next_action": artifact.get("next_action"),
        "review_artifact": artifact_ref,
        "review_artifact_sha256": _sha256(intake_path),
        "historical_result": result_ref,
        "historical_result_sha256": _sha256(result_path),
        "research_only": True,
        "runnable": False,
        "factor_values_computed": True,
        "outcomes_read": True,
        "historical_factor_gate_passed": False,
        "recommendation_eligible": False,
        "production_eligible": False,
    }
    candidates = [item for item in state.get("candidates") or [] if isinstance(item, dict)]
    candidates.append(candidate)
    state["candidates"] = _ordered_candidates(candidates)
    statuses = [str(item.get("status") or "") for item in state["candidates"]]
    state.update({
        "closed_count": sum(value.startswith("closed_") for value in statuses),
        "unreviewed_count": statuses.count("unreviewed_research_source"),
        "reviewed_count": len(statuses) - statuses.count("unreviewed_research_source"),
        "source_contract_pending_count": statuses.count("source_contract_pending"),
        "manual_intake_count": int(state.get("manual_intake_count") or 0) + 1,
        "last_manual_intake_at": timestamp,
        "last_manual_intake_artifact": artifact_ref,
        "last_manual_intake_artifact_sha256": _sha256(intake_path),
    })
    _write(path, state)
    return state


def apply_manual_candidate_intake(
    *, intake_path: Path, state_path: Path | None = None,
    reviewed_at: dt.datetime | None = None,
) -> dict:
    """Record one manually found, outcome-blind candidate without admitting it."""
    path = state_path or STATE_PATH
    before_sha = _sha256(path)
    artifact = json.loads(intake_path.read_text(encoding="utf-8"))
    if artifact.get("record_type") != (
        "public_source_manual_candidate_intake_without_outcome_access"
    ):
        raise ValueError("manual source candidate intake record_type is invalid")
    if artifact.get("source_state_sha256_before_intake") != before_sha:
        raise ValueError("manual source candidate intake state hash mismatch")
    boundary = artifact.get("audit_boundary")
    required_false = {
        "future_returns_read", "factor_values_computed", "candidate_ranks_computed",
        "ic_computed", "backtest_computed", "direction_selected_from_local_outcomes",
    }
    if not isinstance(boundary, dict) or any(
        boundary.get(key) is not False for key in required_false
    ):
        raise ValueError("manual source candidate intake boundary is invalid")

    source = artifact.get("source")
    decision = artifact.get("decision")
    formula = artifact.get("formula")
    next_steps = artifact.get("required_next_steps")
    if not all(isinstance(value, dict) for value in (source, decision, formula)):
        raise ValueError("manual source candidate intake payload is invalid")
    if not isinstance(next_steps, list) or not next_steps or not all(
        isinstance(item, str) and item.strip() for item in next_steps
    ):
        raise ValueError("manual source candidate intake next steps are invalid")
    title = str(source.get("title") or "").strip()
    doi = _doi(source.get("doi"))
    market_scope = str(source.get("market_scope") or "").strip()
    if not title or not doi or not _relevant(f"{title} {market_scope}"):
        raise ValueError("manual source candidate intake identity is invalid")
    if (
        decision.get("status") != "source_contract_pending"
        or decision.get("input_audit_status") != (
            "source_input_feasibility_passed_not_factor"
        )
        or any(decision.get(key) is not False for key in (
            "admission_allowed", "runnable", "recommendation_eligible",
            "production_eligible",
        ))
    ):
        raise ValueError("manual source candidate intake is not fail-closed")

    state = _load_existing(path)
    identity = _identity(doi, title)
    if any(
        isinstance(item, dict) and item.get("identity") == identity
        for item in state.get("candidates") or []
    ):
        raise ValueError("manual source candidate intake already exists")
    timestamp = _stamp(reviewed_at or _now())
    try:
        artifact_ref = str(
            intake_path.resolve().relative_to(config.PROJECT_ROOT.resolve())
        )
    except ValueError:
        artifact_ref = str(intake_path.resolve())
    artifact_sha = _sha256(intake_path)
    candidate = {
        "scout_id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20],
        "identity": identity,
        "doi": doi,
        "title": title,
        "authors": [
            str(value).strip() for value in (source.get("authors") or [])[:8]
            if str(value).strip()
        ],
        "year": source.get("year") if isinstance(source.get("year"), int) else None,
        "container_title": str(source.get("container_title") or "").strip() or None,
        "market_scope": market_scope or None,
        "url": f"https://doi.org/{urllib.parse.quote(doi, safe='/')}",
        "source_provider": "manual_primary_source_review",
        "source_providers": ["manual_primary_source_review"],
        "scope_evidence": "manual_market_mapping",
        "source_urls": [
            str(value).strip() for value in (source.get("source_urls") or [])
            if str(value).strip()
        ],
        "matched_queries": ["manual_primary_source_review"],
        "first_seen_at": timestamp,
        "reviewed_at": timestamp,
        "status": "source_contract_pending",
        "license_audit_status": decision.get("license_audit_status"),
        "pit_feasibility_status": decision.get("pit_feasibility_status"),
        "review_reason": str(artifact.get("review_reason") or "").strip() or None,
        "candidate_family": str(artifact.get("candidate_family") or "").strip() or None,
        "formula": formula,
        "required_next_steps": next_steps,
        "next_action": artifact.get("next_action"),
        "review_artifact": artifact_ref,
        "review_artifact_sha256": artifact_sha,
        "input_audit_status": decision.get("input_audit_status"),
        "input_audit_artifact": artifact_ref,
        "input_audit_artifact_sha256": artifact_sha,
        "input_audited_at": timestamp,
        "research_only": True,
        "runnable": False,
        "factor_values_computed": False,
        "outcomes_read": False,
        "recommendation_eligible": False,
        "production_eligible": False,
    }
    candidates = [
        item for item in state.get("candidates") or [] if isinstance(item, dict)
    ]
    candidates.append(candidate)
    candidates = _ordered_candidates(candidates)
    statuses = [str(item.get("status") or "unreviewed_research_source") for item in candidates]
    unreviewed_count = statuses.count("unreviewed_research_source")
    state.update({
        "candidates": candidates,
        "unreviewed_count": unreviewed_count,
        "reviewed_count": len(statuses) - unreviewed_count,
        "closed_count": sum(value.startswith("closed_") for value in statuses),
        "formula_pending_count": statuses.count("formula_pending"),
        "source_contract_pending_count": statuses.count("source_contract_pending"),
        "manual_intake_count": int(state.get("manual_intake_count") or 0) + 1,
        "last_manual_intake_at": timestamp,
        "last_manual_intake_artifact": artifact_ref,
        "last_manual_intake_artifact_sha256": artifact_sha,
    })
    _write(path, state)
    return state


def apply_triage(
    *, triage_path: Path, state_path: Path | None = None,
    reviewed_at: dt.datetime | None = None,
) -> dict:
    """Apply one hash-bound, outcome-blind source review atomically."""
    path = state_path or STATE_PATH
    before_sha = _sha256(path)
    artifact = json.loads(triage_path.read_text(encoding="utf-8"))
    if artifact.get("record_type") != "public_source_candidate_triage_without_outcome_access":
        raise ValueError("source triage record_type is invalid")
    if artifact.get("source_state_sha256_before_triage") != before_sha:
        raise ValueError("source triage state hash mismatch")
    dispositions = artifact.get("dispositions")
    if not isinstance(dispositions, list) or not dispositions:
        raise ValueError("source triage dispositions are empty")
    if int(artifact.get("items_reviewed") or -1) != len(dispositions):
        raise ValueError("source triage item count mismatch")
    disposition_ids = [str(item.get("scout_id") or "") for item in dispositions]
    if "" in disposition_ids or len(set(disposition_ids)) != len(disposition_ids):
        raise ValueError("source triage scout ids are invalid or duplicated")

    state = _load_existing(path)
    candidates = state.get("candidates") or []
    by_id = {
        str(item.get("scout_id") or ""): item for item in candidates
        if isinstance(item, dict) and item.get("scout_id")
    }
    timestamp = _stamp(reviewed_at or _now())
    try:
        artifact_ref = str(
            triage_path.resolve().relative_to(config.PROJECT_ROOT.resolve())
        )
    except ValueError:
        artifact_ref = str(triage_path.resolve())
    artifact_sha = _sha256(triage_path)
    for disposition in dispositions:
        if not isinstance(disposition, dict):
            raise ValueError("source triage disposition is not an object")
        unexpected = set(disposition) - ({"scout_id", "doi"} | _TRIAGE_MUTABLE_FIELDS)
        if unexpected:
            raise ValueError(
                f"source triage fields are not allowed: {sorted(unexpected)}"
            )
        scout_id = str(disposition.get("scout_id") or "")
        candidate = by_id.get(scout_id)
        if candidate is None:
            raise ValueError(f"source triage candidate missing: {scout_id}")
        if candidate.get("status") != "unreviewed_research_source":
            raise ValueError(f"source triage candidate already reviewed: {scout_id}")
        if str(candidate.get("doi") or "") != str(disposition.get("doi") or ""):
            raise ValueError(f"source triage DOI mismatch: {scout_id}")
        status = str(disposition.get("status") or "")
        if not (
            status.startswith("closed_")
            or status in {"formula_pending", "source_contract_pending"}
        ):
            raise ValueError(f"source triage status is not fail-closed: {scout_id}")
        candidate.update({
            key: disposition[key] for key in _TRIAGE_MUTABLE_FIELDS
            if key in disposition
        })
        candidate.update({
            "reviewed_at": timestamp,
            "review_artifact": artifact_ref,
            "review_artifact_sha256": artifact_sha,
            "research_only": True,
            "runnable": False,
            "factor_values_computed": False,
            "outcomes_read": False,
            "recommendation_eligible": False,
            "production_eligible": False,
        })

    statuses = [
        str(item.get("status") or "unreviewed_research_source")
        for item in candidates if isinstance(item, dict)
    ]
    unreviewed_count = statuses.count("unreviewed_research_source")
    state.update({
        "candidates": _ordered_candidates(candidates),
        "unreviewed_count": unreviewed_count,
        "reviewed_count": len(statuses) - unreviewed_count,
        "closed_count": sum(status.startswith("closed_") for status in statuses),
        "formula_pending_count": statuses.count("formula_pending"),
        "source_contract_pending_count": statuses.count("source_contract_pending"),
        "last_triage_at": timestamp,
        "last_triage_artifact": artifact_ref,
        "last_triage_artifact_sha256": artifact_sha,
    })
    _write(path, state)
    return state


def apply_repository_triage(
    *, triage_path: Path, state_path: Path | None = None,
    reviewed_at: dt.datetime | None = None,
) -> dict:
    """Apply one hash-bound, outcome-blind review to the GitHub inbox."""
    path = state_path or STATE_PATH
    before_sha = _sha256(path)
    artifact = json.loads(triage_path.read_text(encoding="utf-8"))
    if artifact.get("record_type") != (
        "public_repository_source_group_triage_without_outcome_access"
    ):
        raise ValueError("repository triage record_type is invalid")
    if artifact.get("source_state_sha256_before_triage") != before_sha:
        raise ValueError("repository triage state hash mismatch")
    boundary = artifact.get("audit_boundary")
    required_false = {
        "repository_code_executed", "factor_values_computed",
        "future_returns_read", "candidate_ranks_computed", "ic_computed",
        "backtest_computed", "direction_selected_from_local_outcomes",
    }
    if not isinstance(boundary, dict) or any(
        boundary.get(key) is not False for key in required_false
    ):
        raise ValueError("repository triage outcome-blind boundary is invalid")
    dispositions = artifact.get("dispositions")
    if not isinstance(dispositions, list) or not dispositions:
        raise ValueError("repository triage dispositions are empty")
    if int(artifact.get("items_reviewed") or -1) != len(dispositions):
        raise ValueError("repository triage item count mismatch")
    identities = [str(item.get("identity") or "") for item in dispositions]
    if "" in identities or len(set(identities)) != len(identities):
        raise ValueError("repository triage identities are invalid or duplicated")

    state = _load_existing(path)
    rows = [
        item for item in state.get("repository_discoveries") or []
        if isinstance(item, dict)
    ]
    by_identity = {str(item.get("identity") or ""): item for item in rows}
    timestamp = _stamp(reviewed_at or _now())
    try:
        artifact_ref = str(
            triage_path.resolve().relative_to(config.PROJECT_ROOT.resolve())
        )
    except ValueError:
        artifact_ref = str(triage_path.resolve())
    artifact_sha = _sha256(triage_path)
    for disposition in dispositions:
        if not isinstance(disposition, dict):
            raise ValueError("repository triage disposition is not an object")
        unexpected = set(disposition) - (
            {"identity", "repository", "license_spdx"}
            | _REPOSITORY_TRIAGE_MUTABLE_FIELDS
        )
        if unexpected:
            raise ValueError(
                f"repository triage fields are not allowed: {sorted(unexpected)}"
            )
        identity = str(disposition.get("identity") or "")
        row = by_identity.get(identity)
        if row is None:
            raise ValueError(f"repository triage source missing: {identity}")
        if row.get("status") != "unreviewed_repository_source":
            raise ValueError(f"repository triage source already reviewed: {identity}")
        for key in ("repository", "license_spdx"):
            if str(row.get(key) or "") != str(disposition.get(key) or ""):
                raise ValueError(f"repository triage {key} mismatch: {identity}")
        status = str(disposition.get("status") or "")
        if not (status.startswith("reviewed_") or status.startswith("closed_")):
            raise ValueError(f"repository triage status is not terminal: {identity}")
        row.update({
            key: disposition[key] for key in _REPOSITORY_TRIAGE_MUTABLE_FIELDS
            if key in disposition
        })
        row.update({
            "reviewed_at": timestamp,
            "review_artifact": artifact_ref,
            "review_artifact_sha256": artifact_sha,
            "research_only": True,
            "runnable": False,
            "factor_values_computed": False,
            "outcomes_read": False,
            "recommendation_eligible": False,
            "production_eligible": False,
        })

    if _sha256(path) != before_sha:
        raise RuntimeError("source scout state changed during repository triage")
    statuses = [str(item.get("status") or "") for item in rows]
    state.update({
        "repository_discoveries": rows,
        "repository_discovery_unreviewed_count": statuses.count(
            "unreviewed_repository_source"
        ),
        "repository_discovery_reviewed_count": sum(
            value.startswith(("reviewed_", "closed_")) for value in statuses
        ),
        "repository_discovery_closed_count": sum(
            value.startswith("closed_") for value in statuses
        ),
        "repository_last_triage_at": timestamp,
        "repository_last_triage_artifact": artifact_ref,
        "repository_last_triage_artifact_sha256": artifact_sha,
    })
    _write(path, state)
    return state


def apply_input_audit(
    *, audit_path: Path, state_path: Path | None = None,
    audited_at: dt.datetime | None = None,
) -> dict:
    """Bind one outcome-blind input audit to an already reviewed candidate."""
    path = state_path or STATE_PATH
    before_sha = _sha256(path)
    artifact = json.loads(audit_path.read_text(encoding="utf-8"))
    if artifact.get("record_type") != (
        "public_source_candidate_input_feasibility_audit_without_outcome_access"
    ):
        raise ValueError("source input audit record_type is invalid")
    if artifact.get("source_state_sha256_before_audit") != before_sha:
        raise ValueError("source input audit state hash mismatch")
    boundary = artifact.get("audit_boundary")
    required_false = {
        "future_returns_read", "factor_values_computed", "candidate_ranks_computed",
        "ic_computed", "backtest_computed", "direction_selected_from_local_outcomes",
    }
    if not isinstance(boundary, dict) or any(
        boundary.get(key) is not False for key in required_false
    ):
        raise ValueError("source input audit outcome-blind boundary is invalid")
    identity = artifact.get("candidate")
    decision = artifact.get("decision")
    gaps = artifact.get("blocking_gaps")
    if not isinstance(identity, dict) or not isinstance(decision, dict):
        raise ValueError("source input audit candidate or decision is invalid")
    if not isinstance(gaps, list) or not gaps or not all(
        isinstance(item, str) and item.strip() for item in gaps
    ):
        raise ValueError("source input audit blocking gaps are invalid")
    draft_evidence = artifact.get("candidate_draft_evidence")
    if draft_evidence is not None:
        if not isinstance(draft_evidence, dict):
            raise ValueError("source input audit candidate draft evidence is invalid")
        formula = draft_evidence.get("original_formula")
        mechanism = draft_evidence.get("a_share_mechanism")
        fields = draft_evidence.get("required_fields")
        preregistration = draft_evidence.get("preregistration")
        if (
            not isinstance(formula, dict)
            or not all(str(formula.get(key) or "").strip() for key in (
                "expression", "source_locator",
            ))
            or formula.get("verified_exact") is not True
            or not isinstance(mechanism, dict)
            or not all(str(mechanism.get(key) or "").strip() for key in (
                "statement", "market_scope",
            ))
            or not isinstance(fields, list) or not fields
            or any(
                not isinstance(row, dict)
                or not all(str(row.get(key) or "").strip() for key in (
                    "name", "source", "available_at_rule",
                ))
                for row in fields
            )
            or not isinstance(preregistration, dict)
            or preregistration.get("status") != "frozen"
        ):
            raise ValueError("source input audit candidate draft evidence is incomplete")
        prereg_path = (
            config.PROJECT_ROOT
            / str(preregistration.get("artifact_path") or "")
        ).resolve()
        try:
            prereg_path.relative_to(config.PROJECT_ROOT.resolve())
        except ValueError as exc:
            raise ValueError(
                "source input audit preregistration path is invalid"
            ) from exc
        if (
            not prereg_path.is_file()
            or _sha256(prereg_path)
            != str(preregistration.get("artifact_sha256") or "")
        ):
            raise ValueError(
                "source input audit preregistration identity is invalid"
            )
    if (
        decision.get("status") != "input_feasibility_partial_not_admitted"
        or decision.get("candidate_status") != "source_contract_pending"
        or any(decision.get(key) is not False for key in (
            "admission_allowed", "runnable", "recommendation_eligible",
            "production_eligible",
        ))
    ):
        raise ValueError("source input audit decision is not fail-closed")

    state = _load_existing(path)
    candidate = next((
        item for item in state.get("candidates") or []
        if isinstance(item, dict)
        and item.get("scout_id") == identity.get("scout_id")
    ), None)
    if candidate is None:
        raise ValueError("source input audit candidate is missing")
    if str(candidate.get("doi") or "") != str(identity.get("doi") or ""):
        raise ValueError("source input audit DOI mismatch")
    if candidate.get("status") != "source_contract_pending":
        raise ValueError("source input audit candidate is not contract-pending")
    triage = artifact.get("triage_binding")
    if (
        not isinstance(triage, dict)
        or triage.get("sha256") != candidate.get("review_artifact_sha256")
    ):
        raise ValueError("source input audit triage binding mismatch")

    timestamp = _stamp(audited_at or _now())
    try:
        artifact_ref = str(
            audit_path.resolve().relative_to(config.PROJECT_ROOT.resolve())
        )
    except ValueError:
        artifact_ref = str(audit_path.resolve())
    artifact_sha = _sha256(audit_path)
    candidate.update({
        "status": "source_contract_pending",
        "license_audit_status": decision.get("license_audit_status"),
        "pit_feasibility_status": decision.get("pit_feasibility_status"),
        "input_contract_gaps": gaps,
        "next_action": artifact.get("next_action"),
        "input_audit_status": decision.get("status"),
        "input_audited_at": timestamp,
        "input_audit_artifact": artifact_ref,
        "input_audit_artifact_sha256": artifact_sha,
        "research_only": True,
        "runnable": False,
        "factor_values_computed": False,
        "outcomes_read": False,
        "recommendation_eligible": False,
        "production_eligible": False,
    })
    if draft_evidence is not None:
        candidate.update({
            "original_formula": dict(draft_evidence["original_formula"]),
            "a_share_mechanism": dict(draft_evidence["a_share_mechanism"]),
            "required_fields": [
                dict(row) for row in draft_evidence["required_fields"]
            ],
            "preregistration": dict(draft_evidence["preregistration"]),
        })
    state["candidates"] = _ordered_candidates([
        item for item in state.get("candidates") or [] if isinstance(item, dict)
    ])
    state.update({
        "last_input_audit_at": timestamp,
        "last_input_audit_artifact": artifact_ref,
        "last_input_audit_artifact_sha256": artifact_sha,
    })
    _write(path, state)
    return state


def close_pending_candidate(
    *, closure_path: Path, state_path: Path | None = None,
    closed_at: dt.datetime | None = None,
) -> dict:
    """Close one audited candidate when exact reproduction remains impossible."""
    path = state_path or STATE_PATH
    before_sha = _sha256(path)
    artifact = json.loads(closure_path.read_text(encoding="utf-8"))
    if artifact.get("record_type") != (
        "public_source_candidate_reproducibility_closure_without_outcome_access"
    ):
        raise ValueError("source candidate closure record_type is invalid")
    if artifact.get("source_state_sha256_before_closure") != before_sha:
        raise ValueError("source candidate closure state hash mismatch")
    boundary = artifact.get("audit_boundary")
    required_false = {
        "future_returns_read", "factor_values_computed", "candidate_ranks_computed",
        "ic_computed", "backtest_computed", "direction_selected_from_local_outcomes",
    }
    if not isinstance(boundary, dict) or any(
        boundary.get(key) is not False for key in required_false
    ):
        raise ValueError("source candidate closure outcome-blind boundary is invalid")
    identity = artifact.get("candidate")
    decision = artifact.get("decision")
    reasons = artifact.get("closure_reasons")
    if not isinstance(identity, dict) or not isinstance(decision, dict):
        raise ValueError("source candidate closure candidate or decision is invalid")
    status = str(decision.get("status") or "")
    if (
        not status.startswith("closed_")
        or any(decision.get(key) is not False for key in (
            "runnable", "recommendation_eligible", "production_eligible",
        ))
        or not isinstance(reasons, list)
        or not reasons
        or not all(isinstance(item, str) and item.strip() for item in reasons)
    ):
        raise ValueError("source candidate closure decision is not fail-closed")

    state = _load_existing(path)
    candidate = next((
        item for item in state.get("candidates") or []
        if isinstance(item, dict) and item.get("scout_id") == identity.get("scout_id")
    ), None)
    if candidate is None:
        raise ValueError("source candidate closure candidate is missing")
    if str(candidate.get("doi") or "") != str(identity.get("doi") or ""):
        raise ValueError("source candidate closure DOI mismatch")
    prior_status = candidate.get("status")
    if prior_status not in {"formula_pending", "source_contract_pending"}:
        raise ValueError("source candidate closure candidate is not pending")
    if prior_status == "formula_pending":
        binding = artifact.get("bound_triage")
        expected_binding = candidate.get("review_artifact_sha256")
        binding_error = "source candidate closure triage binding mismatch"
    else:
        binding = artifact.get("bound_input_audit")
        expected_binding = candidate.get("input_audit_artifact_sha256")
        binding_error = "source candidate closure input-audit binding mismatch"
    if not isinstance(binding, dict) or binding.get("sha256") != expected_binding:
        raise ValueError(binding_error)

    timestamp = _stamp(closed_at or _now())
    try:
        artifact_ref = str(
            closure_path.resolve().relative_to(config.PROJECT_ROOT.resolve())
        )
    except ValueError:
        artifact_ref = str(closure_path.resolve())
    artifact_sha = _sha256(closure_path)
    candidate.update({
        "status": status,
        "pit_feasibility_status": decision.get("pit_feasibility_status"),
        "input_contract_gaps": reasons,
        "next_action": artifact.get("next_action"),
        "closed_at": timestamp,
        "closure_artifact": artifact_ref,
        "closure_artifact_sha256": artifact_sha,
        "research_only": True,
        "runnable": False,
        "factor_values_computed": False,
        "outcomes_read": False,
        "recommendation_eligible": False,
        "production_eligible": False,
    })
    state["candidates"] = _ordered_candidates([
        item for item in state.get("candidates") or [] if isinstance(item, dict)
    ])
    statuses = [
        str(item.get("status") or "unreviewed_research_source")
        for item in state.get("candidates") or [] if isinstance(item, dict)
    ]
    state.update({
        "closed_count": sum(value.startswith("closed_") for value in statuses),
        "formula_pending_count": statuses.count("formula_pending"),
        "source_contract_pending_count": statuses.count("source_contract_pending"),
        "last_closure_at": timestamp,
        "last_closure_artifact": artifact_ref,
        "last_closure_artifact_sha256": artifact_sha,
    })
    _write(path, state)
    return state


def close_historically_rejected_candidate(
    *, closure_path: Path, state_path: Path | None = None,
    closed_at: dt.datetime | None = None,
) -> dict:
    """Bind one sealed historical rejection or terminal failure into the archive."""
    path = state_path or STATE_PATH
    before_sha = _sha256(path)
    artifact = json.loads(closure_path.read_text(encoding="utf-8"))
    record_type = artifact.get("record_type")
    if record_type not in {
        "public_source_candidate_historical_test_closure",
        "public_source_candidate_terminal_historical_closure",
    }:
        raise ValueError("historical closure record_type is invalid")
    if artifact.get("source_state_sha256_before_closure") != before_sha:
        raise ValueError("historical closure state hash mismatch")
    identity = artifact.get("candidate") or {}
    terminal_failure = record_type == (
        "public_source_candidate_terminal_historical_closure"
    )
    binding = artifact.get(
        "bound_terminal_closure" if terminal_failure else "bound_historical_result"
    ) or {}
    result_path = config.PROJECT_ROOT / str(binding.get("path") or "")
    if not result_path.is_file() or _sha256(result_path) != binding.get("sha256"):
        raise ValueError("historical closure result binding mismatch")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if terminal_failure:
        boundary = result.get("result_boundary") or {}
        disposition = result.get("disposition") or {}
        decision = artifact.get("decision") or {}
        if (
            result.get("candidate_id") != identity.get("candidate_id")
            or result.get("status")
            != "permanently_closed_non_runnable_execution_identity_gap"
            or boundary.get("factor_values_computed_and_byte_reproduced") is not True
            or boundary.get("real_outcome_inputs_opened") is not True
            or boundary.get("monthly_result_artifact_written") is not False
            or boundary.get("statistics_runner_called") is not False
            or boundary.get("aggregate_performance_seen") is not False
            or boundary.get("historical_factor_gate_evaluated") is not False
            or disposition.get("exact_candidate_closed") is not True
            or disposition.get("third_run_allowed") is not False
            or any(disposition.get(key) is not False for key in (
                "runnable", "production_allowed", "recommendation_allowed",
                "position_sizing_allowed", "automatic_trading_allowed",
            ))
            or decision.get("status")
            != "closed_historical_validation_execution_identity_gap"
            or any(decision.get(key) is not False for key in (
                "runnable", "recommendation_eligible", "production_eligible",
            ))
        ):
            raise ValueError("historical closure result is not a sealed terminal failure")
        bindings = result.get("bindings") or {}
        if not isinstance(bindings, dict) or not bindings:
            raise ValueError("historical terminal closure bindings are missing")
        try:
            for bound in bindings.values():
                bound_path = (
                    config.PROJECT_ROOT / str((bound or {}).get("path") or "")
                ).resolve()
                bound_path.relative_to(config.PROJECT_ROOT.resolve())
                if (
                    not bound_path.is_file()
                    or _sha256(bound_path) != (bound or {}).get("sha256")
                ):
                    raise ValueError("historical terminal closure binding mismatch")
        except (AttributeError, TypeError, ValueError):
            raise ValueError("historical terminal closure binding mismatch") from None
        archive_status = "closed_historical_validation_execution_identity_gap"
    else:
        if (
            result.get("candidate_id") != identity.get("candidate_id")
            or result.get("status") != "permanently_rejected_historical_factor"
            or result.get("future_outcome_read") is not True
            or result.get("historical_factor_gate_passed") is not False
            or any(result.get(key) is not False for key in (
                "production_allowed", "recommendation_eligible",
                "automatic_trading_allowed",
            ))
        ):
            raise ValueError("historical closure result is not a sealed rejection")
        archive_status = "closed_historical_factor_rejected"
    reasons = artifact.get("closure_reasons")
    if not isinstance(reasons, list) or not reasons:
        raise ValueError("historical closure reasons are missing")

    state = _load_existing(path)
    candidate = next((
        item for item in state.get("candidates") or []
        if isinstance(item, dict)
        and item.get("scout_id") == identity.get("scout_id")
    ), None)
    if (
        candidate is None
        or str(candidate.get("doi") or "") != str(identity.get("doi") or "")
        or candidate.get("status") != "source_contract_pending"
    ):
        raise ValueError("historical closure candidate is not pending")

    timestamp = _stamp(closed_at or _now())
    try:
        artifact_ref = str(
            closure_path.resolve().relative_to(config.PROJECT_ROOT.resolve())
        )
    except ValueError:
        artifact_ref = str(closure_path.resolve())
    try:
        result_ref = str(
            result_path.resolve().relative_to(config.PROJECT_ROOT.resolve())
        )
    except ValueError:
        result_ref = str(result_path.resolve())
    candidate.update({
        "status": archive_status,
        "candidate_id": identity.get("candidate_id"),
        "input_contract_gaps": reasons,
        "next_action": artifact.get("next_action"),
        "closed_at": timestamp,
        "closure_artifact": artifact_ref,
        "closure_artifact_sha256": _sha256(closure_path),
        "historical_result": result_ref,
        "historical_result_sha256": _sha256(result_path),
        "research_only": True,
        "runnable": False,
        "factor_values_computed": True,
        "outcomes_read": True,
        "historical_factor_gate_passed": False,
        "recommendation_eligible": False,
        "production_eligible": False,
    })
    if terminal_failure:
        candidate.update({
            "pit_feasibility_status": (
                "failed_missing_delisting_or_exchange_economics"
            ),
            "historical_validation_attempted": True,
            "historical_validation_completed": False,
            "historical_factor_gate_evaluated": False,
            "performance_metrics_available": False,
        })
    state["candidates"] = _ordered_candidates([
        item for item in state.get("candidates") or [] if isinstance(item, dict)
    ])
    statuses = [str(item.get("status") or "") for item in state["candidates"]]
    state.update({
        "closed_count": sum(value.startswith("closed_") for value in statuses),
        "source_contract_pending_count": statuses.count("source_contract_pending"),
        "last_closure_at": timestamp,
        "last_closure_artifact": artifact_ref,
        "last_closure_artifact_sha256": _sha256(closure_path),
    })
    _write(path, state, expected_sha=before_sha)
    return state


def _input_snapshot_result(
    candidates: list[dict], *, now: dt.datetime,
    fetcher: Callable | None = None,
) -> dict:
    from . import epu_snapshot

    try:
        return epu_snapshot.capture_if_active(
            candidates, now=now, fetcher=fetcher,
        )
    except Exception as exc:
        return {
            "status": "unavailable",
            "attempted_at": _stamp(now),
            "error": f"{type(exc).__name__}: {exc}"[:300],
            "research_only": True,
            "runnable": False,
            "production_eligible": False,
        }


def capture_inputs_once(
    *, state_path: Path | None = None, now: dt.datetime | None = None,
    fetcher: Callable | None = None,
) -> dict:
    """Capture active forward inputs without running discovery or outcomes."""
    path = state_path or STATE_PATH
    now = now or _now()
    state = _load_existing(path)
    state["input_snapshot_results"] = [
        _input_snapshot_result(
            state.get("candidates") or [], now=now, fetcher=fetcher,
        )
    ]
    _write(path, state)
    return state


def run(
    *, state_path: Path | None = None, now: dt.datetime | None = None,
    fetcher: Callable[[str], dict] | None = None,
    openalex_fetcher: Callable[[str], dict] | None = None,
    repository_fetcher: Callable[[str], object] | None = None,
    repository_discovery_fetcher: Callable[[str], object] | None = None,
    input_snapshot_fetcher: Callable | None = None,
) -> dict:
    """Run one bounded metadata pass and atomically persist its research inbox."""
    path = state_path or STATE_PATH
    now = now or _now()
    attempted_at = _stamp(now)
    before_sha = _sha256(path) if path.exists() else None
    existing = _load_existing(path)
    retry_queue = _retry_rows(existing)
    due_retry_rows = _due_retry_rows(existing, now)
    full_scan_due = _full_scan_due(existing, now)
    retry_only = bool(retry_queue) and not full_scan_due
    due_retry_keys = {
        _retry_key(
            str(item["provider"]), str(item["query"]), str(item["operation"]),
        )
        for item in due_retry_rows
    }
    deferred_retry_keys = {
        key for key, item in retry_queue.items()
        if (
            (due_at := _parse_stamp(item.get("next_due_at"))) is not None
            and now < due_at
        )
    }
    known = _known_source_identities(existing)
    candidates = {}
    screened_out = [
        dict(item) for item in existing.get("screened_out") or []
        if isinstance(item, dict)
    ]
    for item in existing.get("candidates") or []:
        if not isinstance(item, dict) or not item.get("identity"):
            continue
        status = str(item.get("status") or "unreviewed_research_source")
        if (
            status != "unreviewed_research_source"
            or _relevant(str(item.get("title") or ""))
            or item.get("scope_evidence") == "abstract_metadata"
        ):
            candidates[str(item["identity"])] = dict(item)
        else:
            screened_out.append({
                "identity": item.get("identity"),
                "doi": item.get("doi"),
                "title": item.get("title"),
                "screened_out_at": attempted_at,
                "reason": "title_scope_mismatch",
            })
    historical_completed = _historical_queries_completed(existing)
    query_filters = (
        dict(existing.get("filters_by_query") or {}) if retry_only else {}
    )
    fetch = fetcher or _default_fetch
    # An injected Crossref fetcher keeps legacy unit tests isolated.  Production
    # runs (no injection), or tests that explicitly inject OpenAlex, exercise
    # both independent metadata sources.
    openalex_enabled = fetcher is None or openalex_fetcher is not None
    fetch_openalex = openalex_fetcher or _default_openalex_fetch
    openalex_historical_completed = {
        str(query) for query in existing.get("openalex_historical_queries_completed") or []
        if str(query) in QUERIES
    }
    openalex_filters_by_query = (
        dict(existing.get("openalex_filters_by_query") or {})
        if retry_only else {}
    )
    scanned = int(existing.get("items_scanned") or 0) if retry_only else 0
    relevant = int(existing.get("relevant_items") or 0) if retry_only else 0
    duplicate = int(existing.get("deduplicated_count") or 0) if retry_only else 0
    new_count = int(existing.get("new_count") or 0) if retry_only else 0
    successful = (
        int(existing.get("successful_query_count") or 0) if retry_only else 0
    )
    paper_successes_this_run = 0
    errors = (
        [
            dict(item) for item in (existing.get("errors") or [])
            if isinstance(item, dict)
        ]
        if retry_only else []
    )
    run_seen: set[str] = set()
    crossref_queries = [
        query for query in QUERIES
        if (
            _retry_key("Crossref", query, _PAPER_DISCOVERY)
            not in deferred_retry_keys
            and (
                not retry_only or _retry_key(
                    "Crossref", query, _PAPER_DISCOVERY,
                ) in due_retry_keys
            )
        )
    ]
    for query in crossref_queries:
        errors = _without_task_error(
            errors, "Crossref", query, _PAPER_DISCOVERY,
        )
        filters = _query_filters(existing, query, now, historical_completed)
        query_filters[query] = filters
        try:
            payload = fetch(_url(query, filters))
            items = ((payload.get("message") or {}).get("items") or [])
            if not isinstance(items, list):
                raise ValueError("Crossref message.items is not a list")
            successful += 1
            paper_successes_this_run += 1
            historical_completed.add(query)
            retry_queue.pop(
                _retry_key("Crossref", query, _PAPER_DISCOVERY), None,
            )
        except Exception as exc:
            error = _source_error(
                "Crossref", query, _PAPER_DISCOVERY, exc,
            )
            errors.append(error)
            _record_retry_failure(retry_queue, error, now)
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            scanned += 1
            candidate = _candidate(item, query, attempted_at)
            if candidate is None:
                continue
            relevant += 1
            identity = candidate["identity"]
            if identity in run_seen:
                duplicate += 1
                prior = candidates.get(identity)
                if prior is not None:
                    _merge_discovery(prior, candidate, query)
                continue
            run_seen.add(identity)
            if identity in known:
                duplicate += 1
                prior = candidates.get(identity)
                if prior is not None:
                    _merge_discovery(prior, candidate, query)
                continue
            candidates[identity] = candidate
            known.add(identity)
            new_count += 1

    if openalex_enabled:
        openalex_queries = [
            query for query in QUERIES
            if (
                _retry_key("OpenAlex", query, _PAPER_DISCOVERY)
                not in deferred_retry_keys
                and (
                    not retry_only or _retry_key(
                        "OpenAlex", query, _PAPER_DISCOVERY,
                    ) in due_retry_keys
                )
            )
        ]
        for query in openalex_queries:
            errors = _without_task_error(
                errors, "OpenAlex", query, _PAPER_DISCOVERY,
            )
            filters = _openalex_filters(
                existing, now, query, openalex_historical_completed,
            )
            openalex_filters_by_query[query] = filters
            try:
                payload = fetch_openalex(_openalex_url(query, filters))
                items = payload.get("results") or []
                if not isinstance(items, list):
                    raise ValueError("OpenAlex results is not a list")
                successful += 1
                paper_successes_this_run += 1
                openalex_historical_completed.add(query)
                retry_queue.pop(
                    _retry_key("OpenAlex", query, _PAPER_DISCOVERY), None,
                )
            except Exception as exc:
                error = _source_error(
                    "OpenAlex", query, _PAPER_DISCOVERY, exc,
                )
                errors.append(error)
                _record_retry_failure(retry_queue, error, now)
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                scanned += 1
                candidate = _openalex_candidate(item, query, attempted_at)
                if candidate is None:
                    continue
                relevant += 1
                identity = candidate["identity"]
                if identity in run_seen:
                    duplicate += 1
                    prior = candidates.get(identity)
                    if prior is not None:
                        _merge_discovery(prior, candidate, query)
                    continue
                run_seen.add(identity)
                if identity in known:
                    duplicate += 1
                    prior = candidates.get(identity)
                    if prior is not None:
                        _merge_discovery(prior, candidate, query)
                    continue
                candidates[identity] = candidate
                known.add(identity)
                new_count += 1

    expected_queries = len(QUERIES) * (2 if openalex_enabled else 1)
    candidate_values = list(candidates.values())
    input_snapshot_results = list(existing.get("input_snapshot_results") or [])
    if not retry_only and (
        fetcher is None or input_snapshot_fetcher is not None
    ):
        input_snapshot_results = [_input_snapshot_result(
            candidate_values, now=now, fetcher=input_snapshot_fetcher,
        )]
    repository_enabled = (
        not retry_only
        and (fetcher is None or repository_fetcher is not None)
    )
    repository_snapshot_results = list(
        existing.get("repository_snapshot_results") or []
    )
    repository_successful = (
        int(existing.get("repository_successful_checks") or 0)
        if retry_only else 0
    )
    if repository_enabled:
        repository_snapshot_results, repository_errors, repository_successful = (
            _repository_snapshots(
                existing, now=now,
                fetcher=repository_fetcher or _default_fetch,
            )
        )
        errors.extend(repository_errors)
    repository_discovery_enabled = (
        fetcher is None or repository_discovery_fetcher is not None
    )
    repository_discoveries = list(existing.get("repository_discoveries") or [])
    repository_discovery_successful = (
        int(existing.get("repository_discovery_successful_queries") or 0)
        if retry_only else 0
    )
    repository_discovery_new_count = (
        int(existing.get("repository_discovery_new_count") or 0)
        if retry_only else 0
    )
    repository_discovery_screened_out_count = (
        int(existing.get("repository_discovery_screened_out_count") or 0)
        if retry_only else 0
    )
    repository_discovery_successes_this_run = 0
    discovery_queries = [
        query for query in REPOSITORY_DISCOVERY_QUERIES
        if (
            _retry_key("GitHub", query, _REPOSITORY_DISCOVERY)
            not in deferred_retry_keys
            and (
                not retry_only or _retry_key(
                    "GitHub", query, _REPOSITORY_DISCOVERY,
                ) in due_retry_keys
            )
        )
    ]
    if repository_discovery_enabled and discovery_queries:
        for query in discovery_queries:
            errors = _without_task_error(
                errors, "GitHub", query, _REPOSITORY_DISCOVERY,
            )
        (
            repository_discoveries,
            repository_discovery_errors,
            discovery_successes,
            discovery_new,
            discovery_screened_out,
        ) = _repository_discoveries(
            existing, now=now,
            fetcher=repository_discovery_fetcher or _default_fetch,
            queries=discovery_queries,
        )
        failed_discovery_queries = {
            str(item.get("query") or "")
            for item in repository_discovery_errors
        }
        for query in discovery_queries:
            if query not in failed_discovery_queries:
                retry_queue.pop(
                    _retry_key("GitHub", query, _REPOSITORY_DISCOVERY), None,
                )
        for error in repository_discovery_errors:
            _record_retry_failure(retry_queue, error, now)
        errors.extend(repository_discovery_errors)
        repository_discovery_successful += discovery_successes
        repository_discovery_successes_this_run += discovery_successes
        repository_discovery_new_count += discovery_new
        repository_discovery_screened_out_count += discovery_screened_out
    expected_requests = expected_queries + (
        len(REPOSITORY_TARGETS) if repository_enabled else 0
    ) + (
        len(REPOSITORY_DISCOVERY_QUERIES)
        if repository_discovery_enabled else 0
    )
    successful_requests = (
        successful + repository_successful + repository_discovery_successful
    )
    successful_requests_this_run = (
        paper_successes_this_run
        + (repository_successful if repository_enabled else 0)
        + repository_discovery_successes_this_run
    )
    if retry_only:
        status = (
            "ok" if not retry_queue else
            "degraded" if existing.get("ok") or successful_requests else
            "unavailable"
        )
        ok = bool(existing.get("ok") or successful_requests)
    else:
        status = (
            "ok" if successful_requests == expected_requests else
            "degraded" if successful_requests else
            "unavailable"
        )
        ok = successful_requests > 0
    last_full_attempt_at = (
        existing.get("last_full_attempt_at") or existing.get("last_attempt_at")
        if retry_only else attempted_at
    )
    repository_expected_checks = (
        int(existing.get("repository_expected_checks") or 0)
        if retry_only and not repository_enabled
        else len(REPOSITORY_TARGETS) if repository_enabled else 0
    )
    if (
        retry_only
        and not repository_enabled
        and repository_expected_checks == 0
        and repository_snapshot_results
    ):
        repository_expected_checks = len(REPOSITORY_TARGETS)
    repository_status = (
        str(existing.get("repository_status") or "not_checked")
        if retry_only and not repository_enabled
        else "ok" if repository_successful == len(REPOSITORY_TARGETS)
        else "degraded" if repository_successful
        else "unavailable" if repository_enabled
        else "not_checked"
    )
    unreviewed_count = sum(
        str(item.get("status") or "") == "unreviewed_research_source"
        for item in candidate_values
    )
    result = {
        "schema_version": 2,
        "status": status,
        "ok": ok,
        "last_attempt_at": attempted_at,
        "last_full_attempt_at": last_full_attempt_at,
        "last_success_at": (
            attempted_at if successful_requests_this_run
            else existing.get("last_success_at")
        ),
        "next_due_at": _next_due_at(
            {**existing, "last_full_attempt_at": last_full_attempt_at},
            retry_queue,
            now,
        ),
        "run_mode": "retry_failed_only" if retry_only else "daily_full",
        "retry_queue": sorted(
            retry_queue.values(),
            key=lambda item: (
                str(item.get("next_due_at") or "9999"),
                str(item.get("provider") or ""),
                str(item.get("query") or ""),
            ),
        ),
        "retry_due_at": min(
            (
                str(item["next_due_at"])
                for item in retry_queue.values() if item.get("next_due_at")
            ),
            default=None,
        ),
        "retry_pending_count": len(retry_queue),
        "retry_exhausted_count": sum(
            item.get("next_due_at") is None for item in retry_queue.values()
        ),
        "retry_policy": {
            "base_seconds": RETRY_BASE_SECONDS,
            "max_seconds": RETRY_MAX_SECONDS,
            "daily_attempt_limit": RETRY_DAILY_ATTEMPT_LIMIT,
            "successful_queries_repeated": False,
            "retry_after_respected": True,
        },
        "source": {
            "provider": "Crossref",
            "endpoint": _CROSSREF_WORKS,
            "official_documentation": "https://www.crossref.org/documentation/retrieve-metadata/rest-api/",
            "api_key_required": False,
        },
        "sources": [
            {
                "provider": "Crossref", "endpoint": _CROSSREF_WORKS,
                "official_documentation": "https://www.crossref.org/documentation/retrieve-metadata/rest-api/",
                "api_key_required": False,
                "search_scope": "bibliographic metadata",
            },
            *([{
                "provider": "OpenAlex", "endpoint": _OPENALEX_WORKS,
                "official_documentation": "https://help.openalex.org/api/",
                "api_key_required_for_bounded_trial": False,
                "search_scope": "title, abstract and indexed full text",
            }] if openalex_enabled else []),
            *([_github_source()] if (
                repository_enabled or repository_discovery_enabled
            ) else []),
        ],
        "policy": {
            "research_only": True,
            "automatic_candidate_registration": False,
            "automatic_factor_computation": False,
            "automatic_backtest": False,
            "automatic_production": False,
            "abstracts_stored": False,
            "input_snapshots_are_research_only": True,
            "automatic_repository_candidate_registration": False,
            "automatic_repository_execution": False,
        },
        "query_count": expected_queries,
        "successful_query_count": successful,
        "repository_expected_checks": repository_expected_checks,
        "repository_successful_checks": repository_successful,
        "repository_status": repository_status,
        "repository_review_required_count": sum(
            item.get("review_required") is True
            for item in repository_snapshot_results
            if isinstance(item, dict)
        ),
        "repository_discovery_status": (
            "ok" if repository_discovery_successful == len(
                REPOSITORY_DISCOVERY_QUERIES
            ) else "degraded" if repository_discovery_successful else
            "unavailable" if repository_discovery_enabled else "not_checked"
        ),
        "repository_discovery_successful_queries": repository_discovery_successful,
        "repository_discovery_expected_queries": (
            len(REPOSITORY_DISCOVERY_QUERIES)
            if repository_discovery_enabled else 0
        ),
        "repository_discovery_new_count": repository_discovery_new_count,
        "repository_discovery_unreviewed_count": sum(
            str(item.get("status") or "") == "unreviewed_repository_source"
            for item in repository_discoveries if isinstance(item, dict)
        ),
        "repository_discovery_screened_out_count": (
            repository_discovery_screened_out_count
        ),
        "items_scanned": scanned,
        "relevant_items": relevant,
        "deduplicated_count": duplicate,
        "new_count": new_count,
        "unreviewed_count": unreviewed_count,
        "reviewed_count": len(candidate_values) - unreviewed_count,
        "closed_count": sum(
            str(item.get("status") or "").startswith("closed_")
            for item in candidate_values
        ),
        "formula_pending_count": sum(
            item.get("status") == "formula_pending" for item in candidate_values
        ),
        "source_contract_pending_count": sum(
            item.get("status") == "source_contract_pending"
            for item in candidate_values
        ),
        "screened_out_scope_count": len(screened_out),
        "filters": "per_query",
        "filters_by_query": query_filters,
        "openalex_filters_by_query": openalex_filters_by_query,
        "historical_queries_completed": [
            query for query in QUERIES if query in historical_completed
        ],
        "openalex_historical_queries_completed": [
            query for query in QUERIES if query in openalex_historical_completed
        ],
        "errors": errors,
        "input_snapshot_results": input_snapshot_results,
        "repository_snapshot_results": repository_snapshot_results,
        "repository_discoveries": repository_discoveries,
        "screened_out": screened_out,
        "candidates": _ordered_candidates(candidate_values),
    }
    _write(path, result, expected_sha=before_sha)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--once", action="store_true")
    action.add_argument("--apply-triage", type=Path)
    action.add_argument("--apply-repository-triage", type=Path)
    action.add_argument("--apply-manual-closed-intake", type=Path)
    action.add_argument("--apply-manual-candidate-intake", type=Path)
    action.add_argument("--apply-input-audit", type=Path)
    action.add_argument("--close-pending-candidate", type=Path)
    action.add_argument("--close-historical-candidate", type=Path)
    action.add_argument("--capture-input-snapshots", action="store_true")
    action.add_argument("--capture-repository-snapshots", action="store_true")
    action.add_argument("--discover-repositories", action="store_true")
    args = parser.parse_args()
    if args.apply_triage is not None:
        result = apply_triage(triage_path=args.apply_triage)
    elif args.apply_repository_triage is not None:
        result = apply_repository_triage(triage_path=args.apply_repository_triage)
    elif args.apply_manual_closed_intake is not None:
        result = apply_manual_closed_intake(intake_path=args.apply_manual_closed_intake)
    elif args.apply_manual_candidate_intake is not None:
        result = apply_manual_candidate_intake(
            intake_path=args.apply_manual_candidate_intake,
        )
    elif args.apply_input_audit is not None:
        result = apply_input_audit(audit_path=args.apply_input_audit)
    elif args.close_pending_candidate is not None:
        result = close_pending_candidate(closure_path=args.close_pending_candidate)
    elif args.close_historical_candidate is not None:
        result = close_historically_rejected_candidate(
            closure_path=args.close_historical_candidate,
        )
    elif args.capture_input_snapshots:
        result = capture_inputs_once()
    elif args.capture_repository_snapshots:
        result = capture_repository_snapshots_once()
    elif args.discover_repositories:
        result = capture_repository_discoveries_once()
    else:
        result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
