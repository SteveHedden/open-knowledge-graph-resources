const FIELD_SEPARATOR = "\u001f";

function compareStrings(a, b) {
  return a < b ? -1 : a > b ? 1 : 0;
}

export function normalizeText(value) {
  return String(value ?? "")
    .replace(/\s+/g, " ")
    .trim();
}

function valuesFrom(value, objectKeys = ["title", "name", "label"]) {
  if (value == null) return [];
  if (Array.isArray(value)) return value.flatMap((entry) => valuesFrom(entry, objectKeys));
  if (typeof value === "object") {
    for (const key of objectKeys) {
      if (value[key] != null) return valuesFrom(value[key], objectKeys);
    }
    return [];
  }
  const normalized = normalizeText(value);
  return normalized ? [normalized] : [];
}

function stableValues(value, objectKeys) {
  return [...new Set(valuesFrom(value, objectKeys))].sort(compareStrings);
}

const SEMANTIC_FIELDS = [
  ["title", (item) => stableValues(item.title)],
  ["description", (item) => stableValues(item.description)],
  ["resource types", (item) => stableValues(item.types)],
  ["category", (item) => stableValues(item.category)],
  ["software type", (item) => stableValues(item.softwareType)],
  ["programming languages", (item) => stableValues(item.programmingLanguages)],
  ["licenses", (item) => stableValues(item.licenses)],
  ["creators", (item) => stableValues(item.creators, ["name", "title", "label"])],
  ["part of", (item) => stableValues(item.partOf, ["title", "name", "label"])],
  ["related resources", (item) =>
    stableValues(item.relatedTools ?? item.relatedResources, ["title", "name", "label"])],
];

/**
 * One deterministic projection is shared by embeddings and text fallback.
 * Identifiers and URLs deliberately stay out of prose; exactIdentifierValues
 * handles exact lookup for them instead.
 */
export function buildSemanticProjection(item) {
  const lines = [];
  for (const [label, read] of SEMANTIC_FIELDS) {
    const values = read(item);
    if (values.length) lines.push(`${label}: ${values.join(" | ")}`);
  }
  return lines.join("\n");
}

export function exactIdentifierValues(item) {
  return [
    item.wikidataId,
    item.canonicalUrl,
    item.homepage,
    item.sourceRepo,
    item.namespaceURI,
    item.namespaceUri,
    item.latestVersion,
    item.releaseDate,
  ]
    .flatMap((value) => valuesFrom(value))
    .map((value) => value.toLowerCase());
}

export function matchesTextQuery(item, query) {
  const normalizedQuery = normalizeText(query).toLowerCase();
  if (!normalizedQuery) return false;
  if (exactIdentifierValues(item).includes(normalizedQuery)) return true;

  const projection = buildSemanticProjection(item).toLowerCase();
  return normalizedQuery.split(/\s+/).every((term) => projection.includes(term));
}

export function qidFromItem(item) {
  const match = normalizeText(item.wikidataId).match(/(?:^|\/)(Q\d+)$/i);
  if (!match) throw new Error(`Missing canonical Wikidata QID: ${item.wikidataId || "unknown"}`);
  return match[1].toUpperCase();
}

export function vectorId(generationId, dataset, item) {
  const generation = normalizeText(generationId);
  const datasetName = normalizeText(dataset);
  if (!generation || !datasetName) throw new Error("generationId and dataset are required");
  if (new TextEncoder().encode(generation).length > 64) {
    throw new Error(`Vector namespace exceeds Cloudflare's 64-byte limit: ${generation}`);
  }
  const id = `${generation}:${datasetName}:${qidFromItem(item)}`;
  if (new TextEncoder().encode(id).length > 64) {
    throw new Error(`Vector ID exceeds Cloudflare's 64-byte limit: ${id}`);
  }
  return id;
}

function encodeList(value, objectKeys) {
  return stableValues(value, objectKeys).join(FIELD_SEPARATOR);
}

function decodeList(value) {
  if (!value) return [];
  if (Array.isArray(value)) return value.filter(Boolean);
  return String(value)
    .split(value.includes(FIELD_SEPARATOR) ? FIELD_SEPARATOR : ", ")
    .filter(Boolean);
}

export function vectorMetadata(item, dataset, generationId) {
  const metadata = {
    generationId,
    dataset,
    title: normalizeText(item.title),
    wikidataId: normalizeText(item.wikidataId),
  };

  const scalarFields = [
    "description",
    "category",
    "canonicalUrl",
    "homepage",
    "sourceRepo",
    "namespaceURI",
    "latestVersion",
    "releaseDate",
    "partOf",
    "softwareType",
  ];
  for (const field of scalarFields) {
    const value = normalizeText(item[field]);
    if (value) metadata[field] = value;
  }

  const listFields = ["types", "licenses", "programmingLanguages"];
  for (const field of listFields) {
    const value = encodeList(item[field]);
    if (value) metadata[field] = value;
  }

  return metadata;
}

export function formatVectorResult(match) {
  const metadata = match.metadata || {};
  const result = {
    score: match.score,
    title: metadata.title,
    wikidataId: metadata.wikidataId,
  };
  const scalarFields = [
    "description",
    "category",
    "canonicalUrl",
    "homepage",
    "sourceRepo",
    "namespaceURI",
    "latestVersion",
    "releaseDate",
    "partOf",
    "softwareType",
  ];
  for (const field of scalarFields) {
    if (metadata[field]) result[field] = metadata[field];
  }
  for (const field of ["types", "licenses", "programmingLanguages"]) {
    const values = decodeList(metadata[field]);
    if (values.length) result[field] = values;
  }
  return result;
}

export function buildVectorRecord(item, dataset, generationId, values) {
  return {
    id: vectorId(generationId, dataset, item),
    namespace: generationId,
    values,
    metadata: vectorMetadata(item, dataset, generationId),
  };
}
