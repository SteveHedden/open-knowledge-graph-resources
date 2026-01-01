# scripts/build_catalog.py
#
# Build exports from YAML SSOT:
#   - dist/catalog.csv
#   - dist/catalog.ttl
#
# Usage (from repo root):
#   pip install pyyaml rdflib
#   python scripts/build_catalog.py
#
# Notes:
# - Supports YAML files anywhere under /resources (including subfolders).
# - Each YAML file is one "resource" record.
# - Distributions become dcat:Distribution nodes in TTL.
# - CSV is one row per resource (formats are summarized from distributions).

import csv
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml
from rdflib import Graph, Literal, Namespace, RDF, URIRef
from rdflib.namespace import DCTERMS, OWL, SKOS, XSD

DCAT = Namespace("http://www.w3.org/ns/dcat#")

ALLOWED_KINDS = {"ontology", "taxonomy", "vocabulary", "reference-dataset", "schema"}


@dataclass
class Distribution:
    format: Optional[str] = None
    media_type: Optional[str] = None
    download_url: Optional[str] = None


@dataclass
class Resource:
    id: str
    name: str
    kind: str
    domain: Optional[str] = None
    free: Optional[bool] = None
    publisher: Optional[str] = None
    description: Optional[str] = None
    license_name: Optional[str] = None
    license_url: Optional[str] = None
    homepage: Optional[str] = None
    distributions: List[Distribution] = field(default_factory=list)


def _req(obj: Dict[str, Any], key: str, path: str) -> Any:
    """Require a YAML key to exist and be non-empty."""
    if key not in obj or obj[key] is None or obj[key] == "":
        raise ValueError(f"Missing required field '{key}' at {path}")
    return obj[key]


def _bool_or_none(v: Any) -> Optional[bool]:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in {"true", "yes", "y", "1"}:
            return True
        if s in {"false", "no", "n", "0"}:
            return False
    raise ValueError(f"Invalid boolean value: {v!r}")


def iter_yaml_files(root_dir: str) -> List[str]:
    """Return all .yaml/.yml files under root_dir (recursively)."""
    out: List[str] = []
    for r, _, files in os.walk(root_dir):
        for fn in files:
            if fn.endswith(".yaml") or fn.endswith(".yml"):
                out.append(os.path.join(r, fn))
    return sorted(out)


def load_resources(resources_dir: str) -> List[Resource]:
    items: List[Resource] = []
    for fp in iter_yaml_files(resources_dir):
        with open(fp, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        # Allow _template.yaml etc. to exist without breaking builds
        filename = os.path.basename(fp)
        if filename.startswith("_"):
            continue

        rid = _req(data, "id", fp)
        name = _req(data, "name", fp)
        kind = _req(data, "kind", fp)
        if kind not in ALLOWED_KINDS:
            raise ValueError(
                f"Invalid kind '{kind}' in {fp}. Allowed: {sorted(ALLOWED_KINDS)}"
            )

        license_obj = data.get("license") or {}
        links_obj = data.get("links") or {}

        dists: List[Distribution] = []
        for d in (data.get("distributions") or []):
            dists.append(
                Distribution(
                    format=d.get("format"),
                    media_type=d.get("media_type"),
                    download_url=d.get("download_url"),
                )
            )

        items.append(
            Resource(
                id=rid,
                name=name,
                kind=kind,
                domain=data.get("domain"),
                free=_bool_or_none(data.get("free")),
                publisher=data.get("publisher"),
                description=data.get("description"),
                license_name=license_obj.get("name"),
                license_url=license_obj.get("url"),
                homepage=links_obj.get("homepage"),
                distributions=dists,
            )
        )
    return items


def summarize_formats(r: Resource) -> str:
    fmts = [d.format for d in r.distributions if d.format]
    seen = set()
    uniq: List[str] = []
    for f in fmts:
        if f not in seen:
            uniq.append(f)
            seen.add(f)
    return "|".join(uniq)


def write_csv(resources: List[Resource], out_csv: str) -> None:
    os.makedirs(os.path.dirname(out_csv), exist_ok=True)

    fieldnames = [
        "id",
        "name",
        "kind",
        "domain",
        "free",
        "license_name",
        "license_url",
        "publisher",
        "homepage",
        "formats",
    ]

    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in resources:
            w.writerow(
                {
                    "id": r.id,
                    "name": r.name,
                    "kind": r.kind,
                    "domain": r.domain or "",
                    "free": "" if r.free is None else str(r.free).lower(),
                    "license_name": r.license_name or "",
                    "license_url": r.license_url or "",
                    "publisher": r.publisher or "",
                    "homepage": r.homepage or "",
                    "formats": summarize_formats(r),
                }
            )


def write_ttl(resources: List[Resource], out_ttl: str, base_iri: str) -> None:
    os.makedirs(os.path.dirname(out_ttl), exist_ok=True)

    g = Graph()
    g.bind("dcat", DCAT)
    g.bind("dct", DCTERMS)
    g.bind("owl", OWL)
    g.bind("skos", SKOS)

    BASE = Namespace(base_iri.rstrip("/") + "/")
    FREE = URIRef(BASE["prop/free"])  # simple boolean property

    # A simple catalog node
    catalog = URIRef(BASE["catalog"])
    g.add((catalog, RDF.type, DCAT.Catalog))
    g.add((catalog, DCTERMS.title, Literal("Open Knowledge Graph Resources Catalog")))
    g.add(
        (
            catalog,
            DCTERMS.description,
            Literal(
                "A community-maintained catalog of open ontologies, taxonomies, vocabularies, and reference datasets."
            ),
        )
    )

    for r in resources:
        subj = URIRef(BASE[f"resource/{r.id}"])

        # Core typing
        if r.kind == "reference-dataset":
            g.add((subj, RDF.type, DCAT.Dataset))
        else:
            g.add((subj, RDF.type, DCAT.Resource))

        if r.kind == "ontology":
            g.add((subj, RDF.type, OWL.Ontology))
        if r.kind in {"taxonomy", "vocabulary"}:
            g.add((subj, RDF.type, SKOS.ConceptScheme))

        # Basic metadata
        g.add((subj, DCTERMS.identifier, Literal(r.id)))
        g.add((subj, DCTERMS.title, Literal(r.name)))

        if r.description:
            g.add((subj, DCTERMS.description, Literal(r.description)))

        if r.publisher:
            g.add((subj, DCTERMS.publisher, Literal(r.publisher)))

        if r.homepage:
            g.add((subj, DCAT.landingPage, URIRef(r.homepage)))

        if r.license_url:
            g.add((subj, DCTERMS.license, URIRef(r.license_url)))
        elif r.license_name:
            g.add((subj, DCTERMS.license, Literal(r.license_name)))

        if r.domain:
            g.add((subj, DCAT.theme, Literal(r.domain)))

        if r.free is not None:
            g.add((subj, FREE, Literal(bool(r.free), datatype=XSD.boolean)))

        # Link into the catalog
        g.add((catalog, DCAT.dataset, subj))

        # Distributions
        for i, d in enumerate(r.distributions, start=1):
            dist = URIRef(BASE[f"distribution/{r.id}/{i}"])
            g.add((dist, RDF.type, DCAT.Distribution))
            g.add((subj, DCAT.distribution, dist))

            if d.download_url:
                g.add((dist, DCAT.downloadURL, URIRef(d.download_url)))
            if d.media_type:
                g.add((dist, DCAT.mediaType, Literal(d.media_type)))
            if d.format:
                g.add((dist, DCTERMS.format, Literal(d.format)))

    g.serialize(destination=out_ttl, format="turtle")


def main():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    resources_dir = os.path.join(root, "resources")
    dist_dir = os.path.join(root, "dist")

    if not os.path.isdir(resources_dir):
        raise SystemExit(f"Expected a 'resources/' folder at: {resources_dir}")

    resources = load_resources(resources_dir)
    if not resources:
        raise SystemExit("No resources found under /resources (did you add any .yaml files?)")

    out_csv = os.path.join(dist_dir, "catalog.csv")
    out_ttl = os.path.join(dist_dir, "catalog.ttl")

    write_csv(resources, out_csv)

    # TODO: set this to your real repo or GitHub Pages URL if you later publish it
    write_ttl(resources, out_ttl, base_iri="https://example.org/open-knowledge-graph-resources")

    print(f"Built {len(resources)} resources")
    print(f"  - {out_csv}")
    print(f"  - {out_ttl}")


if __name__ == "__main__":
    main()

