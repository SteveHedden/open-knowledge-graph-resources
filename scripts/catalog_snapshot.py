#!/usr/bin/env python3
"""Build, verify, deploy, and point at transactional OKG catalog snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from rdflib import Graph
from rdflib.compare import to_isomorphic
from rdflib.namespace import OWL


ROOT_DIR = Path(__file__).resolve().parent.parent
MANIFEST_PATH = "data/manifest.json"
MANIFEST_SCHEMA_VERSION = "1.0.0"
GENERATION_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{12}$")
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

DEPLOYED_ROOT_FILES = ("ontology.ttl", "sources.ttl")
DEPLOYED_DIRECTORIES = ("data", "curation", "vocabularies", "site")
DIRECTORY_TREE_PATHS = ("site/resource", "site/software")
STAGING_SUPPORT_FILES = (
    "scripts/fetch_data.py",
    "scripts/category_classifier.py",
    "scripts/related_resources.py",
    "validation/known_records.json",
    "validation/catalog-manifest.schema.json",
)
CATALOG_JSON_PATHS = {"data/ontologies.json", "data/software.json"}
IGNORED_PARTS = {".DS_Store", ".wrangler"}

PINNED_PAGES = {
    "/resource/basic-formal-ontology/": "Q4866972",
    "/resource/schema-org-accessibility-properties-for-discoverability-vocabulary/": "Q124134540",
    "/resource/wikidata/": "Q2013",
    "/resource/open-knowledge-graphs/": "Q138639042",
    "/software/open-knowledge-graphs/": "Q138639042",
    "/software/rdflib/": "Q7276224",
}
SMOKE_CHECKSUM_PATHS = (
    "data/ontologies.ttl",
    "data/software.ttl",
    "data/ontologies.json",
    "data/software.json",
)


class SnapshotError(RuntimeError):
    """Raised when a catalog generation violates its publication contract."""


class SmokeCheckError(SnapshotError):
    """Raised when the live Pages site never exposes the expected generation."""


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_timestamp(value: str, field_name: str) -> datetime:
    if not TIMESTAMP_RE.fullmatch(value):
        raise SnapshotError(
            f"{field_name} must use UTC YYYY-MM-DDTHH:MM:SSZ form, found {value!r}."
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SnapshotError(f"{field_name} is not a valid timestamp: {value!r}.") from exc
    return parsed


def normalize_relative_path(value: str | Path | PurePosixPath) -> str:
    raw = str(value).replace("\\", "/")
    path = PurePosixPath(raw)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise SnapshotError(f"Artifact path must be repository-relative POSIX form: {value!r}")
    normalized = path.as_posix()
    if normalized != raw:
        raise SnapshotError(f"Artifact path is not canonical POSIX form: {value!r}")
    return normalized


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_artifact_digest(entries: Iterable[tuple[str, str]]) -> str:
    """Hash canonical path/NUL/digest/newline entries in lexical path order."""
    normalized: list[tuple[str, str]] = []
    seen: set[str] = set()
    for path_value, digest in entries:
        path = normalize_relative_path(path_value)
        if path in seen:
            raise SnapshotError(f"Artifact digest stream repeats path {path}.")
        if not SHA256_RE.fullmatch(digest):
            raise SnapshotError(f"Artifact {path} has invalid lowercase SHA-256 {digest!r}.")
        seen.add(path)
        normalized.append((path, digest))
    digest_stream = hashlib.sha256()
    for path, digest in sorted(normalized, key=lambda entry: entry[0]):
        digest_stream.update(path.encode("utf-8"))
        digest_stream.update(b"\0")
        digest_stream.update(digest.encode("ascii"))
        digest_stream.update(b"\n")
    return digest_stream.hexdigest()


def _ignored(relative_path: str) -> bool:
    return any(part in IGNORED_PARTS for part in PurePosixPath(relative_path).parts)


def _generated_detail_page(relative_path: str) -> bool:
    parts = PurePosixPath(relative_path).parts
    return (
        len(parts) == 4
        and parts[0] == "site"
        and parts[1] in {"resource", "software"}
        and parts[3] == "index.html"
    )


def deployed_files(root: Path, include_manifest: bool = True) -> list[str]:
    root = root.resolve()
    paths: list[str] = []
    for relative_path in DEPLOYED_ROOT_FILES:
        path = root / relative_path
        if path.is_symlink():
            raise SnapshotError(f"Deployed artifact may not be a symlink: {relative_path}")
        if path.is_file():
            paths.append(relative_path)
    for directory in DEPLOYED_DIRECTORIES:
        base = root / directory
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_symlink():
                relative = path.relative_to(root).as_posix()
                raise SnapshotError(f"Deployed artifact may not be a symlink: {relative}")
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if _ignored(relative):
                continue
            if not include_manifest and relative == MANIFEST_PATH:
                continue
            paths.append(relative)
    unique = sorted(set(paths))
    if (root / ".git").exists():
        completed = subprocess.run(
            ["git", "ls-files", "-z", *DEPLOYED_ROOT_FILES, *DEPLOYED_DIRECTORIES],
            cwd=root,
            check=True,
            capture_output=True,
        )
        tracked = {
            raw.decode("utf-8")
            for raw in completed.stdout.split(b"\0")
            if raw
        }
        unique = [
            relative
            for relative in unique
            if relative in tracked
            or relative == MANIFEST_PATH
            or _generated_detail_page(relative)
        ]
    return unique


def tree_file_entries(root: Path, tree_path: str) -> list[tuple[str, str]]:
    normalized_tree = normalize_relative_path(tree_path)
    prefix = normalized_tree + "/"
    entries = [
        (relative, sha256_file(root / relative))
        for relative in deployed_files(root, include_manifest=False)
        if relative.startswith(prefix)
    ]
    return sorted(entries, key=lambda entry: entry[0])


def tree_digest(root: Path, tree_path: str) -> tuple[str, int]:
    entries = tree_file_entries(root, tree_path)
    return canonical_artifact_digest(entries), len(entries)


def _is_in_tree(relative_path: str) -> bool:
    return any(
        relative_path == tree or relative_path.startswith(tree + "/")
        for tree in DIRECTORY_TREE_PATHS
    )


def artifact_coverage(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    files = deployed_files(root, include_manifest=False)
    artifacts = [
        {"path": relative, "sha256": sha256_file(root / relative)}
        for relative in files
        if not _is_in_tree(relative)
    ]
    directory_trees = []
    for relative in DIRECTORY_TREE_PATHS:
        digest, count = tree_digest(root, relative)
        directory_trees.append(
            {"path": relative, "sha256": digest, "fileCount": count}
        )
    return artifacts, directory_trees


def _json_payload(root: Path, relative_path: str) -> dict[str, Any]:
    payload = json.loads((root / relative_path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SnapshotError(f"{relative_path} must contain a JSON object.")
    return payload


def _graph_count(root: Path, relative_path: str) -> int:
    return len(Graph().parse(root / relative_path, format="turtle"))


def catalog_counts(root: Path) -> dict[str, Any]:
    ontology_payload = _json_payload(root, "data/ontologies.json")
    software_payload = _json_payload(root, "data/software.json")
    ontology_items = ontology_payload.get("items")
    software_items = software_payload.get("items")
    if not isinstance(ontology_items, list) or not isinstance(software_items, list):
        raise SnapshotError("Catalog JSON payloads must contain item arrays.")
    return {
        "records": {
            "resources": len(ontology_items),
            "software": len(software_items),
            "total": len(ontology_items) + len(software_items),
        },
        "triples": {
            "ontologies": _graph_count(root, "data/ontologies.ttl"),
            "software": _graph_count(root, "data/software.ttl"),
            "total": _graph_count(root, "data/ontologies.ttl")
            + _graph_count(root, "data/software.ttl"),
        },
    }


def _version(root: Path, relative_path: str) -> str:
    graph = Graph().parse(root / relative_path, format="turtle")
    values = sorted({str(value) for value in graph.objects(None, OWL.versionInfo)})
    if len(values) != 1:
        raise SnapshotError(
            f"{relative_path} must declare exactly one owl:versionInfo, found {len(values)}."
        )
    return values[0]


def catalog_versions(root: Path) -> dict[str, Any]:
    return {
        "ontology": _version(root, "ontology.ttl"),
        "vocabularies": {
            "categories": _version(root, "vocabularies/categories.ttl"),
            "softwareTypes": _version(root, "vocabularies/software-types.ttl"),
        },
    }


def generation_digest(
    artifacts: Sequence[dict[str, Any]],
    directory_trees: Sequence[dict[str, Any]],
) -> str:
    return canonical_artifact_digest(
        (str(entry["path"]), str(entry["sha256"]))
        for entry in [*artifacts, *directory_trees]
    )


def make_manifest(
    root: Path,
    started_at: str,
    source_retrieved_at: str,
    completed_at: str,
) -> dict[str, Any]:
    started = parse_timestamp(started_at, "startedAt")
    retrieved = parse_timestamp(source_retrieved_at, "sourceRetrievedAt")
    completed = parse_timestamp(completed_at, "completedAt")
    if not started <= retrieved <= completed:
        raise SnapshotError("Manifest timestamps must satisfy startedAt <= sourceRetrievedAt <= completedAt.")
    artifacts, directory_trees = artifact_coverage(root)
    digest = generation_digest(artifacts, directory_trees)
    generation_timestamp = completed.strftime("%Y%m%dT%H%M%SZ")
    return {
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "generationId": f"{generation_timestamp}-{digest[:12]}",
        "startedAt": started_at,
        "sourceRetrievedAt": source_retrieved_at,
        "completedAt": completed_at,
        "counts": catalog_counts(root),
        "versions": catalog_versions(root),
        "artifacts": artifacts,
        "directoryTrees": directory_trees,
    }


def write_manifest(
    root: Path,
    started_at: str,
    source_retrieved_at: str,
    completed_at: str | None = None,
) -> dict[str, Any]:
    destination = root / MANIFEST_PATH
    if destination.exists():
        destination.unlink()
    manifest = make_manifest(
        root,
        started_at=started_at,
        source_retrieved_at=source_retrieved_at,
        completed_at=completed_at or utc_now(),
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
    return manifest


def read_manifest(root: Path) -> dict[str, Any]:
    try:
        manifest = json.loads((root / MANIFEST_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"Could not read {MANIFEST_PATH}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise SnapshotError(f"{MANIFEST_PATH} must contain a JSON object.")
    return manifest


def validate_manifest_schema(root: Path, manifest: dict[str, Any]) -> None:
    schema_path = root / "validation/catalog-manifest.schema.json"
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, json.JSONDecodeError, SchemaError) as exc:
        raise SnapshotError(f"Manifest JSON Schema is unavailable or invalid: {exc}") from exc
    errors = sorted(
        Draft202012Validator(schema).iter_errors(manifest),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise SnapshotError(
            f"Manifest violates its JSON Schema at {location}: {error.message}"
        )


def verify_manifest(root: Path) -> dict[str, Any]:
    manifest = read_manifest(root)
    validate_manifest_schema(root, manifest)
    required_keys = {
        "schemaVersion",
        "generationId",
        "startedAt",
        "sourceRetrievedAt",
        "completedAt",
        "counts",
        "versions",
        "artifacts",
        "directoryTrees",
    }
    if set(manifest) != required_keys:
        raise SnapshotError(
            f"Manifest keys differ from the Task 21 contract: {sorted(set(manifest) ^ required_keys)}"
        )
    if manifest["schemaVersion"] != MANIFEST_SCHEMA_VERSION:
        raise SnapshotError(f"Unsupported manifest schemaVersion {manifest['schemaVersion']!r}.")
    generation_id = manifest.get("generationId")
    if not isinstance(generation_id, str) or not GENERATION_ID_RE.fullmatch(generation_id):
        raise SnapshotError(f"Invalid generationId {generation_id!r}.")
    expected = make_manifest(
        root,
        started_at=str(manifest["startedAt"]),
        source_retrieved_at=str(manifest["sourceRetrievedAt"]),
        completed_at=str(manifest["completedAt"]),
    )
    if manifest != expected:
        changed = [key for key in required_keys if manifest.get(key) != expected.get(key)]
        raise SnapshotError(f"Manifest verification failed for: {', '.join(sorted(changed))}.")

    covered: dict[str, int] = {}
    for entry in manifest["artifacts"]:
        covered[str(entry["path"])] = covered.get(str(entry["path"]), 0) + 1
    for tree in manifest["directoryTrees"]:
        prefix = str(tree["path"]) + "/"
        for relative in deployed_files(root, include_manifest=False):
            if relative.startswith(prefix):
                covered[relative] = covered.get(relative, 0) + 1
    actual = set(deployed_files(root, include_manifest=False))
    if set(covered) != actual or any(count != 1 for count in covered.values()):
        raise SnapshotError("Every deployed file must be covered exactly once by the manifest.")
    if MANIFEST_PATH in covered:
        raise SnapshotError("The manifest may not include itself in its digest coverage.")
    return manifest


def _git_tracked_deployed_files(repository: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "ls-files", "-z", *DEPLOYED_ROOT_FILES, *DEPLOYED_DIRECTORIES],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    result = []
    for raw in completed.stdout.split(b"\0"):
        if not raw:
            continue
        relative = raw.decode("utf-8")
        if not _ignored(relative):
            result.append(relative)
    return sorted(result)


def prepare_staging(repository: Path, destination: Path) -> None:
    repository = repository.resolve()
    destination = destination.resolve()
    if destination == repository or repository in destination.parents:
        raise SnapshotError("The staging directory must be outside the repository checkout.")
    if destination.exists() and any(destination.iterdir()):
        raise SnapshotError(f"Staging directory is not empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    for relative in _git_tracked_deployed_files(repository):
        source = repository / relative
        if not source.is_file():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    for relative in STAGING_SUPPORT_FILES:
        source = repository / relative
        if not source.is_file():
            raise SnapshotError(f"Required staging support file is missing: {relative}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    for relative in DIRECTORY_TREE_PATHS:
        (destination / relative).mkdir(parents=True, exist_ok=True)


def _rdf_fingerprint(path: Path) -> str:
    graph = Graph().parse(path, format="turtle")
    return str(to_isomorphic(graph).graph_digest())


def _sitemap_fingerprint(path: Path) -> str:
    root = ET.parse(path).getroot()
    locations = sorted(
        element.text.strip()
        for element in root.findall("{http://www.sitemaps.org/schemas/sitemap/0.9}url/{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
        if element.text and element.text.strip()
    )
    return sha256_bytes(json.dumps(locations, separators=(",", ":")).encode("utf-8"))


VOLATILE_PAGE_PATTERNS = (
    re.compile(rb'(data-generation-(?:timestamp|time)=["\'])[^"\']+(["\'])'),
)


def _page_fingerprint(path: Path) -> str:
    content = path.read_bytes()
    for pattern in VOLATILE_PAGE_PATTERNS:
        content = pattern.sub(rb"\1<VOLATILE>\2", content)
    return sha256_bytes(content)


def normalized_artifact_fingerprint(root: Path, relative: str) -> str:
    path = root / relative
    if relative.endswith(".ttl"):
        return "rdf:" + _rdf_fingerprint(path)
    if relative in CATALOG_JSON_PATHS:
        payload = _json_payload(root, relative)
        payload.pop("generatedAt", None)
        normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "json:" + sha256_bytes(normalized.encode("utf-8"))
    if relative.endswith(".json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "json:" + sha256_bytes(normalized.encode("utf-8"))
    if relative == "site/sitemap.xml":
        return "sitemap:" + _sitemap_fingerprint(path)
    if relative.startswith(("site/resource/", "site/software/")) and relative.endswith(".html"):
        return "page:" + _page_fingerprint(path)
    return "bytes:" + sha256_file(path)


def substantive_changes(candidate: Path, baseline: Path) -> list[str]:
    if not (baseline / MANIFEST_PATH).is_file():
        return [MANIFEST_PATH]
    candidate_files = set(deployed_files(candidate, include_manifest=False))
    baseline_files = set(deployed_files(baseline, include_manifest=False))
    changed = sorted(candidate_files ^ baseline_files)
    for relative in sorted(candidate_files & baseline_files):
        if normalized_artifact_fingerprint(candidate, relative) != normalized_artifact_fingerprint(
            baseline, relative
        ):
            changed.append(relative)
    return sorted(set(changed))


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def promote_candidate(candidate: Path, repository: Path) -> None:
    verify_manifest(candidate)
    candidate = candidate.resolve()
    repository = repository.resolve()
    if candidate == repository:
        raise SnapshotError("Candidate and repository roots must differ.")
    candidate_files = set(deployed_files(candidate, include_manifest=True))
    tracked_repository_files = set(_git_tracked_deployed_files(repository))
    for relative in sorted(tracked_repository_files - candidate_files):
        target = repository / relative
        if target.is_file() or target.is_symlink():
            target.unlink()
    for relative in sorted(candidate_files):
        _copy_file(candidate / relative, repository / relative)


def build_pages_artifact(root: Path, destination: Path) -> None:
    verify_manifest(root)
    root = root.resolve()
    destination = destination.resolve()
    if destination == root or root in destination.parents:
        raise SnapshotError("Pages output must be outside the catalog root.")
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    for relative in deployed_files(root, include_manifest=True):
        public_relative = relative[5:] if relative.startswith("site/") else relative
        _copy_file(root / relative, destination / public_relative)


def _url_with_attempt(base_url: str, public_path: str, generation: str, attempt: int) -> str:
    base = base_url.rstrip("/") + "/"
    path = public_path.lstrip("/")
    split = urlsplit(base + path)
    query = dict(parse_qsl(split.query, keep_blank_values=True))
    query.update({"generation": generation, "attempt": str(attempt)})
    return urlunsplit((split.scheme, split.netloc, split.path, urlencode(query), split.fragment))


FetchFunction = Callable[[str, float], tuple[int, bytes]]


def _http_fetch(url: str, timeout: float) -> tuple[int, bytes]:
    response = requests.get(url, timeout=timeout)
    return response.status_code, response.content


def _fetch_200(fetch: FetchFunction, url: str, timeout: float) -> bytes:
    status, content = fetch(url, timeout)
    if status != 200:
        raise SnapshotError(f"{url} returned HTTP {status}, expected 200.")
    return content


def check_live_once(
    base_url: str,
    expected_generation_id: str,
    attempt: int,
    fetch: FetchFunction = _http_fetch,
    request_timeout: float = 20.0,
) -> None:
    def get(public_path: str) -> bytes:
        return _fetch_200(
            fetch,
            _url_with_attempt(base_url, public_path, expected_generation_id, attempt),
            request_timeout,
        )

    manifest_bytes = get("/data/manifest.json")
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        raise SnapshotError("Live data/manifest.json is not valid JSON.") from exc
    if manifest.get("generationId") != expected_generation_id:
        raise SnapshotError(
            f"Live generation is {manifest.get('generationId')!r}, expected {expected_generation_id!r}."
        )
    checksums = {
        str(entry.get("path")): str(entry.get("sha256"))
        for entry in manifest.get("artifacts", [])
        if isinstance(entry, dict)
    }
    for relative in SMOKE_CHECKSUM_PATHS:
        expected_digest = checksums.get(relative)
        if expected_digest is None:
            raise SnapshotError(f"Live manifest does not individually cover {relative}.")
        actual_digest = sha256_bytes(get("/" + relative))
        if actual_digest != expected_digest:
            raise SnapshotError(
                f"Live checksum mismatch for {relative}: {actual_digest} != {expected_digest}."
            )
    get("/")
    sitemap = get("/sitemap.xml").decode("utf-8", errors="replace")
    for page_path, qid in PINNED_PAGES.items():
        canonical = "https://openknowledgegraphs.com" + page_path
        if canonical not in sitemap:
            raise SnapshotError(f"Live sitemap omits pinned URL {canonical}.")
        page = get(page_path).decode("utf-8", errors="replace")
        wikidata = f"https://www.wikidata.org/wiki/{qid}"
        if canonical not in page or wikidata not in page:
            raise SnapshotError(f"Pinned page {page_path} does not preserve identity {qid}.")


def live_smoke_check(
    base_url: str,
    expected_generation_id: str,
    fetch: FetchFunction = _http_fetch,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    retry_interval: float = 15.0,
    deadline_seconds: float = 300.0,
    request_timeout: float = 20.0,
) -> int:
    if not GENERATION_ID_RE.fullmatch(expected_generation_id):
        raise SnapshotError(f"Invalid expected generation ID {expected_generation_id!r}.")
    started = monotonic()
    attempt = 0
    last_error: Exception | None = None
    while True:
        attempt += 1
        try:
            check_live_once(
                base_url,
                expected_generation_id,
                attempt,
                fetch=fetch,
                request_timeout=request_timeout,
            )
            return attempt
        except (SnapshotError, requests.RequestException, OSError) as exc:
            last_error = exc
        elapsed = monotonic() - started
        if elapsed >= deadline_seconds:
            break
        sleep(min(retry_interval, deadline_seconds - elapsed))
    raise SmokeCheckError(
        f"Live verification failed after {attempt} attempts and {deadline_seconds:g}s: {last_error}"
    )


def _run_git(
    repository: Path,
    arguments: Sequence[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=check,
        capture_output=True,
        text=True,
    )


def _resolve_commit(repository: Path, ref: str) -> str:
    result = _run_git(repository, ["rev-parse", "--verify", f"{ref}^{{commit}}"])
    return result.stdout.strip()


def _optional_tag_target(repository: Path, tag: str) -> str | None:
    result = _run_git(
        repository,
        ["rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}"],
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def resolve_rollback_target(repository: Path, target: str) -> tuple[str, str]:
    generation_ref = f"catalog-generation/{target}" if GENERATION_ID_RE.fullmatch(target) else target
    commit = _resolve_commit(repository, generation_ref)
    try:
        manifest_bytes = _run_git(repository, ["show", f"{commit}:{MANIFEST_PATH}"]).stdout
        generation_id = json.loads(manifest_bytes)["generationId"]
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError) as exc:
        raise SnapshotError(f"Rollback target {target!r} has no valid generation manifest.") from exc
    if not isinstance(generation_id, str) or not GENERATION_ID_RE.fullmatch(generation_id):
        raise SnapshotError(f"Rollback target {target!r} has invalid generation ID {generation_id!r}.")
    return commit, generation_id


def advance_success_tags(repository: Path, generation_id: str, commit: str) -> None:
    if not GENERATION_ID_RE.fullmatch(generation_id):
        raise SnapshotError(f"Invalid generation ID {generation_id!r}.")
    commit = _resolve_commit(repository, commit)
    manifest_commit, manifest_generation_id = resolve_rollback_target(repository, commit)
    if manifest_commit != commit or manifest_generation_id != generation_id:
        raise SnapshotError(
            f"Commit {commit} contains generation {manifest_generation_id}, not {generation_id}."
        )
    immutable = f"catalog-generation/{generation_id}"
    if _optional_tag_target(repository, immutable) is not None:
        raise SnapshotError(f"Immutable successful-generation tag already exists: {immutable}")
    former_current = _optional_tag_target(repository, "catalog-current")
    _run_git(repository, ["tag", immutable, commit])
    _run_git(repository, ["tag", "-f", "catalog-current", commit])
    refspecs = [
        f"refs/tags/{immutable}:refs/tags/{immutable}",
        "+refs/tags/catalog-current:refs/tags/catalog-current",
    ]
    if former_current is not None:
        _run_git(repository, ["tag", "-f", "catalog-previous", former_current])
        refspecs.append("+refs/tags/catalog-previous:refs/tags/catalog-previous")
    _run_git(repository, ["push", "--atomic", "origin", *refspecs])


def advance_rollback_tags(repository: Path, verified_target: str) -> None:
    target_commit, generation_id = resolve_rollback_target(repository, verified_target)
    immutable = f"catalog-generation/{generation_id}"
    immutable_target = _optional_tag_target(repository, immutable)
    if immutable_target is not None and immutable_target != target_commit:
        raise SnapshotError(
            f"Immutable tag {immutable} points to {immutable_target}, not {target_commit}."
        )
    former_current = _optional_tag_target(repository, "catalog-current")
    refspecs: list[str] = []
    if immutable_target is None:
        _run_git(repository, ["tag", immutable, target_commit])
        refspecs.append(f"refs/tags/{immutable}:refs/tags/{immutable}")
    _run_git(repository, ["tag", "-f", "catalog-current", target_commit])
    refspecs.append("+refs/tags/catalog-current:refs/tags/catalog-current")
    if former_current is not None:
        _run_git(repository, ["tag", "-f", "catalog-previous", former_current])
        refspecs.append("+refs/tags/catalog-previous:refs/tags/catalog-previous")
    _run_git(repository, ["push", "--atomic", "origin", *refspecs])


def append_output(path: Path | None, **values: object) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            rendered = str(value).lower() if isinstance(value, bool) else str(value)
            handle.write(f"{key}={rendered}\n")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="Copy deployed tracked files to staging.")
    prepare.add_argument("--repository", type=Path, default=ROOT_DIR)
    prepare.add_argument("--destination", type=Path, required=True)

    changed = subparsers.add_parser("changed", help="Compare normalized staged and published output.")
    changed.add_argument("--candidate", type=Path, required=True)
    changed.add_argument("--baseline", type=Path, default=ROOT_DIR)
    changed.add_argument("--output-file", type=Path)

    finalize = subparsers.add_parser("finalize", help="Create the immutable generation manifest.")
    finalize.add_argument("--root", type=Path, required=True)
    finalize.add_argument("--started-at", required=True)
    finalize.add_argument("--source-retrieved-at", required=True)
    finalize.add_argument("--completed-at")
    finalize.add_argument("--output-file", type=Path)

    verify = subparsers.add_parser("verify", help="Verify the manifest and complete coverage.")
    verify.add_argument("--root", type=Path, default=ROOT_DIR)
    verify.add_argument("--output-file", type=Path)

    promote = subparsers.add_parser("promote", help="Replace repository output with a candidate.")
    promote.add_argument("--candidate", type=Path, required=True)
    promote.add_argument("--repository", type=Path, default=ROOT_DIR)

    pages = subparsers.add_parser("build-pages", help="Build Pages files from a verified generation.")
    pages.add_argument("--root", type=Path, default=ROOT_DIR)
    pages.add_argument("--destination", type=Path, required=True)

    smoke = subparsers.add_parser("smoke", help="Verify a deployed generation over HTTP.")
    smoke.add_argument("--base-url", required=True)
    smoke.add_argument("--generation-id", required=True)
    smoke.add_argument("--output-file", type=Path)

    resolve = subparsers.add_parser("resolve-rollback", help="Resolve a generation ID or Git ref.")
    resolve.add_argument("--repository", type=Path, default=ROOT_DIR)
    resolve.add_argument("--target", required=True)
    resolve.add_argument("--output-file", type=Path)

    success = subparsers.add_parser("advance-success", help="Advance tags after live verification.")
    success.add_argument("--repository", type=Path, default=ROOT_DIR)
    success.add_argument("--generation-id", required=True)
    success.add_argument("--commit", required=True)

    rollback = subparsers.add_parser("advance-rollback", help="Advance moving tags after rollback.")
    rollback.add_argument("--repository", type=Path, default=ROOT_DIR)
    rollback.add_argument("--target", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "prepare":
            prepare_staging(args.repository, args.destination)
        elif args.command == "changed":
            paths = substantive_changes(args.candidate, args.baseline)
            append_output(args.output_file, changed=bool(paths))
            print(json.dumps({"changed": bool(paths), "paths": paths}, indent=2))
        elif args.command == "finalize":
            manifest = write_manifest(
                args.root,
                args.started_at,
                args.source_retrieved_at,
                args.completed_at,
            )
            append_output(args.output_file, generation_id=manifest["generationId"])
            print(manifest["generationId"])
        elif args.command == "verify":
            manifest = verify_manifest(args.root)
            append_output(args.output_file, generation_id=manifest["generationId"])
            print(f"Verified generation {manifest['generationId']}")
        elif args.command == "promote":
            promote_candidate(args.candidate, args.repository)
        elif args.command == "build-pages":
            build_pages_artifact(args.root, args.destination)
        elif args.command == "smoke":
            attempts = live_smoke_check(args.base_url, args.generation_id)
            append_output(args.output_file, live_verified=True, attempts=attempts)
            print(f"Live generation {args.generation_id} verified after {attempts} attempt(s).")
        elif args.command == "resolve-rollback":
            commit, generation_id = resolve_rollback_target(args.repository, args.target)
            append_output(args.output_file, commit=commit, generation_id=generation_id)
            print(f"{commit} {generation_id}")
        elif args.command == "advance-success":
            advance_success_tags(args.repository, args.generation_id, args.commit)
        elif args.command == "advance-rollback":
            advance_rollback_tags(args.repository, args.target)
        else:  # pragma: no cover - argparse prevents this
            raise SnapshotError(f"Unknown command {args.command!r}.")
    except (SnapshotError, OSError, subprocess.CalledProcessError) as exc:
        print(f"catalog snapshot failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
