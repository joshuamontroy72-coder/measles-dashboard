"""
Measles Evidence Watch — pipeline orchestrator.

Run:
    python update.py            # incremental (keeps records already in evidence.json)
    python update.py --full     # full rebuild
    python update.py --no-enrich  # skip Altmetric enrichment

Writes (repo root, flat layout):
    evidence.json
    coverage.json

CHANGES in this version
  * Every record now gets a stable `id` (DOI > URL > title).  Previously the
    fetchers never set one, so records could not be matched between runs.
  * Near-duplicate news / press items are collapsed into one card
    (see dedupe.py); `duplicate_count` / `also_reported_by` are attached.
  * NACI / ACIP / JCVI / ATAGI / STIKO pages are promoted to the NITAG channel.
  * News older than NEWS_RETENTION_DAYS is dropped so the feed stays current.
  * coverage.json now carries `latest_signal` — one explicit, documented rule
    for the banner (see pick_latest_signal) — plus `latest_official_date`.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from classify import classify, is_relevant, passes_inclusion, POLICY_AREA_LABELS
from dedupe import dedupe, record_id
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

NEWS_RETENTION_DAYS = 120          # news / press older than this are dropped
LATEST_SIGNAL_MAX_AGE_DAYS = 45    # banner shows nothing older than this

# ---------------------------------------------------------------------------
# Channel definitions (order = display order in the UI)
# ---------------------------------------------------------------------------

CHANNELS: list[dict] = [
    {"id": "journals",     "label": "Journals (Europe PMC / PubMed)",   "icon": "📄"},
    {"id": "preprints",    "label": "Preprints (bioRxiv / medRxiv)",    "icon": "📋"},
    {"id": "trials",       "label": "ClinicalTrials.gov",               "icon": "🔬"},
    {"id": "who_sage",     "label": "WHO / SAGE guidance",              "icon": "🌐"},
    {"id": "nitag",        "label": "National NITAG guidance",          "icon": "🏛"},
    {"id": "surveillance", "label": "Surveillance & outbreak reports",  "icon": "📊"},
    {"id": "news",         "label": "News",                             "icon": "📰"},
    {"id": "press",        "label": "Press releases",                   "icon": "📣"},
]

CHANNEL_IDS = {c["id"] for c in CHANNELS}

SOURCE_TYPE_LABELS: dict[str, str] = {
    "journal":          "Journal article",
    "journal_article":  "Journal article",
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

_WHO_HOSTS = {"who.int", "iris.who.int", "searo.who.int"}
_NITAG_HOSTS = {
    "canada.ca", "phac-aspc.gc.ca", "healthycanadians.gc.ca",
    "cdc.gov", "ecdc.europa.eu",
    "gov.uk", "nhs.uk", "assets.publishing.service.gov.uk",
    "rki.de", "has-sante.fr", "nih.gov", "immunize.ca",
    "health.gov.au", "immunisation.health.gov.au", "nitag-resource.org",
}

# (host fragment, path fragment) pairs that identify a NITAG statement page
_NITAG_PAGE_PATTERNS = [
    ("canada.ca",     "national-advisory-committee-immunization"),
    ("canada.ca",     "national-advisory-committee-on-immunization"),
    ("canada.ca",     "canadian-immunization-guide"),
    ("cdc.gov",       "/acip"),
    ("cdc.gov",       "/vaccines/hcp/acip"),
    ("gov.uk",        "joint-committee-on-vaccination-and-immunisation"),
    ("gov.uk",        "jcvi"),
    ("health.gov.au", "atagi"),
    ("immunisation.health.gov.au", "atagi"),
    ("rki.de",        "stiko"),
    ("nitag-resource.org", ""),
]


def _host_in(url: str, host_set: set[str]) -> bool:
    try:
        host = urlparse(url or "").hostname or ""
        return any(h in host for h in host_set)
    except Exception:
        return False


def _is_nitag_page(url: str) -> bool:
    try:
        p = urlparse((url or "").lower())
        host, path = p.hostname or "", p.path
        return any(h in host and frag in path for h, frag in _NITAG_PAGE_PATTERNS)
    except Exception:
        return False


def promote_nitag(rec: dict) -> None:
    """A page on a NITAG statement URL is guidance, whatever feed it arrived in."""
    if rec.get("source_type") in ("journal", "journal_article", "preprint", "clinical_trial"):
        return
    if _is_nitag_page(rec.get("url", "")):
        rec["source_type"] = "guideline"


def channel_of(rec: dict) -> str:
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
        if _host_in(url, _WHO_HOSTS) or "who" in src.split() or "sage" in src or "iris" in src:
            return "who_sage"
        return "nitag"
    if st in ("journal", "journal_article"):
        return "journals"

    if _host_in(url, _WHO_HOSTS):
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


def finalize(rec: dict, reviewed_ids: set[str], today_iso: str) -> dict:
    """Ensure all UI-required fields are present."""
    rec["id"] = rec.get("id") or record_id(rec)
    rec.setdefault("first_seen", today_iso)

    # Normalize date. If a feed gave no date, fall back to when we first saw it
    # so the record sorts sensibly instead of sinking to 1900.
    if not rec.get("date"):
        rec["date"] = rec.get("published_date") or rec["first_seen"]

    promote_nitag(rec)
    classify(rec)

    # Channel is always re-derived (so reclassification fixes apply to old
    # records too) except for curated records, which are pinned.
    rec["channel"] = "nitag" if rec.get("curated") else channel_of(rec)

    rec["source_type_label"] = SOURCE_TYPE_LABELS.get(
        rec.get("source_type", "other"), "Other"
    )

    rec.setdefault("policy_area",        ["general_measles"])
    rec.setdefault("policy_area_labels", ["General measles"])
    rec.setdefault("pregnancy_related",  False)
    rec.setdefault("canadian",           False)
    rec.setdefault("species",            "na")

    rec["reviewed"] = rec["id"] in reviewed_ids

    from config import PHARMA_HOSTS
    try:
        host = urlparse(rec.get("url", "")).hostname or ""
        rec.setdefault("pharma", any(ph in host for ph in PHARMA_HOSTS))
    except Exception:
        rec.setdefault("pharma", False)

    return rec


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def _merge(existing: list[dict], new_records: list[dict]) -> list[dict]:
    """Merge new into existing by id (new wins; first_seen is preserved)."""
    by_id: dict[str, dict] = {}
    for r in existing:
        r["id"] = r.get("id") or record_id(r)
        by_id[r["id"]] = r
    for rec in new_records:
        rec["id"] = rec.get("id") or record_id(rec)
        old = by_id.get(rec["id"])
        if old:
            if old.get("first_seen"):
                rec["first_seen"] = old["first_seen"]
            # keep enrichment we already paid for
            for k in ("altmetric_score", "altmetric_url", "altmetric_reads", "altmetric_mentions"):
                if k in old and k not in rec:
                    rec[k] = old[k]
        by_id[rec["id"]] = rec
    return list(by_id.values())


def _record_date(r: dict) -> date | None:
    try:
        return date.fromisoformat((r.get("date") or r.get("published_date") or "")[:10])
    except Exception:
        return None


def prune_old_news(records: list[dict], today: date) -> list[dict]:
    cutoff = today - timedelta(days=NEWS_RETENTION_DAYS)
    out = []
    for r in records:
        if r.get("source_type") in ("news", "press_release") and not (
            r.get("reviewed") or r.get("curated")
        ):
            d = _record_date(r)
            if d and d < cutoff:
                continue
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# Latest signal (banner)
# ---------------------------------------------------------------------------

def pick_latest_signal(records: list[dict], today: date) -> tuple[dict | None, str | None]:
    """
    The banner shows the NEWEST *official* item: WHO/SAGE guidance, NITAG
    guidance, or an agency surveillance / outbreak report.  News stories and
    journal articles are never the banner.  Items dated in the future are
    ignored, and anything older than LATEST_SIGNAL_MAX_AGE_DAYS is not shown
    (the banner then says there has been no new official signal).
    Returns (signal_or_None, date_of_newest_official_item_or_None).
    """
    official = [
        r for r in records
        if r.get("channel") in ("who_sage", "nitag", "surveillance")
        and r.get("source_type") not in ("journal", "journal_article")
        and r.get("upgraded_from") not in ("news", "press_release")
        and (d := _record_date(r)) is not None and d <= today
    ]
    if not official:
        return None, None
    official.sort(key=lambda r: _record_date(r), reverse=True)
    best = official[0]
    best_date = _record_date(best)
    newest_iso = best_date.isoformat()
    if (today - best_date).days > LATEST_SIGNAL_MAX_AGE_DAYS:
        return None, newest_iso
    return {
        "id":       best["id"],
        "title":    best.get("title", ""),
        "url":      best.get("url", ""),
        "source":   best.get("source", ""),
        "channel":  best.get("channel", ""),
        "date":     newest_iso,
        "age_days": (today - best_date).days,
    }, newest_iso


# ---------------------------------------------------------------------------
# Coverage panel
# ---------------------------------------------------------------------------

def build_coverage(records: list[dict], fetcher_log: list[dict], today: date) -> dict:
    channel_counts: dict[str, int] = defaultdict(int)
    policy_counts:  dict[str, int] = defaultdict(int)

    for rec in records:
        ch = rec.get("channel", "news")
        if ch in CHANNEL_IDS:
            channel_counts[ch] += 1
        for area in rec.get("policy_area", ["general_measles"]):
            policy_counts[area] += 1

    latest, newest_official = pick_latest_signal(records, today)

    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "total":      len(records),
        "latest_signal": latest,
        "latest_official_date": newest_official,
        "latest_signal_max_age_days": LATEST_SIGNAL_MAX_AGE_DAYS,
        "channels": [
            {**ch, "count": channel_counts.get(ch["id"], 0)}
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
    today = datetime.now(timezone.utc).date()
    today_iso = today.isoformat()

    existing: list[dict] = []
    if EVIDENCE_PATH.exists() and not full:
        try:
            existing = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
            log.info("Loaded %d existing records", len(existing))
        except Exception as exc:
            log.warning("Could not load existing evidence: %s", exc)

    existing_ids: set[str] = {r.get("id") or record_id(r) for r in existing}
    reviewed_ids = _load_reviewed_ids()

    all_new: list[dict] = []
    fetcher_log: list[dict] = []

    for label, fetcher_fn in ALL_FETCHERS:
        log.info("Fetching: %s", label)
        try:
            records = fetcher_fn()
            log.info("  → %d raw records", len(records))
            kept = [r for r in records if is_relevant(r) and passes_inclusion(r)]
            log.info("  → %d after relevance filter", len(kept))
            all_new.extend(kept)
            fetcher_log.append({
                "source": label, "fetched": len(records), "kept": len(kept),
                "error": None, "fetched_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as exc:
            log.error("  FAILED %s: %s", label, exc, exc_info=True)
            fetcher_log.append({
                "source": label, "fetched": 0, "kept": 0, "error": str(exc),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            })

    for r in all_new:
        r["id"] = r.get("id") or record_id(r)

    merged = _merge(existing, all_new)
    log.info("Total records after merge: %d", len(merged))

    if not no_enrich:
        new_ids = {r["id"] for r in all_new} - existing_ids
        to_enrich = [r for r in merged if r["id"] in new_ids and r.get("doi")]
        if to_enrich:
            log.info("Enriching %d new records with Altmetric", len(to_enrich))
            try:
                enrich_altmetric(to_enrich)
            except Exception as exc:
                log.warning("Altmetric enrichment failed: %s", exc)

    for rec in merged:
        finalize(rec, reviewed_ids, today_iso)

    before = len(merged)
    merged = dedupe(merged)
    log.info("De-duplication removed %d records (%d remain)", before - len(merged), len(merged))

    merged = prune_old_news(merged, today)

    merged.sort(key=lambda r: r.get("date") or "1900-01-01", reverse=True)

    EVIDENCE_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Wrote %d records to %s", len(merged), EVIDENCE_PATH)

    coverage = build_coverage(merged, fetcher_log, today)
    COVERAGE_PATH.write_text(json.dumps(coverage, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Wrote coverage panel to %s", COVERAGE_PATH)
    log.info("=== Pipeline complete ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Measles Evidence Watch pipeline")
    parser.add_argument("--full",      action="store_true", help="Full rebuild")
    parser.add_argument("--no-enrich", action="store_true", help="Skip Altmetric")
    args = parser.parse_args()
    main(full=args.full, no_enrich=args.no_enrich)
