"""
Measles Evidence Watch — pipeline orchestrator.

Run:
    python update.py            # incremental (skips records already in evidence.json)
    python update.py --full     # full rebuild
    python update.py --no-enrich  # skip Altmetric enrichment

Writes:
    ../data/evidence.json
    ../data/coverage.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from classify import classify, is_relevant, passes_inclusion, POLICY_AREA_LABELS
from sources import ALL_FETCHERS, enrich_altmetric

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

ROOT = Path(__file__).parent

EVIDENCE_PATH = ROOT / "evidence.json"
COVERAGE_PATH = ROOT / "coverage.json"
REVIEWED_IDS_PATH = ROOT / "reviewed_ids.txt"

# ---------------------------------------------------------------------------
# Channel definitions (order = display order in the UI)
# ---------------------------------------------------------------------------

CHANNELS: list[dict] = [
    {
        "id":    "journals",
        "label": "Journals (Europe PMC / PubMed)",
        "icon":  "📄",
    },
    {
        "id":    "preprints",
        "label": "Preprints (bioRxiv / medRxiv)",
        "icon":  "📋",
    },
    {
        "id":    "trials",
        "label": "ClinicalTrials.gov",
        "icon":  "🔬",
    },
    {
        "id":    "who_sage",
        "label": "WHO / SAGE guidance",
        "icon":  "🌐",
    },
    {
        "id":    "nitag",
        "label": "National NITAG guidance",
        "icon":  "🏛",
    },
    {
        "id":    "surveillance",
        "label": "Surveillance & outbreak reports",
        "icon":  "📊",
    },
    {
        "id":    "news",
        "label": "News",
        "icon":  "📰",
    },
    {
        "id":    "press",
        "label": "Press releases",
        "icon":  "📣",
    },
]

CHANNEL_IDS = {c["id"] for c in CHANNELS}

# Human-readable source_type labels
# NOTE: sources.py writes "journal_article"; both keys map to the same label
SOURCE_TYPE_LABELS: dict[str, str] = {
    "journal":          "Journal article",
    "journal_article":  "Journal article",   # canonical from sources.py
    "preprint":         "Preprint",
    "clinical_trial":   "Clinical trial",
    "guideline":        "Guideline / policy document",
    "surveillance":     "Surveillance report",
    "outbreak_report":  "Outbreak / disease news",
    "news":             "News article",
    "press_release":    "Press release",
    "blog":             "Blog / commentary",
    "other":            "Other",
}


# ---------------------------------------------------------------------------
# Channel assignment
# ---------------------------------------------------------------------------

_WHO_HOSTS = {"who.int", "iris.who.int", "sage", "searo.who.int"}
_NITAG_HOSTS = {
    "canada.ca", "phac-aspc.gc.ca", "healthycanadians.gc.ca",
    "cdc.gov", "ecdc.europa.eu",
    "gov.uk", "nhs.uk", "assets.publishing.service.gov.uk",
    "rki.de", "has-sante.fr", "nih.gov", "immunize.ca",
}


def _host_in(url: str, host_set: set[str]) -> bool:
    """Check whether a URL's host is in the given set."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url or "").hostname or ""
        return any(h in host for h in host_set)
    except Exception:
        return False


def channel_of(rec: dict) -> str:
    """Map a record to one of the 8 channel IDs."""
    st   = rec.get("source_type", "other")
    url  = rec.get("url", "")
    src  = (rec.get("source") or "").lower()

    if st == "clinical_trial":
        return "trials"

    if st == "preprint":
        return "preprints"

    if st in ("outbreak_report", "surveillance"):
        return "surveillance"

    if st == "press_release":
        return "press"

    if st == "news":
        return "news"

    if st == "guideline":
        # Distinguish WHO/SAGE from national NITAGs
        if _host_in(url, _WHO_HOSTS) or "who" in src or "sage" in src or "iris" in src:
            return "who_sage"
        return "nitag"

    if st in ("journal", "journal_article"):
        return "journals"

    # Fallbacks by URL / source
    if _host_in(url, _WHO_HOSTS) or "who" in src:
        return "who_sage"
    if _host_in(url, _NITAG_HOSTS):
        return "nitag"

    return "news"


# ---------------------------------------------------------------------------
# Finalize a record (add derived fields used by the UI)
# ---------------------------------------------------------------------------

def _load_reviewed_ids() -> set[str]:
    if REVIEWED_IDS_PATH.exists():
        return {
            line.strip()
            for line in REVIEWED_IDS_PATH.read_text().splitlines()
            if line.strip()
        }
    return set()


def finalize(rec: dict, reviewed_ids: set[str]) -> dict:
    """Ensure all UI-required fields are present."""
    # Normalize date field: sources.py uses "published_date", seed uses "date"
    if not rec.get("date") and rec.get("published_date"):
        rec["date"] = rec["published_date"]

    # Normalize source_type: sources.py uses "journal_article", pipeline expects "journal"
    # Keep original as published_source_type, normalize to canonical key
    if rec.get("source_type") == "journal_article":
        rec["source_type"] = "journal_article"  # keep as-is; channel_of() now handles both

    # Classification (idempotent)
    classify(rec)

    # Channel
    rec.setdefault("channel", channel_of(rec))

    # Source-type label
    rec["source_type_label"] = SOURCE_TYPE_LABELS.get(
        rec.get("source_type", "other"), "Other"
    )

    # Policy area labels (list) — already set by classify(), but safe to default
    rec.setdefault("policy_area",        ["general_measles"])
    rec.setdefault("policy_area_labels", ["General measles"])
    rec.setdefault("pregnancy_related",  False)
    rec.setdefault("canadian",           False)
    rec.setdefault("species",            "na")

    # Reviewed flag
    rec["reviewed"] = rec.get("id", "") in reviewed_ids

    # Pharma flag (check url)
    from config import PHARMA_HOSTS
    try:
        from urllib.parse import urlparse
        host = urlparse(rec.get("url", "")).hostname or ""
        rec.setdefault("pharma", any(ph in host for ph in PHARMA_HOSTS))
    except Exception:
        rec.setdefault("pharma", False)

    return rec


# ---------------------------------------------------------------------------
# Merge helpers
# ---------------------------------------------------------------------------

def _merge(existing: list[dict], new_records: list[dict]) -> list[dict]:
    """Merge new records into existing, dedup by id (new wins on conflict)."""
    by_id: dict[str, dict] = {r["id"]: r for r in existing if r.get("id")}
    for rec in new_records:
        rid = rec.get("id")
        if rid:
            by_id[rid] = rec
        else:
            by_id[id(rec)] = rec  # fallback: keep anyway
    return list(by_id.values())


# ---------------------------------------------------------------------------
# Coverage panel
# ---------------------------------------------------------------------------

def build_coverage(records: list[dict], fetcher_log: list[dict]) -> dict:
    """Build the coverage panel written to coverage.json."""
    channel_counts: dict[str, int] = defaultdict(int)
    policy_counts:  dict[str, int] = defaultdict(int)

    for rec in records:
        ch = rec.get("channel", "news")
        if ch in CHANNEL_IDS:
            channel_counts[ch] += 1

        for area in rec.get("policy_area", ["general_measles"]):
            policy_counts[area] += 1

    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "total":      len(records),
        "channels": [
            {
                **ch,
                "count": channel_counts.get(ch["id"], 0),
            }
            for ch in CHANNELS
        ],
        "policy_areas": [
            {
                "id":    area,
                "label": POLICY_AREA_LABELS.get(area, area),
                "count": policy_counts.get(area, 0),
            }
            for area in ["pregnancy_mmr", "pregnancy_infection", "schedule_timing", "general_measles"]
        ],
        "fetcher_log": fetcher_log,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(full: bool = False, no_enrich: bool = False) -> None:
    log.info("=== Measles Evidence Watch pipeline start ===")
    log.info("Mode: %s", "full rebuild" if full else "incremental")

    # Load existing records
    existing: list[dict] = []
    if EVIDENCE_PATH.exists() and not full:
        try:
            existing = json.loads(EVIDENCE_PATH.read_text())
            log.info("Loaded %d existing records", len(existing))
        except Exception as exc:
            log.warning("Could not load existing evidence: %s", exc)

    existing_ids: set[str] = {r["id"] for r in existing if r.get("id")}
    reviewed_ids = _load_reviewed_ids()

    # Run fetchers
    all_new: list[dict] = []
    fetcher_log: list[dict] = []

    for label, fetcher_fn in ALL_FETCHERS:
        log.info("Fetching: %s", label)
        try:
            records = fetcher_fn()
            log.info("  → %d raw records", len(records))

            kept = []
            for rec in records:
                if not is_relevant(rec):
                    continue
                if not passes_inclusion(rec):
                    continue
                kept.append(rec)

            log.info("  → %d after relevance filter", len(kept))
            all_new.extend(kept)

            fetcher_log.append({
                "source":      label,
                "fetched":     len(records),
                "kept":        len(kept),
                "error":       None,
                "fetched_at":  datetime.now(timezone.utc).isoformat(),
            })
        except Exception as exc:
            log.error("  FAILED %s: %s", label, exc, exc_info=True)
            fetcher_log.append({
                "source":     label,
                "fetched":    0,
                "kept":       0,
                "error":      str(exc),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            })

    # Merge
    merged = _merge(existing, all_new)
    log.info("Total records after merge: %d", len(merged))

    # Enrich with Altmetric (only new records)
    if not no_enrich:
        new_ids = {r["id"] for r in all_new if r.get("id")} - existing_ids
        to_enrich = [r for r in merged if r.get("id") in new_ids]
        if to_enrich:
            log.info("Enriching %d new records with Altmetric", len(to_enrich))
            try:
                enrich_altmetric(to_enrich)
            except Exception as exc:
                log.warning("Altmetric enrichment failed: %s", exc)

    # Finalize
    for rec in merged:
        finalize(rec, reviewed_ids)

    # Sort: newest first (by date). sources.py stores "published_date"; seed stores "date".
    def _sort_key(r: dict) -> str:
        return r.get("date") or r.get("published_date") or r.get("published") or "1900-01-01"

    merged.sort(key=_sort_key, reverse=True)

    # Write outputs
    EVIDENCE_PATH.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info("Wrote %d records to %s", len(merged), EVIDENCE_PATH)

    coverage = build_coverage(merged, fetcher_log)
    COVERAGE_PATH.write_text(
        json.dumps(coverage, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info("Wrote coverage panel to %s", COVERAGE_PATH)
    log.info("=== Pipeline complete ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Measles Evidence Watch pipeline")
    parser.add_argument("--full",       action="store_true", help="Full rebuild")
    parser.add_argument("--no-enrich",  action="store_true", help="Skip Altmetric")
    args = parser.parse_args()
    main(full=args.full, no_enrich=args.no_enrich)
