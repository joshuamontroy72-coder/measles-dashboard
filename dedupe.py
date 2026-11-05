"""
Measles Evidence Watch — record identity + de-duplication helpers.

Why this exists
---------------
* The fetchers never assigned a stable `id`, so update.py could not tell a
  record it already had from a new one.  `record_id()` fixes that.
* The same news story was returned once per Google News search term and once
  per syndicating outlet.  `dedupe()` collapses those into a single card and
  keeps a count of how many outlets ran it.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

# ---------------------------------------------------------------------------
# Canonical URL / identity
# ---------------------------------------------------------------------------

_TRACKING_PARAMS = {
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ocid", "cmpid",
    "taid", "oc", "guccounter", "source", "cid",
}


def canonical_url(url: str) -> str:
    """Lower-case host, drop www., tracking params, fragments, trailing slash."""
    if not url:
        return ""
    try:
        p = urlparse(url.strip())
        host = (p.netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        q = [
            (k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
            if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
        ]
        path = p.path.rstrip("/") or "/"
        return urlunparse(("https", host, path, "", urlencode(q), ""))
    except Exception:
        return url.strip().lower()


def _norm_title(title: str) -> str:
    t = (title or "").lower()
    t = re.sub(r"[‘’“”]", "'", t)
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def record_id(rec: dict) -> str:
    """Stable id: DOI > canonical URL > normalised title."""
    doi = (rec.get("doi") or "").strip().lower()
    if doi:
        return "doi:" + doi
    url = canonical_url(rec.get("url", ""))
    if url:
        return "url:" + hashlib.sha1(url.encode()).hexdigest()[:16]
    return "t:" + hashlib.sha1(_norm_title(rec.get("title", "")).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Headline similarity
# ---------------------------------------------------------------------------

_STOP = set("""
a an the of in on at to for from by with and or as is are was were be been it its
this that these those after before over under into about amid says say said new
how why what when who will would could can may might more most than not no
""".split())

# Words that appear in nearly every measles headline carry no signal about
# *which* story it is.  Removing them stops "Measles outbreak in Ontario" and
# "Measles outbreak in Alberta" from looking alike.
_DOMAIN_STOP = set("""
measles outbreak outbreaks vaccine vaccines vaccination vaccinated vaccinations
case cases health public mmr virus cdc phac who report reports reported
""".split())


def _tokens(title: str) -> frozenset[str]:
    toks = [
        w for w in _norm_title(title).split()
        if (len(w) > 2 or w.isdigit()) and w not in _STOP and w not in _DOMAIN_STOP
    ]
    return frozenset(toks)


def _similar(a: frozenset[str], b: frozenset[str],
             jaccard: float = 0.6, min_shared: int = 3) -> bool:
    if not a or not b:
        return False
    if a == b:
        # Identical distinctive words (wire copies, re-ordered headlines).
        # Needs >=2 words so a one-word headline can't match everything.
        return len(a) >= 2
    inter = len(a & b)
    if inter < min_shared:
        return False
    union = len(a | b)
    small = min(len(a), len(b))
    return (inter / union) >= jaccard or (inter / small) >= 0.85


def _to_date(s: str):
    try:
        return dt.date.fromisoformat((s or "")[:10])
    except Exception:
        return None


# Outlets we would rather show as the "main" card when a story is syndicated.
_PREFERRED_OUTLETS = (
    "cidrap", "cbc", "reuters", "stat", "the globe and mail", "ctv",
    "canadian press", "associated press", "ap news", "bbc", "who",
    "phac", "public health", "cdc", "ecdc", "statnews",
)


def _outlet_rank(rec: dict) -> int:
    src = (rec.get("source") or rec.get("journal") or "").lower()
    for i, name in enumerate(_PREFERRED_OUTLETS):
        if name in src:
            return i
    return len(_PREFERRED_OUTLETS)


# ---------------------------------------------------------------------------
# De-duplication
# ---------------------------------------------------------------------------

_CLUSTERABLE = {"news", "press_release"}


def dedupe(records: list[dict], window_days: int = 10) -> list[dict]:
    """
    1. Exact duplicates (same id)                    -> keep one.
    2. Preprint whose title matches a journal paper   -> drop the preprint.
    3. News / press near-duplicates (similar headline within `window_days`)
       -> one card, with `duplicate_count` and `also_reported_by` attached.
    Records flagged `curated` or `reviewed` are never dropped.
    """
    # ---- 1. exact id ------------------------------------------------------
    by_id: dict[str, dict] = {}
    for r in records:
        rid = r.get("id") or record_id(r)
        r["id"] = rid
        cur = by_id.get(rid)
        if cur is None or _better(r, cur):
            by_id[rid] = r
    recs = list(by_id.values())

    # ---- 2. preprint superseded by journal version ------------------------
    journal_titles = {
        _norm_title(r.get("title", ""))
        for r in recs if r.get("source_type") == "journal_article"
    }
    kept: list[dict] = []
    for r in recs:
        if (r.get("source_type") == "preprint"
                and _norm_title(r.get("title", "")) in journal_titles
                and not (r.get("curated") or r.get("reviewed"))):
            continue
        kept.append(r)
    recs = kept

    # ---- 3. headline clusters among news / press --------------------------
    news = [r for r in recs if r.get("source_type") in _CLUSTERABLE
            and not (r.get("curated") or r.get("reviewed"))]
    news_ids = {id(r) for r in news}
    others = [r for r in recs if id(r) not in news_ids]

    news.sort(key=lambda r: (_to_date(r.get("date") or r.get("published_date")) or dt.date.min),
              reverse=True)
    toks = [_tokens(r.get("title", "")) for r in news]

    parent = list(range(len(news)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(news)):
        di = _to_date(news[i].get("date") or news[i].get("published_date"))
        for j in range(i + 1, len(news)):
            dj = _to_date(news[j].get("date") or news[j].get("published_date"))
            if di and dj and abs((di - dj).days) > window_days:
                # list is date-sorted, so everything further on is older still
                break
            if _similar(toks[i], toks[j]):
                parent[find(j)] = find(i)

    clusters: dict[int, list[dict]] = {}
    for i, r in enumerate(news):
        clusters.setdefault(find(i), []).append(r)

    deduped_news: list[dict] = []
    for group in clusters.values():
        group.sort(key=lambda r: (_outlet_rank(r), -len(r.get("summary") or "")))
        main = group[0]
        others_in_group = group[1:]
        if others_in_group:
            outlets = []
            for g in others_in_group:
                name = (g.get("source") or "").strip()
                if name and name != main.get("source") and name not in outlets:
                    outlets.append(name)
            main["duplicate_count"] = len(others_in_group)
            main["also_reported_by"] = outlets[:6]
        deduped_news.append(main)

    return others + deduped_news


def _better(a: dict, b: dict) -> bool:
    """True if `a` should replace `b` when both share an id."""
    if a.get("curated") and not b.get("curated"):
        return True
    if b.get("curated") and not a.get("curated"):
        return False
    return len(a.get("summary") or "") > len(b.get("summary") or "")
