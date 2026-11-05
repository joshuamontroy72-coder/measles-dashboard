"""
Classification logic for the Measles Evidence Watch pipeline.

Each record is tagged with:
  policy_area   — list: "pregnancy_mmr" | "pregnancy_infection" |
                         "schedule_timing" | "general_measles"
  pregnancy_related — bool: either pregnancy_mmr or pregnancy_infection
  canadian          — bool
  species           — "human" | "animal" | "na"

Relevance filter: record must have a measles/MMR/rubella signal in its text.
Inclusion rule: all relevant records are kept (no animal-only exclusion —
  animal immunogenicity studies are useful for schedule questions).

CHANGES in this version
  * The "surveillance" keyword upgrade no longer applies to journal articles,
    news or press releases.  Before, any paper whose abstract said
    "surveillance system" (e.g. an outbreak cohort study in BMJ) was
    re-labelled as a surveillance report and disappeared from the Journals
    channel.
  * Curated records always pass the relevance filter.
"""

from __future__ import annotations
import re

from config import (
    PREGNANCY_MMR_KEYWORDS,
    PREGNANCY_INFECTION_KEYWORDS,
    SCHEDULE_KEYWORDS,
    SURVEILLANCE_KEYWORDS,
    MEASLES_SCOPE_KEYWORDS,
    ANIMAL_KEYWORDS,
    HUMAN_KEYWORDS,
    CANADA_KEYWORDS,
)

# source_types that must never be re-labelled by the keyword upgrade below
_PROTECTED_TYPES = (
    "guideline", "clinical_trial", "preprint",
    "journal", "journal_article", "news", "press_release",
    "surveillance", "outbreak_report",
)


def _text(rec: dict) -> str:
    """Lower-cased concatenation of searchable fields."""
    parts = [
        rec.get("title", ""),
        rec.get("summary", ""),
        rec.get("journal", ""),
        rec.get("authors", ""),
        rec.get("affiliations", ""),
        rec.get("source", ""),
        (rec.get("extra") or {}).get("conditions", ""),
        (rec.get("extra") or {}).get("interventions", ""),
    ]
    return " ".join(p for p in parts if isinstance(p, str) and p).lower()


def _has(text: str, keywords: list[str]) -> bool:
    return any(kw in text for kw in keywords)


def _has_word(text: str, keywords: list[str]) -> bool:
    """Match whole-word / phrase (avoids 'measles' matching 'rubella-measles-mumps')."""
    for kw in keywords:
        if re.search(r'\b' + re.escape(kw) + r'\b', text):
            return True
    return False


def classify(rec: dict) -> None:
    """
    Tag rec in-place. Adds policy_area, policy_area_labels,
    pregnancy_related, canadian, species.
    """
    text = _text(rec)

    # ---- policy area -------------------------------------------------------
    areas: list[str] = []

    if _has(text, PREGNANCY_MMR_KEYWORDS):
        areas.append("pregnancy_mmr")
    if _has(text, PREGNANCY_INFECTION_KEYWORDS):
        areas.append("pregnancy_infection")
    if _has(text, SCHEDULE_KEYWORDS):
        areas.append("schedule_timing")

    # Anything measles-relevant that doesn't fit a specific policy area is
    # tagged as general_measles.
    if not areas:
        areas.append("general_measles")

    rec["policy_area"] = areas
    rec["policy_area_labels"] = [POLICY_AREA_LABELS.get(a, a) for a in areas]
    rec["pregnancy_related"] = any(
        a in ("pregnancy_mmr", "pregnancy_infection") for a in areas
    )

    # ---- surveillance flag -------------------------------------------------
    # Only for items with no stronger classification (blogs, "other").
    # Fetchers already label agency bulletins (PHAC / CDC / ECDC) correctly.
    if rec.get("source_type") not in _PROTECTED_TYPES:
        if _has(text, SURVEILLANCE_KEYWORDS):
            rec["source_type"] = "surveillance"

    # ---- species -----------------------------------------------------------
    has_animal = _has(text, ANIMAL_KEYWORDS)
    has_human  = _has(text, HUMAN_KEYWORDS)
    if has_human and has_animal:
        rec["species"] = "both"
    elif has_animal:
        rec["species"] = "animal"
    elif has_human:
        rec["species"] = "human"
    else:
        rec["species"] = "na"

    # ---- Canadian flag -----------------------------------------------------
    rec["canadian"] = _has(text, CANADA_KEYWORDS)


POLICY_AREA_LABELS: dict[str, str] = {
    "pregnancy_mmr":       "MMR in pregnancy",
    "pregnancy_infection": "Measles/rubella infection in pregnancy",
    "schedule_timing":     "Second-dose timing",
    "general_measles":     "General measles",
}


def is_relevant(rec: dict) -> bool:
    """
    Must have a measles / MMR / rubella signal.
    Clinical trials are always kept if they passed ClinicalTrials.gov queries.
    Curated NITAG records are always kept.
    WHO IRIS items are kept if they mention measles-adjacent terms.
    """
    if rec.get("curated"):
        return True
    if rec.get("source_type") == "clinical_trial":
        return True   # fetched from targeted queries — always relevant
    text = _text(rec)
    return _has(text, MEASLES_SCOPE_KEYWORDS)


def passes_inclusion(rec: dict) -> bool:
    """
    Inclusion rule: all relevant records are kept.
    (Unlike the Ebola dashboard, we do not filter out animal studies — animal
    immunogenicity data is useful for the schedule timing question.)
    """
    return True
