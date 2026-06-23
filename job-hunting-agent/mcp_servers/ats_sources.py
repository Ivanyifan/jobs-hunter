import datetime
import hashlib
import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote, urljoin, urlparse

import requests


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
}

LANG_SEGMENT_RE = re.compile(r"^[a-z]{2}(?:-[A-Z]{2})?$")

ENTRY_QUERY_MARKERS = (
    "entry", "entry-level", "entry level", "junior", "jr.", "new grad",
    "associate", "early career", "university", "graduate",
)

ENTRY_SEARCH_PREFIXES = (
    "entry level",
    "junior",
    "new grad",
    "associate",
    "early career",
)

ENTRY_NEGATIVE_TITLE_TERMS = (
    "senior", "sr.", "staff", "principal", "lead", "manager", "director",
    "head", "architect", "executive", "account executive", "sales",
    "product owner", "portfolio", "research associate", "test engineer",
    "quality engineer", "qa engineer",
)

ENTRY_TECH_TITLE_TERMS = (
    "software", "developer", "frontend", "front end", "backend",
    "back end", "full stack", "data engineer", "application engineer",
    "systems engineer", "machine learning engineer", "ml engineer",
    "programmer",
)

ENTRY_POSITIVE_TITLE_TERMS = (
    "entry", "entry-level", "entry level", "junior", "jr.", "associate",
    "new grad", "university", "graduate", "engineer i", "software engineer i",
    "developer i", "level 1", "early career",
)

NON_US_LOCATION_TERMS = (
    "singapore", "india", "canada", "united kingdom", "uk", "ireland",
    "australia", "germany", "france", "spain", "poland", "mexico",
    "brazil", "china", "japan", "korea", "netherlands", "philippines",
)

WORKDAY_US_COUNTRY_ID = "bc33aa3152ec42d4995f4791a106ed09"

WORKDAY_NON_US_URL_SEGMENTS = (
    "/en-au/", "/en-gb/", "/en-ca/", "/en-in/", "/en-ie/", "/en-sg/",
    "/en-nz/", "/en-ph/", "/en-de/", "/en-fr/", "/en-mx/", "/en-br/",
    "_australia", "_canada", "_europe", "_uk", "_india", "_singapore",
    "australia", "canada", "india", "singapore", "united-kingdom",
    "germany", "poland", "mexico", "brazil",
)


def now_iso() -> str:
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def strip_html(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    value = repair_mojibake(value)
    return value


def repair_mojibake(value: str) -> str:
    if not isinstance(value, str) or not any(marker in value for marker in ("â", "Ã", "Â")):
        return value
    try:
        repaired = value.encode("latin1").decode("utf-8")
    except Exception:
        return value
    old_badness = sum(value.count(marker) for marker in ("â", "Ã", "Â"))
    new_badness = sum(repaired.count(marker) for marker in ("â", "Ã", "Â"))
    return repaired if new_badness < old_badness else value


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def keywords_text(keywords: List[str]) -> str:
    return normalize_space(" ".join(str(term or "") for term in keywords))


def is_entry_query(keywords: List[str]) -> bool:
    text = keywords_text(keywords).lower()
    return any(marker in text for marker in ENTRY_QUERY_MARKERS)


def canonical_role_query(keywords: List[str]) -> str:
    text = keywords_text(keywords).lower()
    text = re.sub(
        r"\b(entry[- ]?level|junior|jr\.?|new grad|associate|early career|university|graduate)\b",
        " ",
        text,
    )
    text = re.sub(r"\b(remote|hybrid|onsite|united states|usa|us)\b", " ", text)
    text = normalize_space(text)
    role_patterns = [
        ("full stack", "full stack engineer"),
        ("frontend", "frontend engineer"),
        ("front end", "frontend engineer"),
        ("backend", "backend engineer"),
        ("back end", "backend engineer"),
        ("data engineer", "data engineer"),
        ("machine learning", "machine learning engineer"),
        ("ml engineer", "machine learning engineer"),
        ("software developer", "software developer"),
        ("developer", "software developer"),
        ("software engineer", "software engineer"),
        ("software", "software engineer"),
    ]
    for needle, role in role_patterns:
        if needle in text:
            return role
    return text or "software engineer"


def workday_search_texts(source: Dict[str, Any], keywords: List[str]) -> List[str]:
    explicit = normalize_space(str(source.get("search_text") or ""))
    if explicit:
        return [explicit]
    query = keywords_text(keywords)
    if not query:
        return [""]
    if not is_entry_query(keywords):
        return [query]
    role = canonical_role_query(keywords)
    variants = [query]
    variants.extend(f"{prefix} {role}" for prefix in ENTRY_SEARCH_PREFIXES)
    if role != "software engineer":
        variants.extend([
            "entry level software engineer",
            "junior software engineer",
            "new grad software engineer",
            "associate software engineer",
        ])
    deduped = []
    for item in variants:
        item = normalize_space(item)
        if item and item not in deduped:
            deduped.append(item)
    return deduped[:4]


def looks_entry_level_tech_job(job: Dict[str, Any]) -> bool:
    title = str(job.get("role") or "").lower()
    body = str(job.get("job_description") or "").lower()
    if any(term in title for term in ENTRY_NEGATIVE_TITLE_TERMS):
        return False
    if re.search(r"\b(engineer|developer|programmer)\s+(ii|iii|iv|2|3|4)\b", title):
        return False
    if not any(term in title for term in ENTRY_TECH_TITLE_TERMS):
        return False
    if any(term in title for term in ENTRY_POSITIVE_TITLE_TERMS):
        return True
    if re.search(r"\b0\s*(?:-|to|\+)\s*3\b|\b0-3\b|\b1\s*(?:-|to|\+)\s*3\b|\b1-3\b", body):
        return True
    return "entry level" in body or "early career" in body or "new grad" in body


def stable_job_id(prefix: str, raw_id: str, company: str, role: str, apply_link: str) -> str:
    raw = raw_id or f"{company}|{role}|{apply_link}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:18]
    return f"{prefix}-{digest}"


def source_company(source: Dict[str, Any], fallback: str) -> str:
    return str(source.get("company") or fallback or "Unknown").strip() or "Unknown"


def parse_source_urls(raw: str) -> List[Dict[str, Any]]:
    sources = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            company, url = [part.strip() for part in line.split("|", 1)]
            sources.append({"company": company, "url": url})
        else:
            sources.append({"url": line})
    return sources


def infer_source_type(source: Dict[str, Any]) -> str:
    explicit = str(source.get("source_type") or source.get("type") or "").strip().lower()
    if explicit:
        return explicit
    url = str(source.get("url") or "")
    host = (urlparse(url).hostname or "").lower()
    if "greenhouse.io" in host:
        return "greenhouse"
    if "lever.co" in host:
        return "lever"
    if "myworkdayjobs.com" in host or "myworkdaysite.com" in host or "workday" in host:
        return "workday"
    return "schema"


def greenhouse_token_from_url(url: str) -> str:
    parsed = urlparse(url or "")
    parts = [part for part in parsed.path.split("/") if part]
    if "boards-api.greenhouse.io" in (parsed.hostname or ""):
        if "boards" in parts:
            index = parts.index("boards")
            if index + 1 < len(parts):
                return parts[index + 1]
    if "job-boards.greenhouse.io" in (parsed.hostname or "") and parts:
        return parts[0]
    if parts:
        return parts[-1]
    return ""


def fetch_greenhouse(source: Dict[str, Any], timeout: int, limit: int) -> List[Dict[str, Any]]:
    token = str(source.get("board_token") or source.get("token") or "").strip()
    if not token:
        token = greenhouse_token_from_url(str(source.get("url") or ""))
    if not token:
        return []
    api_url = f"https://boards-api.greenhouse.io/v1/boards/{quote(token)}/jobs?content=true"
    response = requests.get(api_url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    company = source_company(source, token)
    jobs = []
    for item in (data.get("jobs") or [])[:limit]:
        location = item.get("location") or {}
        jobs.append({
            "job_id": stable_job_id("greenhouse", str(item.get("id") or ""), company, item.get("title") or "", item.get("absolute_url") or ""),
            "company": company,
            "role": item.get("title") or "Unknown",
            "job_description": strip_html(item.get("content") or ""),
            "location": location.get("name") or "Unknown",
            "apply_link": item.get("absolute_url") or "",
            "source": "ats_greenhouse",
            "indexed_at": now_iso(),
            "metadata": {
                "ats": "greenhouse",
                "board_token": token,
                "ats_job_id": item.get("id"),
                "updated_at": item.get("updated_at"),
                "source_url": api_url,
            },
        })
    return jobs


def lever_token_from_url(url: str) -> str:
    parsed = urlparse(url or "")
    parts = [part for part in parsed.path.split("/") if part]
    if parts:
        return parts[0]
    host = parsed.hostname or ""
    if host == "jobs.lever.co":
        return ""
    return ""


def fetch_lever(source: Dict[str, Any], timeout: int, limit: int) -> List[Dict[str, Any]]:
    token = str(source.get("board_token") or source.get("token") or "").strip()
    if not token:
        token = lever_token_from_url(str(source.get("url") or ""))
    if not token:
        return []
    api_url = f"https://api.lever.co/v0/postings/{quote(token)}?mode=json"
    response = requests.get(api_url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    company = source_company(source, token)
    jobs = []
    for item in (data or [])[:limit]:
        categories = item.get("categories") or {}
        description_parts = [
            item.get("descriptionPlain") or item.get("description") or "",
            " ".join(strip_html(line.get("text") or "") for line in item.get("lists") or [] if isinstance(line, dict)),
        ]
        apply_link = item.get("hostedUrl") or item.get("applyUrl") or ""
        jobs.append({
            "job_id": stable_job_id("lever", str(item.get("id") or ""), company, item.get("text") or "", apply_link),
            "company": company,
            "role": item.get("text") or "Unknown",
            "job_description": strip_html(" ".join(description_parts)),
            "location": categories.get("location") or "Unknown",
            "apply_link": apply_link,
            "source": "ats_lever",
            "indexed_at": now_iso(),
            "metadata": {
                "ats": "lever",
                "board_token": token,
                "ats_job_id": item.get("id"),
                "created_at": item.get("createdAt"),
                "commitment": categories.get("commitment"),
                "team": categories.get("team"),
                "source_url": api_url,
            },
        })
    return jobs


def workday_parts(url: str) -> Optional[Dict[str, str]]:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    tenant = host.split(".")[0]
    parts = [part for part in parsed.path.split("/") if part]
    lang = "en-US"
    prefix = ""
    if "myworkdaysite.com" in host and len(parts) >= 3 and parts[0] == "recruiting":
        tenant = parts[1]
        site = parts[2]
        prefix = f"/recruiting/{tenant}/{site}"
        return {"scheme": parsed.scheme or "https", "host": host, "tenant": tenant, "site": site, "lang": lang, "prefix": prefix}
    if parts and LANG_SEGMENT_RE.match(parts[0]):
        lang = parts.pop(0)
    site = parts[0] if parts else str(parsed.fragment or "").strip()
    if not site:
        return None
    prefix = f"/{lang}/{site}" if lang else f"/{site}"
    return {"scheme": parsed.scheme or "https", "host": host, "tenant": tenant, "site": site, "lang": lang, "prefix": prefix}


def workday_detail_url(parts: Dict[str, str], external_path: str) -> str:
    return f"{parts['scheme']}://{parts['host']}/wday/cxs/{parts['tenant']}/{parts['site']}{external_path}"


def append_location_value(values: List[str], value: Any) -> None:
    if not value:
        return
    if isinstance(value, list):
        for item in value:
            append_location_value(values, item)
        return
    if isinstance(value, dict):
        for key in ("descriptor", "name", "location", "formattedAddress"):
            if value.get(key):
                append_location_value(values, value.get(key))
        address = value.get("address")
        if isinstance(address, dict):
            append_location_value(
                values,
                ", ".join(
                    str(address.get(key) or "")
                    for key in ["addressLocality", "addressRegion", "addressCountry"]
                    if address.get(key)
                ),
            )
        return
    text = normalize_space(repair_mojibake(str(value)))
    if text and text not in values:
        values.append(text)


def workday_detail_info(parts: Dict[str, str], external_path: str, timeout: int) -> Dict[str, Any]:
    if not external_path:
        return {"available": False}
    detail_url = workday_detail_url(parts, external_path)
    try:
        response = requests.get(detail_url, headers=HEADERS, timeout=timeout)
        if response.status_code >= 400:
            return {"available": False, "detail_url": detail_url, "status_code": response.status_code}
        data = response.json()
    except Exception:
        return {"available": False, "detail_url": detail_url}
    info = data.get("jobPostingInfo") or {}
    fields = [
        info.get("jobDescription"),
        info.get("jobDescriptionText"),
        info.get("additionalJobDescription"),
    ]
    locations: List[str] = []
    for key in ("location", "locations", "additionalLocations", "jobRequisitionLocation"):
        append_location_value(locations, info.get(key))
    country = info.get("country") or {}
    country_descriptor = ""
    country_id = ""
    if isinstance(country, dict):
        country_descriptor = repair_mojibake(str(country.get("descriptor") or ""))
        country_id = str(country.get("id") or "")
    elif country:
        country_descriptor = repair_mojibake(str(country))
    return {
        "available": True,
        "detail_url": detail_url,
        "job_description": strip_html(" ".join(str(field or "") for field in fields)),
        "title": repair_mojibake(str(info.get("title") or "")),
        "locations": locations,
        "location_text": "; ".join(locations),
        "country_descriptor": country_descriptor,
        "country_id": country_id,
        "posted_on": info.get("postedOn"),
        "job_req_id": info.get("jobReqId"),
        "job_posting_id": info.get("jobPostingId"),
        "external_url": info.get("externalUrl"),
    }


def workday_url_allows_location(apply_link: str, target_location: str) -> bool:
    loc = (target_location or "").strip().lower()
    if loc not in {"united states", "usa", "us"}:
        return True
    lower_url = (apply_link or "").lower()
    return not any(segment in lower_url for segment in WORKDAY_NON_US_URL_SEGMENTS)


def workday_detail_allows_location(detail: Dict[str, Any], target_location: str) -> bool:
    loc = (target_location or "").strip().lower()
    if not loc or loc == "anywhere":
        return True
    locations = [str(item or "").lower() for item in detail.get("locations") or []]
    country_descriptor = str(detail.get("country_descriptor") or "").lower()
    country_id = str(detail.get("country_id") or "")
    surface = " ".join(locations + [country_descriptor, str(detail.get("detail_url") or "").lower()])
    if loc in {"united states", "usa", "us"}:
        if country_id and country_id != WORKDAY_US_COUNTRY_ID:
            return False
        if country_descriptor and "united states" not in country_descriptor and "usa" not in country_descriptor:
            return False
        return not any(term in surface for term in NON_US_LOCATION_TERMS)
    return loc in surface


def build_workday_job(
    source: Dict[str, Any],
    parts: Dict[str, str],
    company: str,
    item: Dict[str, Any],
    search_text: str,
    api_url: str,
    timeout: int,
    target_location: str,
) -> Optional[Dict[str, Any]]:
    title = repair_mojibake(item.get("title") or item.get("jobTitle") or "Unknown")
    external_path = item.get("externalPath") or item.get("externalUrl") or ""
    apply_link = f"{parts['scheme']}://{parts['host']}{parts.get('prefix', '')}{external_path}" if external_path.startswith("/") else urljoin(str(source.get("url") or ""), external_path)
    if not workday_url_allows_location(apply_link, target_location):
        return None
    if source.get("entry_title_prefilter") and not looks_entry_level_tech_job({
        "role": title,
        "job_description": "",
    }):
        return None
    detail = workday_detail_info(parts, external_path, timeout)
    if detail.get("available"):
        if detail.get("title"):
            title = detail["title"]
        if not workday_detail_allows_location(detail, target_location):
            return None
    jd_text = detail.get("job_description") or ""
    if not jd_text:
        jd_text = strip_html(" ".join(str(item.get(key) or "") for key in ["title", "locationsText", "postedOn", "bulletFields"]))
    detail_locations = detail.get("locations") or []
    location_value = detail.get("location_text") or item.get("locationsText") or item.get("location") or "Unknown"
    return {
        "job_id": stable_job_id("workday", str(item.get("bulletFields") or item.get("jobReqId") or detail.get("job_req_id") or external_path), company, title, apply_link),
        "company": company,
        "role": title,
        "job_description": jd_text,
        "location": location_value,
        "apply_link": apply_link,
        "source": "ats_workday",
        "indexed_at": now_iso(),
        "metadata": {
            "ats": "workday",
            "tenant": parts["tenant"],
            "site": parts["site"],
            "search_text": search_text,
            "external_path": external_path,
            "posted_on": detail.get("posted_on") or item.get("postedOn"),
            "source_url": api_url,
            "detail_url": detail.get("detail_url"),
            "detail_locations": detail_locations,
            "country_descriptor": detail.get("country_descriptor"),
            "country_id": detail.get("country_id"),
            "detail_available": bool(detail.get("available")),
        },
    }


def fetch_workday(source: Dict[str, Any], timeout: int, limit: int, target_location: str = "") -> List[Dict[str, Any]]:
    url = str(source.get("url") or "")
    parts = workday_parts(url)
    if not parts:
        return []
    api_url = f"{parts['scheme']}://{parts['host']}/wday/cxs/{parts['tenant']}/{parts['site']}/jobs"
    search_text = str(source.get("search_text") or "")
    applied_facets = dict(source.get("applied_facets") or {})
    auto_us_facet = False
    if (target_location or "").strip().lower() in {"united states", "usa", "us"} and "locationCountry" not in applied_facets:
        applied_facets["locationCountry"] = [WORKDAY_US_COUNTRY_ID]
        auto_us_facet = True
    payload = {
        "appliedFacets": applied_facets,
        "limit": min(max(limit, 1), 20),
        "offset": int(source.get("offset") or 0),
        "searchText": search_text,
    }
    response = requests.post(api_url, headers=HEADERS, json=payload, timeout=timeout)
    if response.status_code == 400 and auto_us_facet:
        payload["appliedFacets"] = source.get("applied_facets") or {}
        response = requests.post(api_url, headers=HEADERS, json=payload, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    company = source_company(source, parts["tenant"])
    items = (data.get("jobPostings") or data.get("jobs") or [])[:limit]
    if not items:
        return []
    jobs: List[Dict[str, Any]] = []
    max_workers = min(6, max(1, len(items)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(
                build_workday_job,
                source,
                parts,
                company,
                item,
                search_text,
                api_url,
                timeout,
                target_location,
            )
            for item in items
        ]
        for future in as_completed(futures):
            try:
                job = future.result()
            except Exception:
                continue
            if job:
                jobs.append(job)
    return jobs


def jsonld_items(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, list):
        for item in value:
            yield from jsonld_items(item)
    elif isinstance(value, dict):
        raw_type = value.get("@type") or []
        types = [raw_type] if isinstance(raw_type, str) else raw_type
        if "JobPosting" in types:
            yield value
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in value:
                yield from jsonld_items(value[key])


def location_text(job: Dict[str, Any]) -> str:
    location = job.get("jobLocation") or job.get("applicantLocationRequirements") or ""
    if isinstance(location, list):
        return "; ".join(location_text({"jobLocation": item}) for item in location)
    if isinstance(location, dict):
        address = location.get("address") or {}
        if isinstance(address, dict):
            return ", ".join(str(address.get(key) or "") for key in ["addressLocality", "addressRegion", "addressCountry"] if address.get(key)) or "Unknown"
        return str(address or location.get("name") or "Unknown")
    return str(location or "Unknown")


def org_name(job: Dict[str, Any], fallback: str) -> str:
    org = job.get("hiringOrganization") or {}
    if isinstance(org, dict):
        return source_company({"company": org.get("name")}, fallback)
    return fallback


def fetch_schema(source: Dict[str, Any], timeout: int, limit: int) -> List[Dict[str, Any]]:
    url = str(source.get("url") or "")
    if not url:
        return []
    response = requests.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    scripts = re.findall(
        r"(?is)<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        response.text,
    )
    jobs = []
    for script in scripts:
        try:
            data = json.loads(html.unescape(script.strip()))
        except Exception:
            continue
        for item in jsonld_items(data):
            company = org_name(item, source_company(source, urlparse(url).hostname or "Unknown"))
            apply_link = item.get("url") or item.get("applicationContact") or url
            if isinstance(apply_link, dict):
                apply_link = apply_link.get("url") or url
            jobs.append({
                "job_id": stable_job_id("schema", str(item.get("identifier") or ""), company, item.get("title") or "", str(apply_link)),
                "company": company,
                "role": item.get("title") or "Unknown",
                "job_description": strip_html(item.get("description") or ""),
                "location": location_text(item),
                "apply_link": str(apply_link),
                "source": "schema_jobposting",
                "indexed_at": now_iso(),
                "metadata": {
                    "ats": "schema",
                    "date_posted": item.get("datePosted"),
                    "valid_through": item.get("validThrough"),
                    "employment_type": item.get("employmentType"),
                    "source_url": url,
                },
            })
            if len(jobs) >= limit:
                return jobs
    return jobs


def matches_filters(job: Dict[str, Any], keywords: List[str], location: str) -> bool:
    loc = (location or "").strip().lower()
    job_location = str(job.get("location") or "").lower()
    metadata = job.get("metadata") or {}
    detail_locations = " ".join(str(item or "") for item in metadata.get("detail_locations") or []).lower()
    country_descriptor = str(metadata.get("country_descriptor") or "").lower()
    country_id = str(metadata.get("country_id") or "")
    location_surface = " ".join([
        job_location,
        detail_locations,
        country_descriptor,
        str(job.get("apply_link") or "").lower(),
        str(metadata.get("external_path") or "").lower(),
    ])
    if loc in {"united states", "usa", "us"}:
        if country_id and country_id != WORKDAY_US_COUNTRY_ID:
            return False
        if country_descriptor and "united states" not in country_descriptor and "usa" not in country_descriptor:
            return False
        if any(term in location_surface for term in NON_US_LOCATION_TERMS):
            return False
    elif loc and loc not in {"anywhere"}:
        full_text = " ".join([
            str(job.get("company") or ""),
            str(job.get("role") or ""),
            str(job.get("job_description") or ""),
            job_location,
        ]).lower()
        if loc not in job_location and loc not in full_text:
            return False
    if is_entry_query(keywords):
        return looks_entry_level_tech_job(job)
    text = " ".join([
        str(job.get("company") or ""),
        str(job.get("role") or ""),
        str(job.get("job_description") or ""),
        str(job.get("location") or ""),
    ]).lower()
    terms = [term.strip().lower() for term in keywords if term and term.strip()]
    if terms and not any(term in text for term in terms):
        return False
    return True


def scan_ats_sources(sources: List[Dict[str, Any]], keywords: Optional[List[str]] = None, location: str = "", limit: int = 50, timeout: int = 20) -> Dict[str, Any]:
    keywords = keywords or []
    jobs: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    enabled_sources = [source for source in sources if source.get("enabled") is not False]
    if not enabled_sources:
        return {"jobs": [], "errors": []}
    per_source_limit = max(1, min(100, (max(limit, 1) + len(enabled_sources) - 1) // len(enabled_sources)))
    ats_fetch_limit = max(per_source_limit, 100 if keywords else per_source_limit)
    workday_fetch_limit = min(20, max(per_source_limit, 8 if keywords else per_source_limit))

    def scan_one_source(source: Dict[str, Any]) -> Dict[str, Any]:
        source_type = infer_source_type(source)
        try:
            if source_type in {"greenhouse", "greenhouse_board"}:
                found = fetch_greenhouse(source, timeout, ats_fetch_limit)
            elif source_type in {"lever", "lever_board"}:
                found = fetch_lever(source, timeout, ats_fetch_limit)
            elif source_type == "workday":
                found = []
                for search_text in workday_search_texts(source, keywords):
                    workday_source = dict(source)
                    workday_source["search_text"] = search_text
                    found.extend(fetch_workday(workday_source, timeout, workday_fetch_limit, location))
                    if len([job for job in found if matches_filters(job, keywords, location)]) >= per_source_limit:
                        break
            else:
                found = fetch_schema(source, timeout, ats_fetch_limit)
            filtered = [job for job in found if matches_filters(job, keywords, location)]
            return {"jobs": filtered[:per_source_limit], "errors": []}
        except Exception as err:
            return {
                "jobs": [],
                "errors": [{
                    "source": source.get("url") or source.get("board_token") or source_type,
                    "source_type": source_type,
                    "error": str(err)[:500],
                }],
            }

    max_source_workers = min(4, max(1, len(enabled_sources)))
    with ThreadPoolExecutor(max_workers=max_source_workers) as executor:
        futures = [executor.submit(scan_one_source, source) for source in enabled_sources]
        for future in as_completed(futures):
            result = future.result()
            jobs.extend(result.get("jobs") or [])
            errors.extend(result.get("errors") or [])
    deduped: Dict[str, Dict[str, Any]] = {}
    for job in jobs:
        if job.get("job_id"):
            deduped[job["job_id"]] = job
    return {"jobs": list(deduped.values())[:limit], "errors": errors}
