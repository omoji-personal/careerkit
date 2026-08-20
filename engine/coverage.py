"""Privacy-safe facts about the configured sourcing surface.

This module deliberately does no I/O.  Callers load the registry, optional
credentials, and optional ``source_health`` rows, then pass them here.  The
returned ledger is made only of JSON-compatible counts and labels: credential
values, raw health errors, job descriptions, and profile criteria are never
copied into it. Health errors are reduced to ``partial``, ``capped``, or
``failed``; callers may also request privacy-safe ``unpolled`` and ``stale``
states so a coverage gap is visible without echoing remote or private text.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from . import adapters, aggregators

__all__ = ["build_coverage_ledger"]


def _value(row: Any, key: str, default: Any = None) -> Any:
    """Read mappings and sqlite3.Row objects without assuming ``.get``."""
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _active(entry: Mapping[str, Any]) -> bool:
    return bool(entry.get("active", True))


def _feed_active(entry: Mapping[str, Any]) -> bool:
    if str(entry.get("name") or "").strip().casefold() == "freehire":
        # Freehire sends profile search terms to a third-party discovery API,
        # so omission can never inherit the legacy feed default of enabled.
        return entry.get("active") is True
    return bool(entry.get("active", True))


def _employer_label(entry: Mapping[str, Any]) -> str:
    ats = str(entry.get("ats") or "").strip().casefold()
    return f"{ats}:{str(entry.get('name') or '').strip()}"


def _feed_label(name: str) -> str:
    return f"feed:{name}"


def _missing_feed_fields(
    feed: Mapping[str, Any],
    keys: Mapping[str, Any],
    required: Iterable[str],
) -> list[str]:
    """Mirror pull-time key precedence without copying credential values."""
    private = keys.get(str(feed.get("name") or ""), {})
    private = private if isinstance(private, Mapping) else {}
    missing = []
    for field in required:
        value = private.get(field) if field in private else feed.get(field)
        if not value:
            missing.append(field)
    return sorted(missing)


def _feed_entry(
    feed: Mapping[str, Any],
    keys: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Classify one active feed as operational, dormant, or unsupported."""
    name = str(feed.get("name") or "").strip()
    label = _feed_label(name)
    policy = aggregators.policy(name)
    kind = str(policy.get("kind") or "unknown")

    if name not in aggregators.FEEDS or not policy.get("supported", True):
        return "unsupported", {
            "label": label,
            "name": name,
            "kind": kind,
            "reason": "unsupported_feed",
        }

    required = tuple(aggregators.FEED_KEYS.get(name, ()))
    missing = _missing_feed_fields(feed, keys, required)
    if missing:
        return "dormant", {
            "label": label,
            "name": name,
            "kind": kind,
            "reason": "missing_required_keys",
            "missing_fields": missing,
        }

    return "operational", {"label": label, "name": name, "kind": kind}


def _health_by_source(rows: Iterable[Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Index health rows exactly and case-insensitively.

    The exact index lets a stable lowercase board id win over a stale legacy
    display-name key that differs only by case. The folded index remains a
    compatibility fallback for databases whose stable keys predate canonical
    casing. Within either index, keep the largest known count as before.
    """
    exact: dict[str, Any] = {}
    folded: dict[str, Any] = {}

    def retain(index: dict[str, Any], source: str, row: Any) -> None:
        try:
            count = int(_value(row, "last_count", -1))
        except (TypeError, ValueError):
            count = -1
        prior = index.get(source)
        try:
            prior_count = int(_value(prior, "last_count", -1)) if prior is not None else -1
        except (TypeError, ValueError):
            prior_count = -1
        if prior is None or count > prior_count:
            index[source] = row

    for row in rows:
        source = str(_value(row, "source", "") or "").strip()
        if not source:
            continue
        retain(exact, source, row)
        retain(folded, source.casefold(), row)
    return exact, folded


def _source_health_row(
    exact: Mapping[str, Any],
    folded: Mapping[str, Any],
    stable_source: str,
    legacy_source: str = "",
) -> Any:
    """Resolve current health with exact stable identity taking precedence."""
    row = exact.get(stable_source)
    if row is not None:
        return row
    if legacy_source:
        row = exact.get(legacy_source)
        if row is not None:
            return row
    row = folded.get(stable_source.casefold())
    if row is not None:
        return row
    return folded.get(legacy_source.casefold()) if legacy_source else None


def _health_gap(row: Any) -> str:
    """Reduce a raw current error to a privacy-safe coverage state."""
    error = str(_value(row, "last_error", "") or "").strip().casefold()
    if not error:
        return ""
    if error.startswith("partial:"):
        return "partial"
    if error.startswith("capped:"):
        return "capped"
    return "failed"


def _health_state(row: Any, as_of: date | None, stale_after_days: int) -> str:
    """Return a privacy-safe current state, including per-source freshness."""
    if row is None:
        return "unpolled"
    gap = _health_gap(row)
    if gap:
        return gap
    if as_of is None:
        return ""
    last_ok = str(_value(row, "last_ok", "") or "").strip()
    try:
        last_ok_date = date.fromisoformat(last_ok[:10])
    except ValueError:
        return "unpolled" if not last_ok else "stale"
    return "stale" if (as_of - last_ok_date).days >= stale_after_days else ""


def _count(row: Any) -> int:
    try:
        return max(0, int(_value(row, "last_count", 0)))
    except (TypeError, ValueError):
        return 0


def build_coverage_ledger(
    registry: Mapping[str, Any],
    source_health: Iterable[Any] | None = None,
    *,
    keys: Mapping[str, Any] | None = None,
    as_of: date | None = None,
    stale_after_days: int = 7,
    search_term_count: int | None = None,
) -> dict[str, Any]:
    """Return a deterministic, JSON-compatible ledger of source coverage.

    ``keys`` may be the loaded private key mapping.  It is inspected only for
    the presence of fields required by a feed; neither values nor the mapping
    itself can appear in the result.  ``source_health`` may contain dictionaries
    or ``sqlite3.Row`` instances. Pass ``as_of`` to classify sources whose last
    successful poll is at least ``stale_after_days`` old.
    """
    employers = [
        entry for entry in (registry.get("employers") or [])
        if isinstance(entry, Mapping) and _active(entry)
    ]
    configured_feeds = [
        entry for entry in (registry.get("feeds") or [])
        if isinstance(entry, Mapping)
    ]
    feeds = [
        entry for entry in configured_feeds
        if isinstance(entry, Mapping) and _feed_active(entry)
    ]

    board_rows: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for entry in employers:
        board_rows[adapters.board_id(dict(entry)).casefold()].append(entry)

    duplicate_boards = []
    for board, entries in sorted(board_rows.items()):
        if len(entries) < 2:
            continue
        labels = sorted(
            (_employer_label(entry) for entry in entries),
            key=lambda label: (label.casefold(), label),
        )
        duplicate_boards.append({
            "board_id": board,
            "row_count": len(entries),
            "duplicate_rows": len(entries) - 1,
            "labels": labels,
        })

    supported = sorted({str(name).strip().casefold() for name in adapters.REGISTRY})
    represented = sorted({str(entry.get("ats") or "").strip().casefold()
                          for entry in employers if str(entry.get("ats") or "").strip()})
    supported_set, represented_set = set(supported), set(represented)

    feed_rows: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for feed in feeds:
        feed_rows[str(feed.get("name") or "").strip().casefold()].append(feed)

    duplicate_feeds = []
    for name, entries in sorted(feed_rows.items()):
        if len(entries) < 2:
            continue
        labels = sorted(
            (_feed_label(str(entry.get("name") or "").strip()) for entry in entries),
            key=lambda label: (label.casefold(), label),
        )
        duplicate_feeds.append({
            "feed_id": _feed_label(name),
            "row_count": len(entries),
            "duplicate_rows": len(entries) - 1,
            "labels": labels,
        })

    safe_keys: Mapping[str, Any] = keys if isinstance(keys, Mapping) else {}
    feed_groups: dict[str, list[dict[str, Any]]] = {
        "operational": [], "dormant": [], "unsupported": [],
    }
    for name, entries in sorted(feed_rows.items()):
        # A canonical exact-case name wins when duplicate rows differ only by
        # case. This mirrors the lowercase feed registry and keys.yaml layout.
        feed = min(
            entries,
            key=lambda entry: (
                str(entry.get("name") or "").strip() != name,
                str(entry.get("name") or "").strip().casefold(),
                str(entry.get("name") or "").strip(),
            ),
        )
        if (search_term_count is not None and search_term_count <= 0
                and name in aggregators.TERM_FEEDS):
            group, item = "dormant", {
                "label": _feed_label(name),
                "name": name,
                "kind": str(aggregators.policy(name).get("kind") or "unknown"),
                "reason": "missing_search_terms",
                "missing_fields": ["profile.search_terms"],
            }
        else:
            group, item = _feed_entry(feed, safe_keys)
        feed_groups[group].append(item)

    # Unlike ordinary inactive feeds, Freehire is shipped as a visible,
    # explicit opt-in coverage choice.  Keep it out of active/operational
    # counts, while still showing that this configured discovery channel was
    # not searched. Missing ``active`` and literal false are equivalent.
    active_feed_names = set(feed_rows)
    disabled_opt_in_names = {
        str(entry.get("name") or "").strip().casefold()
        for entry in configured_feeds
        if str(entry.get("name") or "").strip().casefold() == "freehire"
        and not _feed_active(entry)
    } - active_feed_names
    for name in sorted(disabled_opt_in_names):
        policy = aggregators.policy(name)
        feed_groups["dormant"].append({
            "label": _feed_label(name),
            "name": name,
            "kind": str(policy.get("kind") or "unknown"),
            "reason": "explicit_opt_in_disabled",
            "missing_fields": ["active: true (explicit opt-in)"],
        })
    for items in feed_groups.values():
        items.sort(key=lambda item: (item["label"].casefold(), item["label"]))

    health_exact, health_folded = _health_by_source(source_health or ())
    health_gaps = []
    for entry in sorted(
            employers,
            key=lambda item: (_employer_label(item).casefold(), _employer_label(item))):
        label = _employer_label(entry)
        board = adapters.board_id(dict(entry)).casefold()
        # Stable endpoint identity is authoritative once it exists. A stale
        # display-name row may remain during migration and must not override it.
        row = _source_health_row(health_exact, health_folded, board, label)
        state = _health_state(row, as_of, stale_after_days)
        if state:
            health_gaps.append({
                "label": label,
                "source_id": board,
                "state": state,
                "last_count": _count(row),
            })
    operational_names = {item["name"] for item in feed_groups["operational"]}
    for name in sorted(operational_names, key=str.casefold):
        source_id = _feed_label(name).casefold()
        row = _source_health_row(health_exact, health_folded, source_id)
        state = _health_state(row, as_of, stale_after_days)
        if state:
            health_gaps.append({
                "label": _feed_label(name),
                "source_id": source_id,
                "state": state,
                "last_count": _count(row),
            })

    capped = []
    for entry in sorted(
            employers,
            key=lambda item: (_employer_label(item).casefold(), _employer_label(item))):
        ats = str(entry.get("ats") or "").strip().casefold()
        ceiling = adapters.PAGE_CEILING.get(ats)
        if ceiling is None:
            continue
        label = _employer_label(entry)
        board = adapters.board_id(dict(entry)).casefold()
        row = _source_health_row(health_exact, health_folded, board, label)
        if row is None:
            continue
        try:
            count = int(_value(row, "last_count", -1))
        except (TypeError, ValueError):
            continue
        if count < ceiling:
            continue
        capped.append({
            "label": label,
            "board_id": board,
            "ats": ats,
            "last_count": count,
            "ceiling": ceiling,
        })

    return {
        "employers": {
            "active_rows": len(employers),
            "unique_board_ids": len(board_rows),
            "duplicate_rows": len(employers) - len(board_rows),
            "duplicate_boards": duplicate_boards,
        },
        "ats_families": {
            "supported": supported,
            "represented": represented,
            "supported_unrepresented": sorted(supported_set - represented_set),
            "represented_unsupported": sorted(represented_set - supported_set),
        },
        "feeds": {
            "active_rows": len(feeds),
            "unique_count": len(feed_rows),
            "duplicate_rows": len(feeds) - len(feed_rows),
            "duplicate_feeds": duplicate_feeds,
            "operational_count": len(feed_groups["operational"]),
            "operational": feed_groups["operational"],
            "dormant_count": len(feed_groups["dormant"]),
            "dormant": feed_groups["dormant"],
            "unsupported_count": len(feed_groups["unsupported"]),
            "unsupported": feed_groups["unsupported"],
        },
        "page_ceiling_count": len(capped),
        "page_ceiling_sources": capped,
        "health_gap_count": len(health_gaps),
        "health_gap_sources": health_gaps,
    }
