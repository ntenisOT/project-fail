"""Exact candidate, receipt-attribution, and target-event validation."""

from __future__ import annotations

from typing import Any, Mapping

from tools.evidence_provenance import (
    ATTRIBUTION_PRODUCER_PATHS,
    CANDIDATE_PRODUCER_PATHS,
)
from tools.mint_accounting_inputs import (
    EvidenceError,
    GIT_RE,
    canonical,
    digest,
    integer,
    sha256,
    validated_revision_manifest,
)


STANDARD_ADAPTER = "0xada100db00ca00073811820692005400218fce1f"
OLD_FACTORY = "0xada100874d00e3331d00f2007a9c336a65009718"
NEG_RISK_ADAPTER = "0xada2005600dec949baf300f4c6120000bdb6eaab"
CANDIDATE_CORE_FIELDS = (
    "source_block_number", "source_log_index", "source_block_timestamp", "tx_hash",
    "condition_id", "op", "adapter", "amount", "token_ids",
)
CANDIDATE_EXPORT_FIELDS = frozenset({
    *CANDIDATE_CORE_FIELDS, "adapter_kind", "candidate_scope", "same_tx_clob",
})


def _candidate_key(row: Mapping[str, object]) -> tuple[object, ...]:
    tokens = row.get("token_ids")
    if not isinstance(tokens, list) or len(tokens) != 2:
        raise EvidenceError("candidate token_ids must contain exactly two tokens")
    return (
        str(row.get("adapter") or "").lower(), str(row.get("amount")),
        str(row.get("condition_id") or "").lower(), str(row.get("op") or ""),
        integer(row.get("source_block_number"), "source_block_number"),
        integer(row.get("source_block_timestamp"), "source_block_timestamp"),
        integer(row.get("source_log_index"), "source_log_index"),
        tuple(str(token) for token in tokens), str(row.get("tx_hash") or "").lower(),
    )


def _validated_producer_manifest(
    value: object,
    source_paths: tuple[str, ...],
    runtime_fields: tuple[str, ...],
    expected_revision: Mapping[str, object],
    label: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise EvidenceError(f"{label} revision manifest is missing")
    head = str(value.get("git_head") or "").lower()
    raw_sources, raw_runtime = value.get("source_sha256"), value.get("runtime")
    if (not GIT_RE.fullmatch(head) or not isinstance(raw_sources, Mapping)
            or set(raw_sources) != set(source_paths) or not isinstance(raw_runtime, Mapping)
            or set(raw_runtime) != set(runtime_fields)):
        raise EvidenceError(f"{label} revision manifest has an invalid shape")
    sources = {
        name: digest(str(raw_sources[name]), f"{label} source {name}")
        for name in source_paths
    }
    runtime = {name: str(raw_runtime[name]) for name in runtime_fields}
    if any(not value for value in runtime.values()):
        raise EvidenceError(f"{label} runtime manifest contains an empty value")
    frozen: dict[str, object] = {
        "git_head": head, "source_sha256": sources, "runtime": runtime,
    }
    manifest: dict[str, object] = {
        **frozen, "revision_sha256": sha256(canonical(frozen)),
    }
    if value.get("revision_sha256") != manifest["revision_sha256"]:
        raise EvidenceError(f"{label} revision manifest hash is invalid")
    expected = validated_revision_manifest(expected_revision)
    expected_sources = expected["source_sha256"]
    expected_runtime = expected["runtime"]
    if (head != expected["git_head"] or not isinstance(expected_sources, Mapping)
            or any(sources[name] != expected_sources.get(name) for name in source_paths)
            or not isinstance(expected_runtime, Mapping)
            or any(runtime[name] != expected_runtime.get(name) for name in runtime_fields)):
        raise EvidenceError(f"{label} producer differs from the accounting revision")
    return manifest


def validated_attribution_artifact(
    candidate: Mapping[str, object],
    attribution: Mapping[str, object],
    candidate_sha: str,
    expected_revision: Mapping[str, object],
) -> tuple[list[Mapping[str, object]], list[Mapping[str, object]]]:
    """Require a source-pinned, bijective candidate-to-attribution result set."""
    _validated_producer_manifest(
        candidate.get("generator"), CANDIDATE_PRODUCER_PATHS,
        ("python_implementation", "python_version", "clickhouse_connect_version"),
        expected_revision, "candidate",
    )
    query = candidate.get("query")
    candidate_query = candidate.get("candidate_query")
    if not isinstance(query, Mapping) or not isinstance(candidate_query, Mapping):
        raise EvidenceError("candidate query provenance is missing")
    if sha256(canonical(candidate_query)) != query.get("candidate_query_sha256"):
        raise EvidenceError("candidate query hash is invalid")
    candidates = candidate.get("candidates")
    candidate_counts = candidate.get("counts")
    if (not isinstance(candidates, list) or not isinstance(candidate_counts, Mapping)
            or integer(candidate_counts.get("candidates"), "candidate count") != len(candidates)
            or any(not isinstance(row, Mapping) for row in candidates)):
        raise EvidenceError("candidate rows do not match their declared count")
    candidate_rows = [row for row in candidates if isinstance(row, Mapping)]
    source: dict[tuple[object, ...], Mapping[str, object]] = {}
    for row in candidate_rows:
        if set(row) != CANDIDATE_EXPORT_FIELDS:
            raise EvidenceError("candidate exporter row has an invalid shape")
        adapter = str(row.get("adapter") or "").lower()
        expected_metadata = {
            OLD_FACTORY: ("legacy_clob_factory", "old_factory_without_same_tx_clob"),
            STANDARD_ADAPTER: ("standard", "current_adapter_lifecycle"),
            NEG_RISK_ADAPTER: ("neg_risk", "current_adapter_lifecycle"),
        }.get(adapter)
        if (expected_metadata is None
                or (row.get("adapter_kind"), row.get("candidate_scope")) != expected_metadata
                or not isinstance(row.get("same_tx_clob"), bool)
                or (adapter == OLD_FACTORY and row.get("same_tx_clob") is not False)):
            raise EvidenceError("candidate exporter metadata is inconsistent")
        key = _candidate_key(row)
        if key in source:
            raise EvidenceError("candidate rows are duplicated")
        source[key] = row

    if attribution.get("input_sha256") != digest(candidate_sha, "candidate SHA-256"):
        raise EvidenceError("attribution does not bind the candidate artifact")
    attribution_query = attribution.get("query")
    if (not isinstance(attribution_query, Mapping)
            or canonical(attribution_query) != canonical(query)
            or attribution.get("query_sha256") != sha256(canonical(query))):
        raise EvidenceError("attribution query does not exactly bind the candidate query")
    settings = attribution.get("settings")
    if (not isinstance(settings, Mapping)
            or settings.get("amount_tolerance_base_units") != 0
            or integer(settings.get("max_candidates"), "attribution max candidates") < len(source)):
        raise EvidenceError("attribution zero-tolerance settings are missing or too narrow")
    digest(str(attribution.get("rpc_endpoint_sha256") or ""), "attribution RPC SHA-256")
    _validated_producer_manifest(
        attribution.get("revision"), ATTRIBUTION_PRODUCER_PATHS,
        ("python_implementation", "python_version"), expected_revision, "attribution",
    )
    results = attribution.get("results")
    counts = attribution.get("counts")
    if (not isinstance(results, list) or not isinstance(counts, Mapping)
            or any(not isinstance(row, Mapping) for row in results)):
        raise EvidenceError("attribution results or counts are malformed")
    result_rows = [row for row in results if isinstance(row, Mapping)]
    joined: set[tuple[object, ...]] = set()
    classifications = {"clob_atomic": 0, "explicit_wallet": 0, "unresolved": 0}
    for result in result_rows:
        raw_candidate = result.get("candidate")
        proof = result.get("attribution")
        if not isinstance(raw_candidate, Mapping) or not isinstance(proof, Mapping):
            raise EvidenceError("attribution result lacks a candidate or proof")
        key = _candidate_key(raw_candidate)
        original = source.get(key)
        expected_projection = (
            None if original is None else
            {name: original[name] for name in CANDIDATE_CORE_FIELDS}
        )
        if (original is None or key in joined
                or set(raw_candidate) != set(CANDIDATE_CORE_FIELDS)
                or canonical(raw_candidate) != canonical(expected_projection)):
            raise EvidenceError("attribution results do not bijectively rejoin exact candidates")
        joined.add(key)
        classification = str(proof.get("classification") or "")
        if classification not in classifications:
            raise EvidenceError("attribution result has an unknown classification")
        classifications[classification] += 1
        digest(str(result.get("receipt_sha256") or ""), "receipt SHA-256")
    expected_counts = {
        "candidates": len(source),
        "transactions": len({str(row.get("tx_hash") or "").lower() for row in source.values()}),
        **classifications,
    }
    if joined != set(source) or len(result_rows) != len(source):
        raise EvidenceError("attribution results omit or duplicate candidate rows")
    if set(counts) != set(expected_counts) or any(
        integer(counts.get(name), f"attribution count {name}") != value
        for name, value in expected_counts.items()
    ):
        raise EvidenceError("attribution counts do not reconcile to exact results")
    return candidate_rows, result_rows


def attributed_events(
    candidate: Mapping[str, object], attribution: Mapping[str, object], wallet: str,
    candidate_sha: str, expected_revision: Mapping[str, object],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    candidates, results = validated_attribution_artifact(
        candidate, attribution, candidate_sha, expected_revision
    )
    source = {_candidate_key(row): row for row in candidates}
    selected: list[dict[str, Any]] = []
    receipts: dict[str, str] = {}
    for result in results:
        proof = result["attribution"]
        if not isinstance(proof, Mapping):
            raise EvidenceError("malformed attribution proof")
        addresses = {str(proof.get(name) or "").lower() for name in ("wallet", "counterparty")}
        if wallet not in addresses:
            continue
        if addresses != {wallet} or proof.get("classification") != "explicit_wallet":
            raise EvidenceError("target attribution is not an explicit single-wallet proof")
        raw_row = result.get("candidate")
        if not isinstance(raw_row, Mapping) or _candidate_key(raw_row) not in source:
            raise EvidenceError("attribution does not rejoin an exact candidate")
        row = dict(raw_row)
        if str(row.get("adapter") or "").lower() != STANDARD_ADAPTER:
            raise EvidenceError("target event is not the standard adapter")
        receipt_hash = digest(str(result.get("receipt_sha256")), "receipt_sha256")
        tx_hash = str(row.get("tx_hash") or "").lower()
        if tx_hash in receipts:
            raise EvidenceError("duplicate target receipt transaction")
        receipts[tx_hash] = receipt_hash
        selected.append(row)
    if not selected:
        raise EvidenceError("no target receipt-attributed lifecycle events")
    return selected, receipts
