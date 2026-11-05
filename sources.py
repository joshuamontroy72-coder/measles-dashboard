"""
Source fetchers for the Measles Evidence Watch pipeline.

Each fetcher returns a list of raw record dicts with a common shape:
  title, summary, url, doi, source, source_type, published_date,
  journal, authors, affiliations, extra

source_type values:
  journal_article | preprint | clinical_trial | news | outbreak_report |
  guideline | surveillance | press_release

CHANGES in this version
  * Every feed is downloaded with `requests` (hard timeout) and then parsed.
    feedparser.parse(url) has no timeout and can hang a run for hours.
  * Google News / direct-feed fetchers de-duplicate within a run.
  * New: PubMed (E-utilities) fetcher  -> catches new journal papers quickly.
  * New: broad Europe PMC measles queries (not tied to the narrow policy queries).
  * New: curated NITAG records loaded from nitag_curated.json.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from html import unescape
from pathlib import Path
from typing import Callable

import requests

try:
    import feedparser  # type: ignore
except Exception:
    feedparser = None

from config import (
    LITERATURE_QUERIES,
    LITERATURE_SINCE,
    MAX_PER_QUERY,
    CLINICALTRIALS_QUERIES,
    NEWS_SEARCH_TERMS,
    NEWS_WINDOW_DAYS,
    DIRECT_FEEDS,
    WHO_IRIS_API,
    WHO_IRIS_QUERIES,
    WHO_IRIS_SINCE_DAYS,
    WHO_DON_SEARCHES,
    SOURCE_DOMAIN_MAP,
    PHARMA_HOSTS,
    USER_AGENT,
    NCBI_API_KEY,
    ALTMETRIC_API_KEY,
)

TODAY   = dt.date.today()
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})
TIMEOUT = 30

HERE = Path(__file__).parent


def _log(msg: str) -> None:
    print(f"[sources] {msg}", flush=True)


def _get(url: str, params: dict | None = None, **kw):
    return SESSION.get(url, params=params, timeout=TIMEOUT, **kw)


def _parse_feed(url: str):
    """
    Download a feed with a hard timeout, then parse the text.
    (feedparser.parse(url) uses its own HTTP stack with NO timeout.)
    Returns a feedparser result, or None if the download failed.
    """
    if feedparser is None:
        return None
    r = _get(url, headers={
        "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml, */*",
    })
    r.raise_for_status()
    return feedparser.parse(r.content)


def stable_id(*parts: str) -> str:
    raw = "|".join(p for p in parts if p).lower().strip()
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def host_of(url: str) -> str:
    try:
        netloc = urllib.parse.urlparse(url).netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


def outlet_name(url: str, default: str) -> str:
    return SOURCE_DOMAIN_MAP.get(host_of(url), default)


# ---------------------------------------------------------------------------
# 1. Europe PMC — peer-reviewed journals + preprints
# ---------------------------------------------------------------------------

EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

# Broad "anything about measles" queries, run IN ADDITION to config.LITERATURE_QUERIES.
# The policy queries in config.py are narrow (pregnancy / schedule); papers such as
# outbreak descriptions or elimination-status analyses were never matched by them.
EXTRA_LITERATURE_QUERIES = [
    {
        "label": "Measles — all peer-reviewed (broad)",
        "query": '(TITLE_ABS:"measles" OR TITLE_ABS:"MMR vaccine" OR TITLE_ABS:"measles-mumps-rubella" '
                 'OR TITLE_ABS:"measles virus" OR TITLE_ABS:"measles vaccine") AND (SRC:MED)',
    },
    {
        "label": "Measles — all preprints (broad)",
        "query": '(TITLE_ABS:"measles" OR TITLE_ABS:"MMR vaccine" OR TITLE_ABS:"measles-mumps-rubella") AND (SRC:PPR)',
    },
]

# How far back the broad queries look (days) — keeps the run small.
EXTRA_LITERATURE_DAYS = 365


def fetch_europepmc() -> list[dict]:
    records: list[dict] = []
    seen_ids: set[str] = set()

    broad_since = (TODAY - dt.timedelta(days=EXTRA_LITERATURE_DAYS)).isoformat()
    jobs = [(q, LITERATURE_SINCE) for q in LITERATURE_QUERIES] + \
           [(q, broad_since) for q in EXTRA_LITERATURE_QUERIES]

    for q, since in jobs:
        date_clause = f"(FIRST_PDATE:[{since} TO {TODAY.isoformat()}])"
        query = f"({q['query']}) AND {date_clause}"
        _log(f"Europe PMC: {q['label']}")
        cursor = "*"
        got = 0
        while got < MAX_PER_QUERY:
            params = {
                "query": query, "format": "json", "pageSize": 100,
                "cursorMark": cursor, "resultType": "core",
                "sort": "P_PDATE_D desc",
            }
            try:
                r = _get(EUROPEPMC, params=params)
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                _log(f"  ! Europe PMC error: {e}")
                break

            results = data.get("resultList", {}).get("result", [])
            if not results:
                break

            for it in results:
                epmc_id = f"{it.get('source','')}:{it.get('id','')}"
                if epmc_id in seen_ids:
                    continue
                seen_ids.add(epmc_id)

                doi = (it.get("doi") or "").strip()
                src = it.get("source", "")
                is_preprint = src == "PPR" or "preprint" in " ".join(
                    (it.get("pubTypeList", {}) or {}).get("pubType", [])
                ).lower()

                if doi:
                    url = f"https://doi.org/{doi}"
                elif it.get("pmid"):
                    url = f"https://pubmed.ncbi.nlm.nih.gov/{it['pmid']}/"
                else:
                    url = f"https://europepmc.org/article/{src}/{it.get('id','')}"

                journal = (
                    (it.get("journalInfo", {}) or {}).get("journal", {}).get("title", "")
                    or it.get("journalTitle", "")
                    or ("Preprint" if is_preprint else "")
                )
                if is_preprint and not journal:
                    journal = it.get("publisher", "") or "Preprint"

                records.append({
                    "title": (it.get("title") or "").strip().rstrip("."),
                    "summary": (it.get("abstractText") or "").strip(),
                    "url": url,
                    "doi": doi,
                    "source": "Europe PMC",
                    "source_type": "preprint" if is_preprint else "journal_article",
                    "published_date": _first_date(it),
                    "journal": journal,
                    "authors": it.get("authorString", ""),
                    "affiliations": _epmc_affiliations(it),
                    "extra": {
                        "pmid": it.get("pmid", ""),
                        "cited_by": it.get("citedByCount", 0),
                        "open_access": it.get("isOpenAccess", "N") == "Y",
                        "preprint_server": journal if is_preprint else "",
                    },
                })
                got += 1

            cursor = data.get("nextCursorMark", "")
            if not cursor or cursor == params["cursorMark"]:
                break
            time.sleep(0.34)

    _log(f"Europe PMC total: {len(records)}")
    return records


def _first_date(it: dict) -> str:
    for k in ("firstPublicationDate", "electronicPublicationDate", "pubDate"):
        v = it.get(k)
        if v:
            return v[:10]
    y = it.get("pubYear")
    return f"{y}-01-01" if y else ""


def _epmc_affiliations(it: dict) -> str:
    affs = []
    for a in (it.get("authorList", {}) or {}).get("author", []) or []:
        for aff in (a.get("authorAffiliationDetailsList", {}) or {}).get(
            "authorAffiliation", []
        ) or []:
            if aff.get("affiliation"):
                affs.append(aff["affiliation"])
    if not affs and it.get("affiliation"):
        affs.append(it["affiliation"])
    return " | ".join(affs[:8])


# ---------------------------------------------------------------------------
# 1b. PubMed (NCBI E-utilities) — newly indexed measles papers
#     Europe PMC can lag PubMed by days; this catches papers as soon as they
#     are indexed (e.g. a BMJ paper published this week).
# ---------------------------------------------------------------------------

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
PUBMED_TERM = (
    '(measles[tiab] OR "measles virus"[tiab] OR "measles vaccine"[tiab] OR '
    '"MMR vaccine"[tiab] OR "measles-mumps-rubella"[tiab] OR '
    '"Measles"[MeSH Terms] OR "Measles Vaccine"[MeSH Terms])'
)
PUBMED_RELDATE_DAYS = 120
PUBMED_MAX = 400

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _eutils_params(extra: dict) -> dict:
    p = dict(extra)
    p["tool"] = "measles-evidence-watch"
    if NCBI_API_KEY:
        p["api_key"] = NCBI_API_KEY
    return p


def fetch_pubmed() -> list[dict]:
    records: list[dict] = []
    try:
        r = _get(f"{EUTILS}/esearch.fcgi", params=_eutils_params({
            "db": "pubmed", "term": PUBMED_TERM, "retmode": "json",
            "retmax": PUBMED_MAX, "sort": "pub_date",
            "datetype": "edat", "reldate": PUBMED_RELDATE_DAYS,
        }))
        r.raise_for_status()
        ids = r.json().get("esearchresult", {}).get("idlist", [])
    except Exception as e:
        _log(f"  ! PubMed esearch error: {e}")
        return records

    _log(f"PubMed: {len(ids)} ids in last {PUBMED_RELDATE_DAYS} days")
    for i in range(0, len(ids), 100):
        batch = ids[i:i + 100]
        try:
            r = _get(f"{EUTILS}/efetch.fcgi", params=_eutils_params({
                "db": "pubmed", "id": ",".join(batch), "retmode": "xml",
            }))
            r.raise_for_status()
            records.extend(_parse_pubmed_xml(r.content))
        except Exception as e:
            _log(f"  ! PubMed efetch error: {e}")
        time.sleep(0.34 if not NCBI_API_KEY else 0.12)

    _log(f"PubMed total: {len(records)}")
    return records


def _parse_pubmed_xml(xml_bytes: bytes) -> list[dict]:
    out: list[dict] = []
    root = ET.fromstring(xml_bytes)
    for art in root.iter("PubmedArticle"):
        pmid = (art.findtext(".//MedlineCitation/PMID") or "").strip()
        title_el = art.find(".//Article/ArticleTitle")
        title = "".join(title_el.itertext()).strip().rstrip(".") if title_el is not None else ""
        abstract = " ".join(
            "".join(a.itertext()).strip()
            for a in art.findall(".//Article/Abstract/AbstractText")
        ).strip()
        journal = (art.findtext(".//Article/Journal/Title") or "").strip()

        doi = ""
        for aid in art.findall(".//PubmedData/ArticleIdList/ArticleId"):
            if aid.get("IdType") == "doi" and aid.text:
                doi = aid.text.strip()
                break
        if not doi:
            for el in art.findall(".//Article/ELocationID"):
                if el.get("EIdType") == "doi" and el.text:
                    doi = el.text.strip()
                    break

        # Publication date: electronic date first, else journal issue date
        pub = ""
        ad = art.find(".//Article/ArticleDate")
        if ad is not None:
            pub = _ymd(ad.findtext("Year"), ad.findtext("Month"), ad.findtext("Day"))
        if not pub:
            pd_ = art.find(".//Article/Journal/JournalIssue/PubDate")
            if pd_ is not None:
                pub = _ymd(pd_.findtext("Year"), pd_.findtext("Month"), pd_.findtext("Day"))

        authors, affs = [], []
        for a in art.findall(".//Article/AuthorList/Author"):
            ln, ini = a.findtext("LastName"), a.findtext("Initials")
            if ln:
                authors.append(f"{ln} {ini or ''}".strip())
            for af in a.findall("AffiliationInfo/Affiliation"):
                if af.text and af.text not in affs:
                    affs.append(af.text.strip())
        author_str = ", ".join(authors[:6]) + (" et al." if len(authors) > 6 else "")

        if not title or not pmid:
            continue
        out.append({
            "title": title,
            "summary": abstract,
            "url": f"https://doi.org/{doi}" if doi else f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            "doi": doi,
            "source": "PubMed",
            "source_type": "journal_article",
            "published_date": pub or TODAY.isoformat(),
            "journal": journal,
            "authors": author_str,
            "affiliations": " | ".join(affs[:8]),
            "extra": {"pmid": pmid},
        })
    return out


def _ymd(y, m, d) -> str:
    if not y:
        return ""
    try:
        mm = int(m) if (m and m.isdigit()) else _MONTHS.get((m or "jan")[:3].lower(), 1)
        dd = int(d) if (d and d.isdigit()) else 1
        return dt.date(int(y), mm, dd).isoformat()
    except Exception:
        return f"{y}-01-01"


# ---------------------------------------------------------------------------
# 2. ClinicalTrials.gov API v2
# ---------------------------------------------------------------------------

CTGOV = "https://clinicaltrials.gov/api/v2/studies"


def fetch_clinicaltrials() -> list[dict]:
    records: list[dict] = []
    seen: set[str] = set()

    for q in CLINICALTRIALS_QUERIES:
        _log(f"ClinicalTrials.gov: {q['label']}")
        page_token = None
        got = 0
        while got < MAX_PER_QUERY:
            params = {
                "query.cond": q["cond"],
                "query.intr": q["intr"],
                "pageSize": 100,
                "format": "json",
            }
            if page_token:
                params["pageToken"] = page_token
            try:
                r = _get(CTGOV, params=params)
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                _log(f"  ! ClinicalTrials error: {e}")
                break

            studies = data.get("studies", [])
            if not studies:
                break

            for s in studies:
                ps   = s.get("protocolSection", {})
                idm  = ps.get("identificationModule", {})
                nct  = idm.get("nctId", "")
                if not nct or nct in seen:
                    continue
                seen.add(nct)

                status_m  = ps.get("statusModule", {})
                design_m  = ps.get("designModule", {})
                cond_m    = ps.get("conditionsModule", {})
                arms_m    = ps.get("armsInterventionsModule", {})
                spon_m    = ps.get("sponsorCollaboratorsModule", {})
                desc_m    = ps.get("descriptionModule", {})
                loc_m     = ps.get("contactsLocationsModule", {})

                interventions = [
                    i.get("name", "") for i in arms_m.get("interventions", []) or []
                ]
                countries = sorted({
                    loc.get("country", "")
                    for loc in loc_m.get("locations", []) or []
                    if loc.get("country")
                })
                phases  = design_m.get("phases", []) or []
                sponsor = spon_m.get("leadSponsor", {}).get("name", "")
                last_update = (
                    status_m.get("lastUpdatePostDateStruct", {}).get("date", "")
                    or status_m.get("startDateStruct", {}).get("date", "")
                )

                records.append({
                    "title": idm.get("briefTitle", "") or idm.get("officialTitle", ""),
                    "summary": desc_m.get("briefSummary", "")[:1200],
                    "url": f"https://clinicaltrials.gov/study/{nct}",
                    "doi": "",
                    "source": "ClinicalTrials.gov",
                    "source_type": "clinical_trial",
                    "published_date": _norm_date(last_update),
                    "journal": "",
                    "authors": sponsor,
                    "affiliations": sponsor + " | " + ", ".join(countries),
                    "intervention_raw": " ".join(interventions),
                    "extra": {
                        "nct_id": nct,
                        "status": status_m.get("overallStatus", ""),
                        "phase": phases,
                        "conditions": ", ".join(cond_m.get("conditions", []) or []),
                        "interventions": ", ".join(interventions[:6]),
                        "countries": ", ".join(countries[:8]),
                    },
                })
                got += 1

            page_token = data.get("nextPageToken")
            if not page_token:
                break
            time.sleep(0.5)

    _log(f"ClinicalTrials.gov total: {len(records)}")
    return records


def _norm_date(d: str) -> str:
    if not d:
        return ""
    parts = d.split("-")
    if len(parts) == 2:
        return f"{d}-01"
    return d[:10]


# ---------------------------------------------------------------------------
# 3. WHO Disease Outbreak News — targeted search (legacy RSS is retired)
# ---------------------------------------------------------------------------

def fetch_measles_outbreak_news() -> list[dict]:
    """
    WHO Disease Outbreak News and PHAC surveillance alerts — measles-focused.
    Uses Google News RSS searches targeting WHO DON pages and outbreak sources.
    Items linking to who.int DON pages are classified as outbreak_report.
    """
    records: list[dict] = []
    if feedparser is None:
        _log("feedparser not installed; skipping measles outbreak news")
        return records

    cutoff = TODAY - dt.timedelta(days=NEWS_WINDOW_DAYS)
    seen_urls: set[str] = set()

    for term in WHO_DON_SEARCHES:
        q = urllib.parse.quote(term)
        url = f"https://news.google.com/rss/search?q={q}&hl=en&gl=US&ceid=US:en"
        _log(f"Measles outbreak search: {term[:70]}")
        try:
            parsed = _parse_feed(url)
        except Exception as e:
            _log(f"  ! outbreak search error: {e}")
            continue
        if parsed is None:
            continue

        for entry in parsed.entries[:40]:
            link = entry.get("link", "")
            if link in seen_urls:
                continue
            pub = _entry_date(entry)
            if pub and _to_date(pub) and _to_date(pub) < cutoff:
                continue
            title = entry.get("title", "").strip()
            if " - " in title:
                title_main, _ = title.rsplit(" - ", 1)
                title = title_main.strip()

            u = link.lower()
            if "who.int" in u and ("disease-outbreak-news" in u or "/don/" in u):
                stype = "outbreak_report"
                src   = "WHO Disease Outbreak News"
            elif "promedmail.org" in u or "promed" in u:
                stype = "outbreak_report"
                src   = "ProMED"
            elif "who.int" in u:
                stype = "news"
                src   = "WHO"
            elif "ecdc.europa.eu" in u:
                stype = "surveillance"
                src   = "ECDC"
            elif "cdc.gov" in u:
                stype = "surveillance"
                src   = "CDC"
            elif "phac-aspc.gc.ca" in u or "canada.ca" in u:
                stype = "surveillance"
                src   = "PHAC"
            else:
                stype = "news"
                src   = outlet_name(link, "News")

            seen_urls.add(link)
            records.append({
                "title": title,
                "summary": _clean_html(
                    entry.get("summary", "") or entry.get("description", "")
                )[:600],
                "url": link,
                "doi": "",
                "source": src,
                "source_type": stype,
                "published_date": pub,
                "journal": "",
                "authors": "",
                "affiliations": "",
                "extra": {"feed": "measles outbreak search"},
            })

    _log(f"Measles outbreak news total: {len(records)}")
    return records


# ---------------------------------------------------------------------------
# 4. Direct RSS / Atom feeds (CIDRAP, ProMED, CDC MMWR, ECDC, preprints …)
# ---------------------------------------------------------------------------

def fetch_direct_feeds() -> list[dict]:
    records: list[dict] = []
    if feedparser is None:
        _log("feedparser not installed; skipping direct feeds")
        return records

    cutoff = TODAY - dt.timedelta(days=NEWS_WINDOW_DAYS)
    seen_urls: set[str] = set()

    for feed in DIRECT_FEEDS:
        _log(f"Feed: {feed['source']}")
        try:
            parsed = _parse_feed(feed["url"])
        except Exception as e:
            _log(f"  ! feed error: {e}")
            continue
        if parsed is None:
            continue

        for entry in parsed.entries[:80]:
            link = entry.get("link", "")
            if link and link in seen_urls:
                continue
            pub  = _entry_date(entry)
            if pub and _to_date(pub) and _to_date(pub) < cutoff:
                continue
            title   = entry.get("title", "").strip()
            summary = _clean_html(entry.get("summary", "") or entry.get("description", ""))
            src_name = outlet_name(link, feed["source"])
            stype = feed["type"]
            if host_of(link) in PHARMA_HOSTS:
                stype = "press_release"
            seen_urls.add(link)
            records.append({
                "title": title,
                "summary": summary[:800],
                "url": link,
                "doi": "",
                "source": src_name,
                "source_type": stype,
                "published_date": pub,
                "journal": "",
                "authors": "",
                "affiliations": "",
                "extra": {"feed": feed["source"]},
            })

    _log(f"Direct feeds total: {len(records)}")
    return records


# ---------------------------------------------------------------------------
# 5. Google News RSS searches
# ---------------------------------------------------------------------------

GNEWS = "https://news.google.com/rss/search"


def fetch_google_news() -> list[dict]:
    records: list[dict] = []
    if feedparser is None:
        _log("feedparser not installed; skipping Google News")
        return records

    cutoff = TODAY - dt.timedelta(days=NEWS_WINDOW_DAYS)
    seen_urls: set[str] = set()   # same story is returned by several search terms

    for term in NEWS_SEARCH_TERMS:
        q   = urllib.parse.quote(f'{term} when:{NEWS_WINDOW_DAYS}d')
        url = f"{GNEWS}?q={q}&hl=en-CA&gl=CA&ceid=CA:en"
        _log(f"Google News: {term}")
        try:
            parsed = _parse_feed(url)
        except Exception as e:
            _log(f"  ! news error: {e}")
            continue
        if parsed is None:
            continue

        for entry in parsed.entries[:40]:
            link = entry.get("link", "")
            if link and link in seen_urls:
                continue
            pub  = _entry_date(entry)
            if pub and _to_date(pub) and _to_date(pub) < cutoff:
                continue
            title = entry.get("title", "").strip()
            outlet = ""
            if " - " in title:
                title_main, outlet = title.rsplit(" - ", 1)
                title = title_main.strip()
            src_name = outlet or outlet_name(link, "News")
            host  = host_of(link)
            stype = "press_release" if host in PHARMA_HOSTS else "news"
            # PHAC/ECDC/CDC surveillance bulletins showing up in news
            if "phac-aspc.gc.ca" in host or "canada.ca" in host:
                stype = "surveillance"
                src_name = "PHAC"
            elif "ecdc.europa.eu" in host:
                stype = "surveillance"
                src_name = "ECDC"
            seen_urls.add(link)
            records.append({
                "title": title,
                "summary": _clean_html(entry.get("summary", ""))[:500],
                "url": link,
                "doi": "",
                "source": src_name.strip(),
                "source_type": stype,
                "published_date": pub,
                "journal": "",
                "authors": "",
                "affiliations": "",
                "extra": {"search_term": term},
            })

    _log(f"Google News total: {len(records)}")
    return records


# ---------------------------------------------------------------------------
# 6. WHO IRIS — WHO publications / guidance repository (DSpace REST)
# ---------------------------------------------------------------------------

def fetch_who_iris() -> list[dict]:
    records: list[dict] = []
    seen:    set[str]   = set()
    cutoff = (TODAY - dt.timedelta(days=WHO_IRIS_SINCE_DAYS)).isoformat()

    for q in WHO_IRIS_QUERIES:
        _log(f"WHO IRIS: {q}")
        got = []
        for strategy in (_iris_via_rest, _iris_via_opensearch):
            try:
                got = strategy(q)
            except Exception as e:
                _log(f"  ! {strategy.__name__} failed: {e}")
                got = []
            if got:
                break
        for rec in got:
            uri = rec["url"]
            if uri in seen:
                continue
            if rec["published_date"] and rec["published_date"] < cutoff:
                continue
            seen.add(uri)
            records.append(rec)

    _log(f"WHO IRIS total: {len(records)}")
    return records


def _iris_via_rest(query: str) -> list[dict]:
    out: list[dict] = []
    params = {
        "query": query,
        "scope": "/",
        "embed": "thumbnail",
        "page": 0,
        "size": 30,
        "sort": "dc.date.issued,DESC",
        "configuration": "defaultConfiguration",
    }
    r = _get(WHO_IRIS_API, params=params)
    r.raise_for_status()
    data   = r.json()
    items  = (data.get("_embedded", {}).get("searchResult", {})
                  .get("_embedded", {}).get("objects", []))
    for obj in items:
        emb = obj.get("_embedded", {}).get("indexableObject", obj)
        mdata = {m["key"]: m.get("values", [{}])[0].get("value", "")
                 for m in emb.get("metadata", [])
                 if m.get("values")}
        title  = mdata.get("dc.title", "") or mdata.get("dcterms.title", "")
        date   = mdata.get("dc.date.issued", "")[:10]
        doi    = mdata.get("dc.identifier.doi", "")
        handle = mdata.get("dc.identifier.uri", "")
        url    = f"https://doi.org/{doi}" if doi else handle
        if not url or not title:
            continue
        out.append({
            "title": title.strip(),
            "summary": (mdata.get("dc.description.abstract", "")
                        or mdata.get("dcterms.abstract", ""))[:800],
            "url": url,
            "doi": doi,
            "source": "WHO IRIS",
            "source_type": "guideline",
            "published_date": date,
            "journal": "WHO",
            "authors": mdata.get("dc.contributor.author", ""),
            "affiliations": "World Health Organization",
            "extra": {"handle": handle},
        })
    return out


def _iris_via_opensearch(query: str) -> list[dict]:
    url = "https://iris.who.int/discover"
    params = {"query": query, "rpp": 20, "sort_by": "dc.date.issued_dt",
              "order": "DESC", "format": "atom"}
    r = _get(url, params=params)
    r.raise_for_status()
    parsed = feedparser.parse(r.content)
    out = []
    for entry in parsed.entries[:20]:
        link = entry.get("link", "")
        out.append({
            "title": entry.get("title", "").strip(),
            "summary": _clean_html(entry.get("summary", ""))[:800],
            "url": link,
            "doi": "",
            "source": "WHO IRIS",
            "source_type": "guideline",
            "published_date": _entry_date(entry),
            "journal": "WHO",
            "authors": ", ".join(a.get("name", "") for a in entry.get("authors", [])),
            "affiliations": "World Health Organization",
            "extra": {},
        })
    return out


# ---------------------------------------------------------------------------
# 6b. Curated NITAG guidance (nitag_curated.json)
#     NITAG statements (NACI, ACIP, JCVI, ATAGI, STIKO …) are published a few
#     times a year as web pages / PDFs, not in RSS feeds, so they cannot be
#     discovered reliably by search.  Add each one to nitag_curated.json and
#     it will appear in the NITAG channel permanently.
# ---------------------------------------------------------------------------

def fetch_nitag_curated() -> list[dict]:
    path = HERE / "nitag_curated.json"
    if not path.exists():
        _log("nitag_curated.json not found; skipping curated NITAG records")
        return []
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        _log(f"  ! could not read nitag_curated.json: {e}")
        return []

    out = []
    for it in items:
        if not it.get("title") or not it.get("url"):
            continue
        rec = {
            "doi": "", "journal": "", "authors": "", "affiliations": "",
            "summary": "", "source": "NITAG", "source_type": "guideline",
            "extra": {},
        }
        rec.update(it)
        rec["curated"] = True
        rec["channel"] = "nitag"
        out.append(rec)
    _log(f"Curated NITAG records: {len(out)}")
    return out


# ---------------------------------------------------------------------------
# 7. Altmetric enrichment
# ---------------------------------------------------------------------------

ALTMETRIC = "https://api.altmetric.com/v1/doi/"


def enrich_altmetric(records: list[dict], max_lookups: int = 400) -> None:
    done = 0
    for rec in records:
        doi = rec.get("doi")
        if not doi or done >= max_lookups:
            continue
        try:
            url    = ALTMETRIC + urllib.parse.quote(doi)
            params = {"key": ALTMETRIC_API_KEY} if ALTMETRIC_API_KEY else None
            r      = _get(url, params=params)
            done  += 1
            if r.status_code == 200:
                d = r.json()
                rec["altmetric_score"]    = round(d.get("score", 0), 1)
                rec["altmetric_url"]      = d.get("details_url", "")
                rec["altmetric_reads"]    = d.get("readers_count", 0)
                rec["altmetric_mentions"] = d.get("cited_by_posts_count", 0)
        except Exception:
            pass
        time.sleep(0.2)
    _log(f"Altmetric lookups performed: {done}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _entry_date(entry) -> str:
    for k in ("published_parsed", "updated_parsed"):
        t = entry.get(k)
        if t:
            try:
                return dt.date(t.tm_year, t.tm_mon, t.tm_mday).isoformat()
            except Exception:
                pass
    return ""


def _to_date(s: str):
    try:
        return dt.date.fromisoformat(s[:10])
    except Exception:
        return None


ALL_FETCHERS: list[tuple[str, Callable[[], list[dict]]]] = [
    ("Curated NITAG guidance",                      fetch_nitag_curated),
    ("PubMed (new measles papers)",                 fetch_pubmed),
    ("Europe PMC (journals + preprints)",          fetch_europepmc),
    ("ClinicalTrials.gov",                          fetch_clinicaltrials),
    ("WHO IRIS (guidance / SAGE reports)",          fetch_who_iris),
    ("Measles outbreak news (WHO DON / targeted)",  fetch_measles_outbreak_news),
    ("Direct feeds (CIDRAP/ProMED/MMWR/ECDC/…)",   fetch_direct_feeds),
    ("Google News (CBC/STAT/Reuters/PHAC/…)",       fetch_google_news),
]
