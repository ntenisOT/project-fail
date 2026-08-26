"""Shared executed-source manifests for receipt evidence producers."""

CANDIDATE_PRODUCER_PATHS = (
    "tools/adapter_receipt_candidates.py",
    "tools/clickhouse_forensics.py",
    "tools/crossvenue_dataset.py",
    "tools/crossvenue_gaps.py",
    "tools/evidence_provenance.py",
    "tools/market_windows.py",
    "tools/top_setters.py",
    "tools/transport_telemetry.py",
    "tools/wallet_metrics.py",
    "tools/winner_artifacts.py",
    "requirements.txt",
)

ATTRIBUTION_PRODUCER_PATHS = (
    "tools/adapter_receipt_attributor.py",
    "tools/adapter_receipt_core.py",
    "tools/adapter_receipt_input.py",
    "tools/evidence_provenance.py",
)
