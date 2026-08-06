#!/usr/bin/env python3
"""Generate individual HTML pages for page-worthy catalog items.

Strict criteria: English label + meaningful description + verified working homepage.
Outputs pages to site/ontology/{slug}/ and site/software/{slug}/.
Also generates sitemap.xml and a QID-to-slug mapping for the frontend.
"""

import asyncio
import json
import os
import html
import shutil
import ssl
import sys

import aiohttp

SITE_DIR = os.path.join(os.path.dirname(__file__), "..", "site")
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
BASE_URL = "https://openknowledgegraphs.com"

GENERIC_DESCRIPTIONS = {
    "ontology", "wikimedia glossary list article", "wikimedia list article",
    "ontology part of obofoundry", "glossary", "controlled vocabulary",
    "taxonomy", "vocabulary", "thesaurus", "classification scheme",
    "terminology", "nomenclature",
}

CATEGORY_SLUGS = {
    "Life Sciences & Healthcare": "life-sciences-healthcare",
    "Geospatial": "geospatial",
    "Government & Public Sector": "government-public-sector",
    "International Development": "international-development",
    "Finance & Business": "finance-business",
    "Library & Cultural Heritage": "library-cultural-heritage",
    "Technology & Web": "technology-web",
    "Environment & Agriculture": "environment-agriculture",
    "General / Cross-domain": "general-cross-domain",
}

SOFTWARE_TYPE_SLUGS = {
    "Graph Database": "graph-database",
    "SPARQL Tooling": "sparql-tooling",
    "Ontology Engineering": "ontology-engineering",
    "Reasoning & Inference": "reasoning-inference",
    "RDF Data Mapping / ETL": "data-mapping-etl",
    "Developer Library": "developer-library",
    "Knowledge Graph Construction": "knowledge-graph-construction",
    "AI Agent Tooling": "ai-agent-tooling",
    "Visualization": "visualization",
    "Stream Processing": "stream-processing",
}

PARKED_SIGNALS = [
    "buy this domain", "domain for sale", "this domain is for sale",
    "domain parking", "parked domain", "sedoparking", "hugedomains",
]

SOFT_404_SIGNALS = [
    "page not found", "404 not found", "not found</",
    "page doesn't exist", "page does not exist",
    "has been removed", "error 404",
]


# --- Quality filters ---

def passes_content_filter(item):
    """Label + meaningful description + has homepage."""
    title = item.get("title", "")
    if not title or title.startswith("Q"):
        return False
    desc = (item.get("description") or "").strip().lower()
    if not desc or desc in GENERIC_DESCRIPTIONS or len(desc) < 15:
        return False
    if not item.get("homepage"):
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


def make_json_ld(item, dataset):
    schema_type = "SoftwareApplication" if dataset == "software" else "DefinedTermSet"
    ld = {
        "@context": "https://schema.org",
        "@type": schema_type,
        "@id": item.get("canonicalUrl", ""),
        "name": item["title"],
        "description": item.get("description", ""),
        "url": item.get("homepage", ""),
        "sameAs": item.get("wikidataId", ""),
        "license": item["licenses"][0] if item.get("licenses") else "https://creativecommons.org/publicdomain/mark/1.0/",
        "isPartOf": {
            "@type": "DataCatalog",
            "name": "Open Knowledge Graphs",
            "url": BASE_URL,
        },
    }
    if item.get("latestVersion"):
        ld["softwareVersion"] = item["latestVersion"]
    if item.get("releaseDate"):
        ld["datePublished"] = item["releaseDate"]
    if item.get("sourceRepo"):
        ld["codeRepository"] = item["sourceRepo"]
    if item.get("softwareType"):
        ld["applicationCategory"] = item["softwareType"]
    if item.get("programmingLanguages"):
        langs = item["programmingLanguages"]
        ld["programmingLanguage"] = langs[0] if len(langs) == 1 else langs
    creators = item.get("creators", [])
    if creators:
        creator_entries = []
        for c in creators:
            entry = {"@type": c["type"], "name": c["name"]}
            same_as = [
                url
                for url in (c.get("wikidataId"), c.get("githubProfile"), c.get("googleScholarProfile"))
                if url
            ]
            if same_as:
                entry["sameAs"] = same_as[0] if len(same_as) == 1 else same_as
            creator_entries.append(entry)
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
        related_links = " ".join(
            f'<a href="{esc(entry["canonicalUrl"])}">{esc(entry["title"])}</a>' for entry in related_tools
        )
        related_tools_html = f"""
      <div class="detail-related">
        <h2 class="detail-related-heading">Related tools</h2>
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

def main():
    skip_link_check = "--skip-link-check" in sys.argv

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

    # Step 2: Link check (unless skipped)
    if skip_link_check:
        print("Skipping link check (--skip-link-check)")
        good_urls = None
    else:
        all_items = [item for _, item in candidates]
        good_urls = asyncio.run(check_links(all_items))

    # Step 3: Clean old generated pages
    for d in ["resource", "software"]:
        dirpath = os.path.join(SITE_DIR, d)
        if os.path.exists(dirpath):
            shutil.rmtree(dirpath)

    # Step 4: Generate pages, using the slug already assigned by fetch_data.py
    # (item["canonicalUrl"], e.g. https://openknowledgegraphs.com/software/foops/)
    # so a resource's URI never has to change once its page appears.
    generated = 0
    skipped_no_canonical_url = 0
    pages = []
    page_slugs = {"resource": {}, "software": {}}  # QID -> slug mapping

    for dataset, item in candidates:
        if good_urls is not None and item["homepage"].strip() not in good_urls:
            continue

        qid = extract_qid(item.get("wikidataId", ""))
        if not qid:
            continue

        slug = slug_from_canonical_url(item.get("canonicalUrl", ""), dataset)
        if not slug:
            skipped_no_canonical_url += 1
            continue

        page_dir = os.path.join(SITE_DIR, dataset, slug)
        os.makedirs(page_dir, exist_ok=True)

        page_html = make_page(item, dataset, slug)
        with open(os.path.join(page_dir, "index.html"), "w") as f:
            f.write(page_html)

        pages.append((dataset, slug))
        page_slugs[dataset][qid] = slug
        generated += 1

    if skipped_no_canonical_url:
        print(f"Skipped {skipped_no_canonical_url} candidates with no canonicalUrl (stale data/*.json — rerun fetch_data.py)")

    print(f"Generated {generated} pages")

    # Step 5: Generate sitemap
    sitemap = generate_sitemap(pages)
    with open(os.path.join(SITE_DIR, "sitemap.xml"), "w") as f:
        f.write(sitemap)
    print(f"Sitemap: {len(pages) + 7} URLs")

    # Step 6: Save QID-to-slug mapping for frontend
    with open(os.path.join(DATA_DIR, "page_qids.json"), "w") as f:
        json.dump(page_slugs, f)
    print(f"Saved page_qids.json ({len(page_slugs['resource'])} resource, {len(page_slugs['software'])} software)")


if __name__ == "__main__":
    main()
