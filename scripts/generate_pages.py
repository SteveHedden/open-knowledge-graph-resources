#!/usr/bin/env python3
"""Generate individual HTML pages for page-worthy catalog items.

Strict criteria: English label + meaningful description + verified working homepage.
Outputs pages to site/ontology/{slug}/ and site/software/{slug}/.
Also generates sitemap.xml and a QID-to-slug mapping for the frontend.
"""

import asyncio
import argparse
import json
import os
import html
import shutil
import ssl
from pathlib import Path

import aiohttp

from semantic_config import (
    CATEGORIES_VOCAB_PATH,
    SOFTWARE_TYPES_VOCAB_PATH,
    load_controlled_vocabulary,
)
from recommendation_coverage import (
    RecommendationCoverageError,
    append_diagnostics,
    baseline_generation_id,
    baseline_survivors,
    coverage_by_catalog,
    evaluate_coverage,
    load_policy,
    qualifying_reasons_by_catalog,
)

ROOT_DIR = Path(
    os.environ.get("OKG_CATALOG_ROOT", Path(__file__).resolve().parent.parent)
).resolve()
SITE_DIR = str(ROOT_DIR / "site")
DATA_DIR = str(ROOT_DIR / "data")
BASE_URL = "https://openknowledgegraphs.com"
COVERAGE_POLICY_PATH = ROOT_DIR / "validation" / "recommendation-coverage-policy.json"
RELATED_DIAGNOSTICS_PATH = Path(
    os.environ.get("OKG_RELATED_DIAGNOSTICS_PATH", ROOT_DIR / "build" / "related-resources.json")
).resolve()

GENERIC_DESCRIPTIONS = {
    "ontology", "wikimedia glossary list article", "wikimedia list article",
    "ontology part of obofoundry", "glossary", "controlled vocabulary",
    "taxonomy", "vocabulary", "thesaurus", "classification scheme",
    "terminology", "nomenclature",
}

CATEGORY_SLUGS = {
    concept.label: concept.slug
    for concept in load_controlled_vocabulary(CATEGORIES_VOCAB_PATH).concepts
}

SOFTWARE_TYPE_SLUGS = {
    concept.label: concept.slug
    for concept in load_controlled_vocabulary(SOFTWARE_TYPES_VOCAB_PATH).concepts
}

PARKED_SIGNALS = [
    "buy this domain", "domain for sale", "this domain is for sale",
    "domain parking", "parked domain", "sedoparking", "hugedomains",
]

SOFT_404_SIGNALS = [
    "page not found", "404 not found", "not found</",
    "page doesn't exist", "page does not exist",
    "this page has been removed", "the page has been removed",
    "error 404",
]


# --- Quality filters ---

def passes_content_filter(item):
    """Label + meaningful description + has homepage."""
    title = (item.get("title") or "").strip()
    if not title or title.startswith("Q"):
        return False
    desc = (item.get("description") or "").strip().lower()
    if not desc or desc in GENERIC_DESCRIPTIONS or len(desc) < 15:
        return False
    if not (item.get("homepage") or "").strip():
        return False
    return True


async def check_links(items):
    """Check homepage URLs, return set of working URLs."""
    urls = list(set(i["homepage"].strip() for i in items))
    print(f"  Checking {len(urls)} URLs...")

    results = {}
    semaphore = asyncio.Semaphore(20)
    checked = 0

    async def check(session, url):
        nonlocal checked
        async with semaphore:
            try:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=12),
                    allow_redirects=True, ssl=False
                ) as resp:
                    status = resp.status
                    if status == 403:
                        results[url] = True
                    elif status >= 400:
                        results[url] = False
                    else:
                        try:
                            body = (await resp.text(encoding="utf-8", errors="ignore")).lower()
                        except Exception:
                            body = ""
                        if len(body) < 100:
                            results[url] = False
                        elif any(s in body for s in PARKED_SIGNALS):
                            results[url] = False
                        elif any(s in body for s in SOFT_404_SIGNALS) and len(body) < 50000:
                            results[url] = False
                        else:
                            results[url] = True
            except Exception:
                results[url] = False
            checked += 1
            if checked % 100 == 0:
                print(f"    ...{checked}/{len(urls)}")

    headers = {"User-Agent": "OKG-LinkChecker/1.0 (https://openknowledgegraphs.com)"}
    async with aiohttp.ClientSession(headers=headers) as session:
        await asyncio.gather(*[check(session, url) for url in urls])

    good = {u for u, ok in results.items() if ok}
    print(f"  {len(good)}/{len(urls)} URLs OK")
    return good


# --- HTML generation ---

def extract_qid(wikidata_url):
    return wikidata_url.split("/")[-1] if wikidata_url else ""


def slug_from_canonical_url(url, dataset):
    """Extract the slug fetch_data.py already assigned for this item, e.g.
    https://openknowledgegraphs.com/software/foops/ -> "foops". Returns None
    if the URL is missing or doesn't match this dataset's path prefix.
    """
    prefix = f"{BASE_URL}/{dataset}/"
    if not url or not url.startswith(prefix):
        return None
    return url[len(prefix):].rstrip("/") or None


def esc(text):
    return html.escape(text or "", quote=True)


def is_non_empty_string(value):
    return isinstance(value, str) and bool(value.strip())


def require_json_ld_identity(item):
    """Reject page records whose required content or identity is missing."""
    required_fields = ("canonicalUrl", "title", "description", "homepage", "wikidataId")
    missing = [field for field in required_fields if not is_non_empty_string(item.get(field))]
    if missing:
        raise ValueError(f"Cannot serialize JSON-LD without: {', '.join(missing)}")


def make_json_ld(item, dataset):
    require_json_ld_identity(item)
    schema_type = "SoftwareApplication" if dataset == "software" else "DefinedTermSet"
    ld = {
        "@context": "https://schema.org",
        "@type": schema_type,
        "@id": item["canonicalUrl"],
        "name": item["title"],
        "description": item["description"],
        "url": item["homepage"],
        "sameAs": item["wikidataId"],
    }

    licenses = item.get("licenses")
    if isinstance(licenses, list):
        known_licenses = [value for value in licenses if is_non_empty_string(value)]
        if known_licenses:
            ld["license"] = known_licenses[0]

    ld["isPartOf"] = {
        "@type": "DataCatalog",
        "name": "Open Knowledge Graphs",
        "url": BASE_URL,
    }

    if is_non_empty_string(item.get("latestVersion")):
        ld["softwareVersion"] = item["latestVersion"]
    if is_non_empty_string(item.get("releaseDate")):
        ld["datePublished"] = item["releaseDate"]
    if is_non_empty_string(item.get("sourceRepo")):
        ld["codeRepository"] = item["sourceRepo"]
    if is_non_empty_string(item.get("softwareType")):
        ld["applicationCategory"] = item["softwareType"]

    programming_languages = item.get("programmingLanguages")
    if isinstance(programming_languages, list):
        known_languages = [value for value in programming_languages if is_non_empty_string(value)]
        if known_languages:
            ld["programmingLanguage"] = known_languages[0] if len(known_languages) == 1 else known_languages

    creators = item.get("creators")
    if isinstance(creators, list):
        creator_entries = []
        for c in creators:
            if not isinstance(c, dict):
                continue
            if not is_non_empty_string(c.get("type")) or not is_non_empty_string(c.get("name")):
                continue
            entry = {"@type": c["type"], "name": c["name"]}
            same_as = [
                url
                for url in (c.get("wikidataId"), c.get("githubProfile"), c.get("googleScholarProfile"))
                if is_non_empty_string(url)
            ]
            if same_as:
                entry["sameAs"] = same_as[0] if len(same_as) == 1 else same_as
            creator_entries.append(entry)
        if creator_entries:
            ld["creator"] = creator_entries[0] if len(creator_entries) == 1 else creator_entries
    return json.dumps(ld, indent=2)


def make_page(item, dataset, slug):
    title = esc(item["title"])
    desc = esc(item.get("description", ""))
    homepage = esc(item.get("homepage", ""))
    source_repo = esc(item.get("sourceRepo", ""))
    namespace_uri = esc(item.get("namespaceURI", ""))
    source_url = source_repo or namespace_uri
    wikidata_url = esc(item.get("wikidataId", ""))
    category = item.get("category", "")
    types = item.get("types", [])
    licenses = item.get("licenses", [])
    json_ld = make_json_ld(item, dataset)

    css_path = "../../style.css"
    favicon_path = "../../favicon.svg"

    types_html = ""
    if types:
        types_html = " ".join(f'<span class="detail-tag">{esc(t)}</span>' for t in types)

    category_html = ""
    if category:
        category_slug = CATEGORY_SLUGS.get(category, "")
        category_html = f'<a href="{BASE_URL}/?category={category_slug}" class="detail-category">{esc(category)}</a>'

    software_type = item.get("softwareType", "")
    software_type_html = ""
    if software_type:
        software_type_slug = SOFTWARE_TYPE_SLUGS.get(software_type, "")
        software_type_html = (
            f'<a href="{BASE_URL}/?tab=software&softwareType={software_type_slug}" '
            f'class="detail-category">{esc(software_type)}</a>'
        )

    license_html = ""
    if licenses:
        license_html = f'<p class="detail-field"><strong>License:</strong> {esc(licenses[0])}</p>'

    version_html = ""
    if item.get("latestVersion"):
        v = esc(item["latestVersion"])
        d = esc(item.get("releaseDate", ""))
        version_html = f'<p class="detail-field"><strong>Latest version:</strong> {v}'
        if d:
            version_html += f" ({d})"
        version_html += "</p>"

    related_tools_html = ""
    related_tools = item.get("relatedTools", [])
    if related_tools:
        related_heading = "Related tools" if dataset == "software" else "Related resources"
        related_links = " ".join(
            f'<a href="{esc(entry["canonicalUrl"])}">{esc(entry["title"])}</a>' for entry in related_tools
        )
        related_tools_html = f"""
      <div class="detail-related">
        <h2 class="detail-related-heading">{related_heading}</h2>
        <div class="detail-related-links">
          {related_links}
        </div>
      </div>"""

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title} - Open Knowledge Graphs</title>
    <meta name="description" content="{desc}">
    <link rel="icon" type="image/svg+xml" href="{favicon_path}">
    <link rel="icon" type="image/png" sizes="192x192" href="../../favicon.png">
    <meta property="og:title" content="{title} - Open Knowledge Graphs">
    <meta property="og:description" content="{desc}">
    <meta property="og:type" content="website">
    <meta property="og:url" content="{BASE_URL}/{dataset}/{slug}/">
    <link rel="stylesheet" href="{css_path}">
    <script type="application/ld+json">
    {json_ld}
    </script>
    <style>
      .detail-page {{
        max-width: 720px;
        margin: 2rem auto;
        padding: 0 1.5rem;
      }}
      .detail-back {{
        display: inline-block;
        margin-bottom: 1.5rem;
        color: var(--brand);
        text-decoration: none;
        font-size: 0.9rem;
      }}
      .detail-back:hover {{
        text-decoration: underline;
      }}
      .detail-title {{
        font-size: 1.75rem;
        margin: 0 0 0.75rem;
        line-height: 1.3;
      }}
      .detail-description {{
        font-size: 1.05rem;
        line-height: 1.6;
        color: var(--text-secondary, #555);
        margin-bottom: 1.5rem;
      }}
      .detail-meta {{
        display: flex;
        flex-wrap: wrap;
        gap: 0.5rem;
        margin-bottom: 1.5rem;
      }}
      .detail-tag {{
        background: var(--bg-muted, #f0f0f0);
        padding: 0.25rem 0.75rem;
        border-radius: 999px;
        font-size: 0.85rem;
      }}
      .detail-category {{
        background: var(--highlight, #f6ca67);
        padding: 0.25rem 0.75rem;
        border-radius: 999px;
        font-size: 0.85rem;
        text-decoration: none;
        color: inherit;
      }}
      .detail-links {{
        display: flex;
        gap: 1rem;
        margin-bottom: 1.5rem;
        flex-wrap: wrap;
      }}
      .detail-links a {{
        display: inline-flex;
        align-items: center;
        gap: 0.4rem;
        padding: 0.5rem 1rem;
        border: 1px solid var(--brand);
        border-radius: 6px;
        text-decoration: none;
        color: var(--brand);
        font-size: 0.9rem;
        transition: background 0.15s, color 0.15s;
      }}
      .detail-links a:hover {{
        background: var(--brand);
        color: #fff;
      }}
      .detail-field {{
        margin: 0.5rem 0;
        font-size: 0.95rem;
      }}
      .detail-field strong {{
        color: var(--text-primary, #333);
      }}
      .detail-related {{
        margin-top: 2rem;
        padding-top: 1.5rem;
        border-top: 1px solid var(--bg-muted, #f0f0f0);
      }}
      .detail-related-heading {{
        font-size: 1.1rem;
        margin: 0 0 0.75rem;
      }}
      .detail-related-links {{
        display: flex;
        flex-wrap: wrap;
        gap: 0.5rem;
      }}
      .detail-related-links a {{
        background: var(--bg-muted, #f0f0f0);
        padding: 0.25rem 0.75rem;
        border-radius: 999px;
        font-size: 0.85rem;
        text-decoration: none;
        color: inherit;
      }}
      .detail-related-links a:hover {{
        background: var(--highlight, #f6ca67);
      }}
    </style>
  </head>
  <body>
    <div class="detail-page">
      <a href="{BASE_URL}/" class="detail-back">&larr; Browse all resources</a>
      <h1 class="detail-title">{title}</h1>
      <div class="detail-meta">
        {types_html}
        {category_html}
        {software_type_html}
      </div>
      <p class="detail-description">{desc}</p>
      <div class="detail-links">
        {"" if not homepage else f'<a href="{homepage}" target="_blank" rel="noopener noreferrer">Homepage &nearr;</a>'}
        {"" if not source_url else f'<a href="{source_url}" target="_blank" rel="noopener noreferrer">Source &nearr;</a>'}
        <a href="{wikidata_url}" target="_blank" rel="noopener noreferrer">Wikidata &nearr;</a>
      </div>
      {license_html}
      {version_html}
      {related_tools_html}
    </div>
  </body>
</html>"""


# --- Sitemap ---

def generate_sitemap(pages):
    """Generate sitemap.xml with all pages."""
    urls = [
        f'  <url>\n    <loc>{BASE_URL}/</loc>\n    <changefreq>daily</changefreq>\n    <priority>1.0</priority>\n  </url>',
        f'  <url>\n    <loc>{BASE_URL}/data/ontologies.json</loc>\n    <changefreq>daily</changefreq>\n    <priority>0.8</priority>\n  </url>',
        f'  <url>\n    <loc>{BASE_URL}/data/software.json</loc>\n    <changefreq>daily</changefreq>\n    <priority>0.8</priority>\n  </url>',
        f'  <url>\n    <loc>{BASE_URL}/data/ontologies.ttl</loc>\n    <changefreq>daily</changefreq>\n    <priority>0.7</priority>\n  </url>',
        f'  <url>\n    <loc>{BASE_URL}/data/software.ttl</loc>\n    <changefreq>daily</changefreq>\n    <priority>0.7</priority>\n  </url>',
        f'  <url>\n    <loc>{BASE_URL}/ontology.ttl</loc>\n    <changefreq>monthly</changefreq>\n    <priority>0.6</priority>\n  </url>',
        f'  <url>\n    <loc>{BASE_URL}/vocabularies/categories.ttl</loc>\n    <changefreq>monthly</changefreq>\n    <priority>0.6</priority>\n  </url>',
        f'  <url>\n    <loc>{BASE_URL}/vocabularies/software-types.ttl</loc>\n    <changefreq>monthly</changefreq>\n    <priority>0.6</priority>\n  </url>',
        f'  <url>\n    <loc>{BASE_URL}/sources.ttl</loc>\n    <changefreq>monthly</changefreq>\n    <priority>0.5</priority>\n  </url>',
        f'  <url>\n    <loc>{BASE_URL}/curation/classifications.ttl</loc>\n    <changefreq>daily</changefreq>\n    <priority>0.5</priority>\n  </url>',
        f'  <url>\n    <loc>{BASE_URL}/llms.txt</loc>\n    <changefreq>monthly</changefreq>\n    <priority>0.7</priority>\n  </url>',
    ]

    for dataset, qid in pages:
        urls.append(
            f'  <url>\n    <loc>{BASE_URL}/{dataset}/{qid}/</loc>\n    <changefreq>weekly</changefreq>\n    <priority>0.6</priority>\n  </url>'
        )

    sitemap = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    sitemap += "\n".join(urls)
    sitemap += "\n</urlset>\n"
    return sitemap


# --- Main ---

CATALOG_FILES = {
    "resource": "ontologies.json",
    "software": "software.json",
}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-link-check",
        action="store_true",
        help=(
            "Skip homepage requests. Without a membership baseline this retains the "
            "legacy admit-all behavior; with a baseline it admits only unchanged, "
            "previously verified pages."
        ),
    )
    parser.add_argument(
        "--membership-baseline",
        type=Path,
        help=(
            "Immutable catalog root whose page_qids.json and catalog JSON identify "
            "previously verified page membership. New or homepage-changed candidates "
            "still require a successful link check."
        ),
    )
    return parser.parse_args(argv)


def _read_json(path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_verified_page_membership(baseline_root):
    """Return verified baseline pages keyed by (dataset, QID).

    An entirely empty baseline is valid for the first publication. Once a page
    registry exists, both catalog projections are required so verification is
    bound to the exact homepage that was previously published.
    """
    baseline_root = Path(baseline_root).resolve()
    registry_path = baseline_root / "data" / "page_qids.json"
    if not registry_path.is_file():
        return {}

    registry = _read_json(registry_path)
    membership = {}
    for dataset, catalog_filename in CATALOG_FILES.items():
        entries = registry.get(dataset)
        if not isinstance(entries, dict):
            raise ValueError(f"Baseline page registry lacks a {dataset!r} mapping.")

        catalog_path = baseline_root / "data" / catalog_filename
        if not catalog_path.is_file():
            raise ValueError(f"Baseline page registry requires {catalog_path}.")
        payload = _read_json(catalog_path)
        items = payload.get("items")
        if not isinstance(items, list):
            raise ValueError(f"Baseline catalog has no item list: {catalog_path}")

        items_by_qid = {
            extract_qid(item.get("wikidataId", "")): item
            for item in items
            if isinstance(item, dict) and extract_qid(item.get("wikidataId", ""))
        }
        for qid, slug in entries.items():
            item = items_by_qid.get(qid)
            if item is None:
                raise ValueError(
                    f"Baseline page registry contains {dataset} {qid}, but its catalog record is missing."
                )
            homepage = (item.get("homepage") or "").strip()
            if not homepage:
                raise ValueError(f"Baseline page {dataset} {qid} has no homepage.")
            membership[(dataset, qid)] = {
                "homepage": homepage,
                "slug": str(slug),
            }
    return membership


def split_membership_candidates(candidates, baseline_membership):
    """Separate unchanged verified pages from candidates needing live checks."""
    unchanged = set()
    needs_check = []
    for dataset, item in candidates:
        qid = extract_qid(item.get("wikidataId", ""))
        slug = slug_from_canonical_url(item.get("canonicalUrl", ""), dataset)
        homepage = (item.get("homepage") or "").strip()
        baseline = baseline_membership.get((dataset, qid))
        if (
            baseline is not None
            and baseline["homepage"] == homepage
            and baseline["slug"] == slug
        ):
            unchanged.add((dataset, qid))
        else:
            needs_check.append(item)
    return unchanged, needs_check


def main(argv=None):
    args = parse_args(argv)

    with open(os.path.join(DATA_DIR, "ontologies.json")) as f:
        ont = json.load(f)["items"]
    with open(os.path.join(DATA_DIR, "software.json")) as f:
        sw = json.load(f)["items"]

    # Step 1: Content filter
    candidates = []
    for dataset, items in [("resource", ont), ("software", sw)]:
        for item in items:
            if passes_content_filter(item):
                candidates.append((dataset, item))

    print(f"Content filter: {len(candidates)} candidates")

    # Step 2: Preserve unchanged pages from an immutable verified generation,
    # and check only new pages or records whose homepage/slug changed.
    baseline_membership = None
    unchanged_verified = set()
    if args.membership_baseline is not None:
        baseline_membership = load_verified_page_membership(args.membership_baseline)
        unchanged_verified, needs_check = split_membership_candidates(
            candidates,
            baseline_membership,
        )
        print(
            f"Baseline membership: preserving {len(unchanged_verified)} unchanged "
            f"verified pages; {len(needs_check)} require verification"
        )
        if args.skip_link_check:
            print("Skipping new/changed link checks; no unverified pages will be admitted")
            good_urls = set()
        elif needs_check:
            good_urls = asyncio.run(check_links(needs_check))
        else:
            good_urls = set()
    elif args.skip_link_check:
        print("Skipping link check (--skip-link-check)")
        good_urls = None
    else:
        all_items = [item for _, item in candidates]
        good_urls = asyncio.run(check_links(all_items))

    # Step 3: Determine the final page-worthy set (same checks as before), so we
    # know which canonicalUrls will actually have a page before rendering any
    # of them — needed to prune relatedTools down to links that won't 404.
    survivors = []
    skipped_no_canonical_url = 0
    for dataset, item in candidates:
        qid = extract_qid(item.get("wikidataId", ""))
        if not qid:
            continue

        slug = slug_from_canonical_url(item.get("canonicalUrl", ""), dataset)
        if not slug:
            skipped_no_canonical_url += 1
            continue

        if baseline_membership is not None:
            if (
                (dataset, qid) not in unchanged_verified
                and item["homepage"].strip() not in good_urls
            ):
                continue
        elif good_urls is not None and item["homepage"].strip() not in good_urls:
            continue

        survivors.append((dataset, item, qid, slug))

    if skipped_no_canonical_url:
        print(f"Skipped {skipped_no_canonical_url} candidates with no canonicalUrl (stale data/*.json — rerun fetch_data.py)")

    survivor_urls = {item.get("canonicalUrl") for _, item, _, _ in survivors if item.get("canonicalUrl")}

    # Step 4: Gate the exact post-content-filter, post-link-check page set before
    # deleting or publishing any generated page.
    candidate_coverage = coverage_by_catalog(survivors)
    existing_diagnostics = {}
    if RELATED_DIAGNOSTICS_PATH.is_file():
        existing_diagnostics = _read_json(RELATED_DIAGNOSTICS_PATH)
    reason_distributions = qualifying_reasons_by_catalog(survivors, existing_diagnostics)
    for dataset in ("resource", "software"):
        candidate_coverage[dataset]["qualifyingReasonDistribution"] = reason_distributions[dataset]
    baseline_coverage = None
    baseline_id = None
    if args.membership_baseline is not None:
        baseline_coverage = coverage_by_catalog(
            baseline_survivors(args.membership_baseline.resolve())
        )
        baseline_id = baseline_generation_id(args.membership_baseline.resolve())
    coverage_report = evaluate_coverage(
        candidate_coverage,
        baseline_coverage,
        load_policy(COVERAGE_POLICY_PATH),
        baseline_id,
    )
    append_diagnostics(RELATED_DIAGNOSTICS_PATH, coverage_report)
    for dataset in ("resource", "software"):
        row = candidate_coverage[dataset]
        print(
            f"Recommendation coverage ({dataset}): "
            f"{row['pagesWithRecommendations']}/{row['finalPageCount']} "
            f"({float(row['coverageShare']):.1%})"
        )
    if not coverage_report["gate"]["passed"]:
        raise RecommendationCoverageError(coverage_report)

    # Step 5: The release gate passed; replace old generated pages.
    for d in ["resource", "software"]:
        dirpath = os.path.join(SITE_DIR, d)
        if os.path.exists(dirpath):
            shutil.rmtree(dirpath)

    # Step 6: Generate pages, using the slug already assigned by fetch_data.py
    # (item["canonicalUrl"], e.g. https://openknowledgegraphs.com/software/foops/)
    # so a resource's URI never has to change once its page appears.
    generated = 0
    pages = []
    page_slugs = {"resource": {}, "software": {}}  # QID -> slug mapping

    for dataset, item, qid, slug in survivors:
        related_tools = item.get("relatedTools")
        if related_tools:
            item = {**item, "relatedTools": [r for r in related_tools if r["canonicalUrl"] in survivor_urls]}

        page_dir = os.path.join(SITE_DIR, dataset, slug)
        os.makedirs(page_dir, exist_ok=True)

        page_html = make_page(item, dataset, slug)
        with open(os.path.join(page_dir, "index.html"), "w") as f:
            f.write(page_html)

        pages.append((dataset, slug))
        page_slugs[dataset][qid] = slug
        generated += 1

    print(f"Generated {generated} pages")

    # Step 7: Generate sitemap
    sitemap = generate_sitemap(pages)
    with open(os.path.join(SITE_DIR, "sitemap.xml"), "w") as f:
        f.write(sitemap)
    print(f"Sitemap: {len(pages) + 7} URLs")

    # Step 8: Save QID-to-slug mapping for frontend
    with open(os.path.join(DATA_DIR, "page_qids.json"), "w") as f:
        json.dump(page_slugs, f)
    print(f"Saved page_qids.json ({len(page_slugs['resource'])} resource, {len(page_slugs['software'])} software)")


if __name__ == "__main__":
    main()
