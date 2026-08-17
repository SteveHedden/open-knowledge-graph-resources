(function () {
  "use strict";

  const DATA_PATHS = {
    ontologies: ["./data/ontologies.json", "../data/ontologies.json"],
    software: ["./data/software.json", "../data/software.json"],
    controlledVocabularies: [
      "./data/controlled_vocabularies.json",
      "../data/controlled_vocabularies.json",
    ],
    pageQids: ["./data/page_qids.json", "../data/page_qids.json"],
    manifest: ["./data/manifest.json", "../data/manifest.json"],
  };

  const DEFAULT_STATE = {
    tab: "ontologies",
    q: "",
    category: "all",
    softwareType: "all",
    page: 1,
  };

  let CATEGORY_OPTIONS = [{ id: "all", label: "All" }];
  let CATEGORY_IDS = new Set(["all"]);
  let CATEGORY_ID_TO_LABEL = new Map([["all", "All"]]);

  let SOFTWARE_TYPE_OPTIONS = [{ id: "all", label: "All" }];
  let SOFTWARE_TYPE_IDS = new Set(["all"]);
  let SOFTWARE_TYPE_ID_TO_LABEL = new Map([["all", "All"]]);

  const TAB_DEFAULT_SORT = {
    ontologies: { sort: "documentationScore", order: "desc" },
    software: { sort: "releaseDate", order: "desc" },
  };

  const SORT_FIELDS = {
    ontologies: new Set(["title", "types", "licenses", "partOf", "documentationScore"]),
    software: new Set(["title", "licenses", "latestVersion", "releaseDate"]),
  };

  const TABLE_BODY_IDS = {
    ontologies: "ontologies-table-body",
    software: "software-table-body",
  };

  const CARD_CONTAINER_IDS = {
    ontologies: "ontologies-cards",
    software: "software-cards",
  };

  const PANEL_IDS = {
    ontologies: "panel-ontologies",
    software: "panel-software",
  };

  const TAB_IDS = {
    ontologies: "tab-ontologies",
    software: "tab-software",
  };

  const TAB_ORDER = ["ontologies", "software"];
  const PAGE_SIZE = 50;
  const RESPONSIVE_QUERY = "(max-width: 760px)";
  const SEARCH_DEBOUNCE_MS = 180;
  const MAX_TRACKED_QUERY_LENGTH = 64;

  const dom = {
    searchInput: document.getElementById("catalog-search"),
    resultsMeta: document.getElementById("results-meta"),
    pagination: document.getElementById("catalog-pagination"),
    previousPage: document.getElementById("previous-page"),
    nextPage: document.getElementById("next-page"),
    pageStatus: document.getElementById("page-status"),
    freshness: document.getElementById("catalog-freshness"),
    generationId: document.getElementById("generation-id"),
    lastUpdated: document.getElementById("last-updated"),
    tabs: Array.from(document.querySelectorAll('[role="tab"]')),
    panels: Array.from(document.querySelectorAll('[role="tabpanel"]')),
    sortButtons: Array.from(document.querySelectorAll(".sort-button")),
    categoryFilters: document.getElementById("ontology-category-filters"),
    categoryButtons: Array.from(
      document.querySelectorAll("#ontology-category-filters .category-pill")
    ),
    softwareTypeFilters: document.getElementById("software-type-filters"),
    softwareTypeButtons: Array.from(
      document.querySelectorAll("#software-type-filters .category-pill")
    ),
    ontologiesTtlLink: document.querySelector('a[href$="ontologies.ttl"]'),
    softwareTtlLink: document.querySelector('a[href$="software.ttl"]'),
  };

  const store = {
    ontologies: [],
    software: [],
    loadStatus: { ontologies: "loading", software: "loading" },
    pageSlugs: { resource: {}, software: {} },
    manifest: null,
  };

  const responsiveMedia =
    typeof window.matchMedia === "function"
      ? window.matchMedia(RESPONSIVE_QUERY)
      : { matches: false };

  function normalizeVocabularyOptions(rawEntries) {
    const options = [{ id: "all", label: "All" }];
    const seenIds = new Set(["all"]);
    const seenLabels = new Set(["All"]);
    if (!Array.isArray(rawEntries)) {
      return options;
    }
    rawEntries.forEach((entry) => {
      const id = typeof entry?.id === "string" ? entry.id.trim() : "";
      const label = typeof entry?.label === "string" ? entry.label.trim() : "";
      if (!id || !label || seenIds.has(id) || seenLabels.has(label)) {
        return;
      }
      seenIds.add(id);
      seenLabels.add(label);
      options.push({ id, label });
    });
    return options;
  }

  function renderVocabularyButtons(container, options, dataAttribute) {
    const list = container?.querySelector(".category-pill-list");
    if (!list) {
      return [];
    }
    list.replaceChildren();
    options.forEach((entry) => {
      const button = document.createElement("button");
      button.className = `category-pill${entry.id === "all" ? " is-active" : ""}`;
      button.type = "button";
      button.setAttribute("aria-pressed", entry.id === "all" ? "true" : "false");
      button.dataset[dataAttribute] = entry.id;
      button.textContent = entry.label;
      list.appendChild(button);
    });
    return Array.from(list.querySelectorAll(".category-pill"));
  }

  function configureControlledVocabularies(payload) {
    CATEGORY_OPTIONS = normalizeVocabularyOptions(payload?.categories);
    SOFTWARE_TYPE_OPTIONS = normalizeVocabularyOptions(payload?.softwareTypes);

    CATEGORY_IDS = new Set(CATEGORY_OPTIONS.map((entry) => entry.id));
    CATEGORY_ID_TO_LABEL = new Map(CATEGORY_OPTIONS.map((entry) => [entry.id, entry.label]));
    SOFTWARE_TYPE_IDS = new Set(SOFTWARE_TYPE_OPTIONS.map((entry) => entry.id));
    SOFTWARE_TYPE_ID_TO_LABEL = new Map(
      SOFTWARE_TYPE_OPTIONS.map((entry) => [entry.id, entry.label])
    );

    dom.categoryButtons = renderVocabularyButtons(
      dom.categoryFilters,
      CATEGORY_OPTIONS,
      "category"
    );
    dom.softwareTypeButtons = renderVocabularyButtons(
      dom.softwareTypeFilters,
      SOFTWARE_TYPE_OPTIONS,
      "softwareType"
    );
  }

  function setFilterAvailability(available) {
    [dom.categoryFilters, dom.softwareTypeFilters].forEach((container) => {
      if (!container) {
        return;
      }
      container.classList.toggle("is-disabled", !available);
      container.setAttribute("aria-disabled", available ? "false" : "true");
      container.title = available ? "" : "Filters are unavailable for this catalog generation.";
    });
    [...dom.categoryButtons, ...dom.softwareTypeButtons].forEach((button) => {
      button.disabled = !available;
    });
  }

  let state = normalizeState(parseStateFromUrl());
  let lastTrackedSearchSignature = "";

  function debounce(fn, waitMs) {
    let timeoutId;
    return (...args) => {
      window.clearTimeout(timeoutId);
      timeoutId = window.setTimeout(() => fn(...args), waitMs);
    };
  }

  function trackAnalyticsEvent(eventName, payload = {}) {
    if (
      !window.umami ||
      typeof window.umami.track !== "function" ||
      typeof eventName !== "string" ||
      !eventName
    ) {
      return;
    }

    try {
      window.umami.track(eventName, payload);
    } catch (error) {
      console.warn("Analytics event failed", error);
    }
  }

  function sanitizeSearchQuery(rawValue) {
    if (typeof rawValue !== "string") {
      return "";
    }
    return rawValue
      .trim()
      .toLowerCase()
      .replace(/\s+/g, " ")
      .replace(/[^a-z0-9 _-]/g, "")
      .slice(0, MAX_TRACKED_QUERY_LENGTH);
  }

  function isPotentiallySensitiveQuery(rawValue) {
    if (typeof rawValue !== "string") {
      return false;
    }
    const value = rawValue.trim().toLowerCase();
    if (!value) {
      return false;
    }
    const emailPattern = /\b[^\s@]+@[^\s@]+\.[^\s@]+\b/;
    const urlPattern = /\bhttps?:\/\/|\bwww\./;
    return emailPattern.test(value) || urlPattern.test(value);
  }

  function trackSearchQuery(rawValue) {
    const sanitized = sanitizeSearchQuery(rawValue);
    if (!sanitized) {
      lastTrackedSearchSignature = "";
      return;
    }

    const safeQuery = isPotentiallySensitiveQuery(rawValue) ? "[redacted]" : sanitized;
    const signature = `${state.tab}|${safeQuery}`;
    if (signature === lastTrackedSearchSignature) {
      return;
    }

    lastTrackedSearchSignature = signature;
    trackAnalyticsEvent("search_query", {
      tab: state.tab,
      query: safeQuery,
      queryLength: sanitized.length,
    });
  }

  function extractHost(value) {
    if (typeof value !== "string" || !value) {
      return "";
    }
    try {
      return new URL(value, window.location.href).hostname;
    } catch (error) {
      return "";
    }
  }

  function isValidTab(tab) {
    return tab === "ontologies" || tab === "software";
  }

  function isValidOrder(order) {
    return order === "asc" || order === "desc";
  }

  function isSortAllowed(tab, sort) {
    const allowed = SORT_FIELDS[tab];
    return allowed ? allowed.has(sort) : false;
  }

  function isValidCategoryId(categoryId) {
    return typeof categoryId === "string" && CATEGORY_IDS.has(categoryId);
  }

  function isValidSoftwareTypeId(softwareTypeId) {
    return typeof softwareTypeId === "string" && SOFTWARE_TYPE_IDS.has(softwareTypeId);
  }

  function parseStateFromUrl() {
    const params = new URLSearchParams(window.location.search);
    return {
      tab: params.get("tab"),
      q: params.get("q"),
      category: params.get("category"),
      softwareType: params.get("softwareType"),
      sort: params.get("sort"),
      order: params.get("order"),
      page: params.get("page"),
    };
  }

  function normalizeState(rawState) {
    const normalizedTab = isValidTab(rawState.tab) ? rawState.tab : DEFAULT_STATE.tab;
    const tabDefaults = TAB_DEFAULT_SORT[normalizedTab];
    const requestedSort = typeof rawState.sort === "string" ? rawState.sort : "";
    const requestedOrder = typeof rawState.order === "string" ? rawState.order : "";
    const requestedPage = Number.parseInt(String(rawState.page || ""), 10);

    const normalized = {
      tab: normalizedTab,
      q: String(rawState.q || ""),
      category: isValidCategoryId(rawState.category)
        ? rawState.category
        : DEFAULT_STATE.category,
      softwareType: isValidSoftwareTypeId(rawState.softwareType)
        ? rawState.softwareType
        : DEFAULT_STATE.softwareType,
      sort: requestedSort,
      order: requestedOrder,
      page: Number.isInteger(requestedPage) && requestedPage > 0 ? requestedPage : 1,
    };

    if (!normalized.sort || !isSortAllowed(normalized.tab, normalized.sort)) {
      normalized.sort = tabDefaults.sort;
      normalized.order = tabDefaults.order;
    } else if (!isValidOrder(normalized.order)) {
      normalized.order = "asc";
    }

    return normalized;
  }

  function updateUrlFromState(historyAction = "replace") {
    const params = new URLSearchParams();
    params.set("tab", state.tab);
    params.set("q", state.q);
    params.set("category", state.category);
    params.set("softwareType", state.softwareType);
    params.set("sort", state.sort);
    params.set("order", state.order);
    params.set("page", String(state.page));

    const nextQuery = params.toString();
    const nextUrl = nextQuery
      ? `${window.location.pathname}?${nextQuery}`
      : window.location.pathname;
    const method = historyAction === "push" ? "pushState" : "replaceState";
    window.history[method]({}, "", nextUrl);
  }

  async function fetchJsonWithFallback(paths) {
    let lastError = null;

    for (const path of paths) {
      try {
        const response = await window.fetch(path, {
          headers: { Accept: "application/json" },
        });
        if (!response.ok) {
          lastError = new Error(`Request failed (${response.status}) for ${path}`);
          continue;
        }
        return { path, payload: await response.json() };
      } catch (error) {
        lastError = error;
      }
    }

    throw lastError || new Error("Unable to fetch JSON payload");
  }

  function updateTtlLinksFromJsonPath(jsonPath) {
    const fromSiteFolder = jsonPath.startsWith("../");
    const prefix = fromSiteFolder ? "../data/" : "./data/";
    if (dom.ontologiesTtlLink) {
      dom.ontologiesTtlLink.setAttribute("href", `${prefix}ontologies.ttl`);
    }
    if (dom.softwareTtlLink) {
      dom.softwareTtlLink.setAttribute("href", `${prefix}software.ttl`);
    }
  }

  function normalizeItem(item) {
    const safeItem = { ...item };
    safeItem.types = Array.isArray(item.types) ? item.types : [];
    safeItem.licenses = Array.isArray(item.licenses) ? item.licenses : [];
    safeItem.programmingLanguages = Array.isArray(item.programmingLanguages)
      ? item.programmingLanguages
      : [];
    safeItem.creators = Array.isArray(item.creators) ? item.creators : [];
    safeItem.relatedTools = Array.isArray(item.relatedTools) ? item.relatedTools : [];
    safeItem.category = typeof item.category === "string" ? item.category.trim() : "";
    safeItem.softwareType =
      typeof item.softwareType === "string" ? item.softwareType.trim() : "";
    safeItem._searchText = buildSearchText(safeItem);
    return safeItem;
  }

  function getDetailPageUrl(item, tab) {
    const dataset = tab === "software" ? "software" : "resource";
    const qid = (item.wikidataId || "").split("/").pop();
    const slug = qid && store.pageSlugs[dataset][qid];
    if (!slug) return null;
    return `./${dataset}/${slug}/`;
  }

  function buildSearchText(item) {
    const creatorValues = item.creators.flatMap((creator) =>
      creator && typeof creator === "object"
        ? [creator.name, creator.wikidataId]
        : [creator]
    );
    const relatedValues = item.relatedTools.flatMap((related) =>
      related && typeof related === "object"
        ? [related.title, related.canonicalUrl]
        : [related]
    );
    const parts = [
      item.title,
      item.description,
      item.wikidataId,
      item.canonicalUrl,
      item.homepage,
      item.sourceRepo,
      item.namespaceURI,
      item.partOf,
      item.category,
      item.softwareType,
      item.latestVersion,
      item.releaseDate,
      ...(Array.isArray(item.types) ? item.types : []),
      ...(Array.isArray(item.licenses) ? item.licenses : []),
      ...item.programmingLanguages,
      ...creatorValues,
      ...relatedValues,
    ];
    return parts
      .filter((value) => typeof value === "string" && value.trim())
      .join(" ")
      .toLowerCase();
  }

  function formatDate(dateInput) {
    if (!dateInput) {
      return "";
    }
    const parsed = new Date(dateInput);
    if (Number.isNaN(parsed.getTime())) {
      return String(dateInput);
    }
    return parsed.toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  }

  function formatDateTime(dateInput) {
    const parsed = new Date(dateInput);
    if (Number.isNaN(parsed.getTime())) {
      return String(dateInput);
    }
    return parsed.toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function setFreshnessMetadata(manifest) {
    const generationId = typeof manifest?.generationId === "string" ? manifest.generationId : "";
    const sourceRetrievedAt =
      typeof manifest?.sourceRetrievedAt === "string" ? manifest.sourceRetrievedAt : "";
    if (!generationId || !sourceRetrievedAt || Number.isNaN(Date.parse(sourceRetrievedAt))) {
      store.manifest = null;
      if (dom.freshness) {
        dom.freshness.hidden = true;
      }
      return;
    }
    store.manifest = { generationId, sourceRetrievedAt };
    if (dom.generationId) {
      dom.generationId.textContent = generationId;
    }
    if (dom.lastUpdated) {
      dom.lastUpdated.textContent = formatDateTime(sourceRetrievedAt);
      dom.lastUpdated.setAttribute("datetime", sourceRetrievedAt);
    }
    if (dom.freshness) {
      dom.freshness.hidden = false;
    }
  }

  function itemSortValue(item, key) {
    if (key === "documentationScore") {
      const hasHomepage = item.homepage ? 2 : 0;
      const hasSourceRepo = item.sourceRepo ? 2 : 0;
      const hasLicense = Array.isArray(item.licenses) && item.licenses.length ? 1 : 0;
      return hasHomepage + hasSourceRepo + hasLicense;
    }
    if (key === "types") {
      return Array.isArray(item.types) && item.types.length ? item.types.join(", ") : "";
    }
    if (key === "licenses") {
      return Array.isArray(item.licenses) && item.licenses.length ? item.licenses.join(", ") : "";
    }
    return item[key] || "";
  }

  function isMissingSortValue(value) {
    if (Array.isArray(value)) {
      return value.length === 0;
    }
    return value === null || value === undefined || String(value).trim() === "";
  }

  function compareValues(aValue, bValue, key) {
    if (key === "documentationScore") {
      return Number(aValue) - Number(bValue);
    }
    if (key === "releaseDate") {
      const aTime = Date.parse(String(aValue));
      const bTime = Date.parse(String(bValue));
      return aTime - bTime;
    }

    return String(aValue).localeCompare(String(bValue), undefined, {
      numeric: true,
      sensitivity: "base",
    });
  }

  function sortItems(items) {
    const sorted = [...items];
    const { sort, order } = state;

    sorted.sort((a, b) => {
      const aValue = itemSortValue(a, sort);
      const bValue = itemSortValue(b, sort);
      const aMissing = isMissingSortValue(aValue);
      const bMissing = isMissingSortValue(bValue);

      if (aMissing && bMissing) {
        return String(a.title).localeCompare(String(b.title), undefined, {
          numeric: true,
          sensitivity: "base",
        });
      }
      if (aMissing) {
        return 1;
      }
      if (bMissing) {
        return -1;
      }

      const compared = compareValues(aValue, bValue, sort);
      if (compared === 0) {
        return String(a.title).localeCompare(String(b.title), undefined, {
          numeric: true,
          sensitivity: "base",
        });
      }
      return order === "desc" ? -compared : compared;
    });

    return sorted;
  }

  function filterItems(items) {
    const categoryFiltered =
      state.tab === "ontologies" && state.category !== DEFAULT_STATE.category
        ? items.filter((item) => item.category === CATEGORY_ID_TO_LABEL.get(state.category))
        : items;

    const softwareTypeFiltered =
      state.tab === "software" && state.softwareType !== DEFAULT_STATE.softwareType
        ? categoryFiltered.filter(
            (item) => item.softwareType === SOFTWARE_TYPE_ID_TO_LABEL.get(state.softwareType)
          )
        : categoryFiltered;

    if (!state.q) {
      return softwareTypeFiltered;
    }
    const tokens = state.q.toLowerCase().split(/\s+/).filter(Boolean);
    if (!tokens.length) {
      return softwareTypeFiltered;
    }
    return softwareTypeFiltered.filter((item) =>
      tokens.every((token) => item._searchText.includes(token))
    );
  }

  function getActiveItems() {
    const active = store[state.tab] || [];
    return sortItems(filterItems(active));
  }

  function paginateItems(items) {
    const totalPages = Math.max(1, Math.ceil(items.length / PAGE_SIZE));
    const page = Math.min(state.page, totalPages);
    if (page !== state.page) {
      state.page = page;
      updateUrlFromState("replace");
    }
    const start = (page - 1) * PAGE_SIZE;
    return {
      items: items.slice(start, start + PAGE_SIZE),
      page,
      totalPages,
      start,
    };
  }

  function isCardView() {
    return Boolean(responsiveMedia.matches);
  }

  function hasActiveCategoryFilter() {
    return state.tab === "ontologies" && state.category !== DEFAULT_STATE.category;
  }

  function hasActiveSoftwareTypeFilter() {
    return state.tab === "software" && state.softwareType !== DEFAULT_STATE.softwareType;
  }

  function clearChildren(node) {
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function createLink(href, text, analyticsData) {
    const link = document.createElement("a");
    link.href = href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = text;
    if (analyticsData && typeof analyticsData === "object") {
      link.dataset.trackOutbound = "true";
      if (typeof analyticsData.linkType === "string" && analyticsData.linkType) {
        link.dataset.linkType = analyticsData.linkType;
      }
      if (
        typeof analyticsData.resourceTitle === "string" &&
        analyticsData.resourceTitle
      ) {
        link.dataset.resourceTitle = analyticsData.resourceTitle;
      }
      if (typeof analyticsData.tab === "string" && analyticsData.tab) {
        link.dataset.tab = analyticsData.tab;
      }
    }
    return link;
  }

  function renderNoResultsTableRow(columnCount, message) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.className = "placeholder-cell";
    cell.colSpan = columnCount;
    cell.textContent = message;
    row.appendChild(cell);
    return row;
  }

  function renderOntologyRow(item) {
    const row = document.createElement("tr");
    row.dataset.record = "true";

    const titleCell = document.createElement("td");
    const detailUrl = getDetailPageUrl(item, "ontologies");
    if (detailUrl) {
      const link = document.createElement("a");
      link.href = detailUrl;
      link.textContent = item.title;
      titleCell.appendChild(link);
    } else {
      titleCell.textContent = item.title;
    }
    row.appendChild(titleCell);

    const descriptionCell = document.createElement("td");
    descriptionCell.className = "description-cell";
    descriptionCell.textContent = item.description || "—";
    row.appendChild(descriptionCell);

    const typeCell = document.createElement("td");
    typeCell.textContent = item.types.join(", ");
    row.appendChild(typeCell);

    const licenseCell = document.createElement("td");
    licenseCell.textContent = item.licenses.length ? item.licenses.join(", ") : "—";
    row.appendChild(licenseCell);

    const partOfCell = document.createElement("td");
    partOfCell.textContent = item.partOf || "—";
    row.appendChild(partOfCell);

    const linksCell = document.createElement("td");
    linksCell.className = "link-cell";
    linksCell.appendChild(
      createLink(item.wikidataId, "Wikidata", {
        linkType: "wikidata",
        resourceTitle: item.title,
        tab: "ontologies",
      })
    );
    if (item.homepage) {
      const separator = document.createElement("span");
      separator.textContent = " | ";
      linksCell.appendChild(separator);
      linksCell.appendChild(
        createLink(item.homepage, "Website", {
          linkType: "homepage",
          resourceTitle: item.title,
          tab: "ontologies",
        })
      );
    }
    if (item.sourceRepo) {
      const separator = document.createElement("span");
      separator.textContent = " | ";
      linksCell.appendChild(separator);
      linksCell.appendChild(
        createLink(item.sourceRepo, "Source", {
          linkType: "source_repo",
          resourceTitle: item.title,
          tab: "ontologies",
        })
      );
    }
    row.appendChild(linksCell);

    return row;
  }

  function renderSoftwareRow(item) {
    const row = document.createElement("tr");
    row.dataset.record = "true";

    const titleCell = document.createElement("td");
    const detailUrl = getDetailPageUrl(item, "software");
    if (detailUrl) {
      const link = document.createElement("a");
      link.href = detailUrl;
      link.textContent = item.title;
      titleCell.appendChild(link);
    } else {
      titleCell.textContent = item.title;
    }
    row.appendChild(titleCell);

    const descriptionCell = document.createElement("td");
    descriptionCell.className = "description-cell";
    descriptionCell.textContent = item.description || "—";
    row.appendChild(descriptionCell);

    const licenseCell = document.createElement("td");
    licenseCell.textContent = item.licenses.length ? item.licenses.join(", ") : "—";
    row.appendChild(licenseCell);

    const versionCell = document.createElement("td");
    versionCell.textContent = item.latestVersion || "—";
    row.appendChild(versionCell);

    const dateCell = document.createElement("td");
    dateCell.textContent = item.releaseDate ? formatDate(item.releaseDate) : "—";
    row.appendChild(dateCell);

    const linksCell = document.createElement("td");
    linksCell.className = "link-cell";
    linksCell.appendChild(
      createLink(item.wikidataId, "Wikidata", {
        linkType: "wikidata",
        resourceTitle: item.title,
        tab: "software",
      })
    );
    if (item.homepage) {
      const separator = document.createElement("span");
      separator.textContent = " | ";
      linksCell.appendChild(separator);
      linksCell.appendChild(
        createLink(item.homepage, "Website", {
          linkType: "homepage",
          resourceTitle: item.title,
          tab: "software",
        })
      );
    }
    if (item.sourceRepo) {
      const separator = document.createElement("span");
      separator.textContent = " | ";
      linksCell.appendChild(separator);
      linksCell.appendChild(
        createLink(item.sourceRepo, "Source", {
          linkType: "source_repo",
          resourceTitle: item.title,
          tab: "software",
        })
      );
    }
    row.appendChild(linksCell);

    return row;
  }

  function appendCardMetaLine(card, label, value) {
    if (!value) {
      return;
    }
    const line = document.createElement("p");
    line.className = "card-row";
    const strong = document.createElement("strong");
    strong.textContent = `${label}: `;
    line.appendChild(strong);
    line.appendChild(document.createTextNode(value));
    card.appendChild(line);
  }

  function appendCardDescription(card, value) {
    if (!value) {
      return;
    }
    const description = document.createElement("p");
    description.className = "card-description";
    description.textContent = value;
    card.appendChild(description);
  }

  function renderOntologyCard(item) {
    const card = document.createElement("article");
    card.className = "card";
    card.dataset.record = "true";

    const title = document.createElement("h3");
    const detailUrl = getDetailPageUrl(item, "ontologies");
    if (detailUrl) {
      const link = document.createElement("a");
      link.href = detailUrl;
      link.textContent = item.title;
      title.appendChild(link);
    } else {
      title.textContent = item.title;
    }
    card.appendChild(title);
    appendCardDescription(card, item.description || "");

    appendCardMetaLine(card, "Type", item.types.join(", "));
    appendCardMetaLine(
      card,
      "License",
      item.licenses.length ? item.licenses.join(", ") : ""
    );
    appendCardMetaLine(card, "Part Of", item.partOf || "");

    const links = document.createElement("p");
    links.className = "card-links";
    links.appendChild(
      createLink(item.wikidataId, "Wikidata", {
        linkType: "wikidata",
        resourceTitle: item.title,
        tab: "ontologies",
      })
    );
    if (item.homepage) {
      links.appendChild(document.createTextNode(" | "));
      links.appendChild(
        createLink(item.homepage, "Website", {
          linkType: "homepage",
          resourceTitle: item.title,
          tab: "ontologies",
        })
      );
    }
    if (item.sourceRepo) {
      links.appendChild(document.createTextNode(" | "));
      links.appendChild(
        createLink(item.sourceRepo, "Source", {
          linkType: "source_repo",
          resourceTitle: item.title,
          tab: "ontologies",
        })
      );
    }
    card.appendChild(links);

    return card;
  }

  function renderSoftwareCard(item) {
    const card = document.createElement("article");
    card.className = "card";
    card.dataset.record = "true";

    const title = document.createElement("h3");
    const detailUrl = getDetailPageUrl(item, "software");
    if (detailUrl) {
      const link = document.createElement("a");
      link.href = detailUrl;
      link.textContent = item.title;
      title.appendChild(link);
    } else {
      title.textContent = item.title;
    }
    card.appendChild(title);
    appendCardDescription(card, item.description || "");

    appendCardMetaLine(
      card,
      "License",
      item.licenses.length ? item.licenses.join(", ") : ""
    );
    appendCardMetaLine(card, "Version", item.latestVersion || "");
    appendCardMetaLine(
      card,
      "Release Date",
      item.releaseDate ? formatDate(item.releaseDate) : ""
    );

    const links = document.createElement("p");
    links.className = "card-links";
    links.appendChild(
      createLink(item.wikidataId, "Wikidata", {
        linkType: "wikidata",
        resourceTitle: item.title,
        tab: "software",
      })
    );
    if (item.homepage) {
      links.appendChild(document.createTextNode(" | "));
      links.appendChild(
        createLink(item.homepage, "Website", {
          linkType: "homepage",
          resourceTitle: item.title,
          tab: "software",
        })
      );
    }
    if (item.sourceRepo) {
      links.appendChild(document.createTextNode(" | "));
      links.appendChild(
        createLink(item.sourceRepo, "Source", {
          linkType: "source_repo",
          resourceTitle: item.title,
          tab: "software",
        })
      );
    }
    card.appendChild(links);

    return card;
  }

  function renderTable(items) {
    const tableBody = document.getElementById(TABLE_BODY_IDS[state.tab]);
    if (!tableBody) {
      return;
    }
    clearChildren(tableBody);

    if (items.length === 0) {
      const noResultsMessage =
        state.q || hasActiveCategoryFilter()
          ? "No matching resources for the current filters."
          : "No resources available.";
      tableBody.appendChild(
        renderNoResultsTableRow(
          6,
          noResultsMessage
        )
      );
      return;
    }

    const rowRenderer = state.tab === "ontologies" ? renderOntologyRow : renderSoftwareRow;
    items.forEach((item) => {
      tableBody.appendChild(rowRenderer(item));
    });
  }

  function renderCards(items) {
    const cardContainer = document.getElementById(CARD_CONTAINER_IDS[state.tab]);
    if (!cardContainer) {
      return;
    }

    clearChildren(cardContainer);

    if (items.length === 0) {
      const placeholder = document.createElement("article");
      placeholder.className = "card card-placeholder";
      const heading = document.createElement("h3");
      heading.textContent =
        state.q || hasActiveCategoryFilter()
          ? "No matching resources for the current filters."
          : "No resources available.";
      placeholder.appendChild(heading);
      cardContainer.appendChild(placeholder);
      return;
    }

    const cardRenderer = state.tab === "ontologies" ? renderOntologyCard : renderSoftwareCard;
    items.forEach((item) => {
      cardContainer.appendChild(cardRenderer(item));
    });
  }

  function clearAllPresentations() {
    TAB_ORDER.forEach((tabName) => {
      const tableBody = document.getElementById(TABLE_BODY_IDS[tabName]);
      const cardContainer = document.getElementById(CARD_CONTAINER_IDS[tabName]);
      if (tableBody) {
        clearChildren(tableBody);
      }
      if (cardContainer) {
        clearChildren(cardContainer);
      }
    });
  }

  function updateResultsMeta(pageInfo, matchingCount, totalCount) {
    const label = state.tab === "ontologies" ? "resources" : "software entries";
    const firstShown = matchingCount ? pageInfo.start + 1 : 0;
    const lastShown = pageInfo.start + pageInfo.items.length;
    const rangeText =
      firstShown === lastShown
        ? firstShown.toLocaleString()
        : `${firstShown.toLocaleString()}–${lastShown.toLocaleString()}`;
    const matchingText = matchingCount.toLocaleString();
    const totalText = totalCount.toLocaleString();
    const queryText = state.q ? ` for "${state.q}"` : "";
    const categoryText =
      hasActiveCategoryFilter() && CATEGORY_ID_TO_LABEL.has(state.category)
        ? ` in ${CATEGORY_ID_TO_LABEL.get(state.category)}`
        : hasActiveSoftwareTypeFilter() && SOFTWARE_TYPE_ID_TO_LABEL.has(state.softwareType)
        ? ` in ${SOFTWARE_TYPE_ID_TO_LABEL.get(state.softwareType)}`
        : "";
    const catalogText = matchingCount === totalCount ? "" : ` (${totalText} total)`;
    dom.resultsMeta.textContent = `Showing ${rangeText} of ${matchingText} ${label}${categoryText}${queryText}${catalogText}.`;
  }

  function updatePagination(pageInfo, matchingCount) {
    if (!dom.pagination || !dom.previousPage || !dom.nextPage || !dom.pageStatus) {
      return;
    }
    dom.pagination.hidden = matchingCount === 0;
    dom.previousPage.disabled = pageInfo.page <= 1;
    dom.nextPage.disabled = pageInfo.page >= pageInfo.totalPages;
    dom.pageStatus.textContent = `Page ${pageInfo.page.toLocaleString()} of ${pageInfo.totalPages.toLocaleString()}`;
  }

  function updateTabUi() {
    TAB_ORDER.forEach((tabName) => {
      const tabButton = document.getElementById(TAB_IDS[tabName]);
      const panel = document.getElementById(PANEL_IDS[tabName]);
      const isActive = tabName === state.tab;

      if (!tabButton || !panel) {
        return;
      }

      tabButton.classList.toggle("is-active", isActive);
      tabButton.setAttribute("aria-selected", isActive ? "true" : "false");
      tabButton.setAttribute("tabindex", isActive ? "0" : "-1");
      panel.hidden = !isActive;
    });
  }

  function updateCategoryUi() {
    const isOntologyTab = state.tab === "ontologies";
    if (dom.categoryFilters) {
      dom.categoryFilters.hidden = !isOntologyTab;
    }

    dom.categoryButtons.forEach((button) => {
      const categoryId = button.dataset.category;
      const isActive = isOntologyTab && categoryId === state.category;
      button.classList.toggle("is-active", isActive);
      button.setAttribute("aria-pressed", isActive ? "true" : "false");
    });
  }

  function updateSoftwareTypeUi() {
    const isSoftwareTab = state.tab === "software";
    if (dom.softwareTypeFilters) {
      dom.softwareTypeFilters.hidden = !isSoftwareTab;
    }

    dom.softwareTypeButtons.forEach((button) => {
      const softwareTypeId = button.dataset.softwareType;
      const isActive = isSoftwareTab && softwareTypeId === state.softwareType;
      button.classList.toggle("is-active", isActive);
      button.setAttribute("aria-pressed", isActive ? "true" : "false");
    });
  }

  function updateSortUi() {
    dom.sortButtons.forEach((button) => {
      const panel = button.closest("[data-panel]");
      if (!panel) {
        return;
      }
      const isPanelActive = panel.getAttribute("data-panel") === state.tab;
      const isActiveSort = isPanelActive && button.dataset.sort === state.sort;
      const header = button.closest("th");

      button.classList.toggle("is-active", isActiveSort);

      if (isActiveSort) {
        button.dataset.order = state.order;
        if (header) {
          header.setAttribute(
            "aria-sort",
            state.order === "asc" ? "ascending" : "descending"
          );
        }
      } else {
        button.removeAttribute("data-order");
        if (header) {
          header.setAttribute("aria-sort", "none");
        }
      }
    });
  }

  function render() {
    clearAllPresentations();
    updateTabUi();
    updateCategoryUi();
    updateSoftwareTypeUi();
    updateSortUi();

    const allItems = store[state.tab] || [];
    if (store.loadStatus[state.tab] === "error") {
      const message =
        state.tab === "ontologies"
          ? "Unable to load ontologies and vocabularies. The software catalog remains available."
          : "Unable to load software. The ontology catalog remains available.";
      if (isCardView()) {
        renderCards([]);
      } else {
        renderTable([]);
      }
      dom.resultsMeta.textContent = message;
      if (dom.pagination) {
        dom.pagination.hidden = true;
      }
      return;
    }

    const matchingItems = getActiveItems();
    const pageInfo = paginateItems(matchingItems);
    if (isCardView()) {
      renderCards(pageInfo.items);
    } else {
      renderTable(pageInfo.items);
    }
    updateResultsMeta(pageInfo, matchingItems.length, allItems.length);
    updatePagination(pageInfo, matchingItems.length);
  }

  function setLoadingState() {
    dom.resultsMeta.textContent = "Loading catalog data...";
  }

  function syncSearchInput() {
    if (dom.searchInput) {
      dom.searchInput.value = state.q;
    }
  }

  function applyState(nextState, historyAction = "push") {
    state = normalizeState(nextState);
    syncSearchInput();
    if (historyAction !== "none") {
      updateUrlFromState(historyAction);
    }
    render();
  }

  function toggleSort(sortKey) {
    const nextState = { ...state, page: 1 };
    const previousSort = state.sort;
    const previousOrder = state.order;
    if (nextState.sort === sortKey) {
      nextState.order = nextState.order === "asc" ? "desc" : "asc";
    } else {
      nextState.sort = sortKey;
      nextState.order = "asc";
    }
    const normalizedNextState = normalizeState(nextState);
    applyState(normalizedNextState);
    trackAnalyticsEvent("sort_change", {
      tab: normalizedNextState.tab,
      sort: normalizedNextState.sort,
      order: normalizedNextState.order,
      previousSort,
      previousOrder,
    });
  }

  function switchTab(nextTab) {
    if (!isValidTab(nextTab) || nextTab === state.tab) {
      return;
    }
    const previousTab = state.tab;
    const nextState = { ...state, tab: nextTab, page: 1 };
    if (!isSortAllowed(nextState.tab, nextState.sort)) {
      nextState.sort = TAB_DEFAULT_SORT[nextState.tab].sort;
      nextState.order = TAB_DEFAULT_SORT[nextState.tab].order;
    }
    applyState(nextState);
    trackAnalyticsEvent("tab_switch", {
      fromTab: previousTab,
      toTab: nextTab,
    });
  }

  function moveTabFocus(currentTab, direction) {
    const currentIndex = TAB_ORDER.indexOf(currentTab);
    if (currentIndex === -1) {
      return;
    }
    const nextIndex = (currentIndex + direction + TAB_ORDER.length) % TAB_ORDER.length;
    const nextTab = TAB_ORDER[nextIndex];
    switchTab(nextTab);
    const targetButton = document.getElementById(TAB_IDS[nextTab]);
    if (targetButton) {
      targetButton.focus();
    }
  }

  function bindEvents() {
    dom.tabs.forEach((tabButton) => {
      tabButton.addEventListener("click", () => {
        const nextTab = tabButton.dataset.tab;
        if (nextTab) {
          switchTab(nextTab);
        }
      });

      tabButton.addEventListener("keydown", (event) => {
        const currentTab = tabButton.dataset.tab;
        if (!currentTab) {
          return;
        }

        if (event.key === "ArrowRight") {
          event.preventDefault();
          moveTabFocus(currentTab, 1);
          return;
        }
        if (event.key === "ArrowLeft") {
          event.preventDefault();
          moveTabFocus(currentTab, -1);
          return;
        }
        if (event.key === "Home") {
          event.preventDefault();
          switchTab(TAB_ORDER[0]);
          const first = document.getElementById(TAB_IDS[TAB_ORDER[0]]);
          if (first) {
            first.focus();
          }
          return;
        }
        if (event.key === "End") {
          event.preventDefault();
          const lastTab = TAB_ORDER[TAB_ORDER.length - 1];
          switchTab(lastTab);
          const last = document.getElementById(TAB_IDS[lastTab]);
          if (last) {
            last.focus();
          }
        }
      });
    });

    dom.sortButtons.forEach((button) => {
      button.addEventListener("click", () => {
        const sortKey = button.dataset.sort;
        const panel = button.closest("[data-panel]");
        if (!sortKey || !panel) {
          return;
        }
        const panelTab = panel.getAttribute("data-panel");
        if (panelTab !== state.tab) {
          return;
        }
        if (!isSortAllowed(state.tab, sortKey)) {
          return;
        }
        toggleSort(sortKey);
      });
    });

    dom.categoryButtons.forEach((button) => {
      button.addEventListener("click", () => {
        const categoryId = button.dataset.category;
        if (!isValidCategoryId(categoryId)) {
          return;
        }
        if (categoryId === state.category) {
          return;
        }
        applyState({ ...state, category: categoryId, page: 1 });
        trackAnalyticsEvent("category_filter", {
          tab: state.tab,
          category: categoryId,
          categoryLabel: CATEGORY_ID_TO_LABEL.get(categoryId) || "Unknown",
        });
      });
    });

    dom.softwareTypeButtons.forEach((button) => {
      button.addEventListener("click", () => {
        const softwareTypeId = button.dataset.softwareType;
        if (!isValidSoftwareTypeId(softwareTypeId)) {
          return;
        }
        if (softwareTypeId === state.softwareType) {
          return;
        }
        applyState({ ...state, softwareType: softwareTypeId, page: 1 });
        trackAnalyticsEvent("software_type_filter", {
          tab: state.tab,
          softwareType: softwareTypeId,
          softwareTypeLabel: SOFTWARE_TYPE_ID_TO_LABEL.get(softwareTypeId) || "Unknown",
        });
      });
    });

    if (dom.searchInput) {
      const debounced = debounce((rawValue) => {
        applyState({ ...state, q: rawValue, page: 1 });
        trackSearchQuery(rawValue);
      }, SEARCH_DEBOUNCE_MS);

      dom.searchInput.addEventListener("input", (event) => {
        const target = event.target;
        debounced(target.value);
      });
    }

    if (dom.previousPage) {
      dom.previousPage.addEventListener("click", () => {
        if (state.page > 1) {
          applyState({ ...state, page: state.page - 1 });
        }
      });
    }

    if (dom.nextPage) {
      dom.nextPage.addEventListener("click", () => {
        const matchingCount = getActiveItems().length;
        const totalPages = Math.max(1, Math.ceil(matchingCount / PAGE_SIZE));
        if (state.page < totalPages) {
          applyState({ ...state, page: state.page + 1 });
        }
      });
    }

    document.addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof Element)) {
        return;
      }

      const link = target.closest('a[data-track-outbound="true"]');
      if (!link) {
        return;
      }

      const href = link.getAttribute("href") || "";
      if (!/^https?:\/\//i.test(href)) {
        return;
      }

      trackAnalyticsEvent("outbound_link_click", {
        tab: link.dataset.tab || state.tab,
        linkType: link.dataset.linkType || "external",
        destinationHost: extractHost(href),
        resourceTitle: (link.dataset.resourceTitle || "").slice(0, 120),
      });
    });

    window.addEventListener("popstate", () => {
      applyState(parseStateFromUrl(), "none");
    });

    const rebuildResponsivePresentation = () => render();
    if (typeof responsiveMedia.addEventListener === "function") {
      responsiveMedia.addEventListener("change", rebuildResponsivePresentation);
    } else if (typeof responsiveMedia.addListener === "function") {
      responsiveMedia.addListener(rebuildResponsivePresentation);
    }
  }

  async function init() {
    setLoadingState();

    const [ontologyResult, softwareResult, vocabularyResult, qidResult, manifestResult] =
      await Promise.allSettled([
        fetchJsonWithFallback(DATA_PATHS.ontologies),
        fetchJsonWithFallback(DATA_PATHS.software),
        fetchJsonWithFallback(DATA_PATHS.controlledVocabularies),
        fetchJsonWithFallback(DATA_PATHS.pageQids),
        fetchJsonWithFallback(DATA_PATHS.manifest),
      ]);

    if (vocabularyResult.status === "fulfilled") {
      configureControlledVocabularies(vocabularyResult.value.payload);
      setFilterAvailability(true);
    } else {
      configureControlledVocabularies({ categories: [], softwareTypes: [] });
      setFilterAvailability(false);
      console.warn("Controlled vocabularies unavailable", vocabularyResult.reason);
    }

    for (const [tabName, result] of [
      ["ontologies", ontologyResult],
      ["software", softwareResult],
    ]) {
      if (result.status === "fulfilled" && Array.isArray(result.value.payload?.items)) {
        store[tabName] = result.value.payload.items.map(normalizeItem);
        store.loadStatus[tabName] = "ready";
      } else {
        store[tabName] = [];
        store.loadStatus[tabName] = "error";
        console.warn(`${tabName} catalog unavailable`, result.reason || "Invalid payload");
      }
    }

    const pathSource =
      ontologyResult.status === "fulfilled"
        ? ontologyResult.value.path
        : softwareResult.status === "fulfilled"
        ? softwareResult.value.path
        : null;
    if (pathSource) {
      updateTtlLinksFromJsonPath(pathSource);
    }

    if (qidResult.status === "fulfilled") {
      const slugs = qidResult.value.payload;
      store.pageSlugs.resource = slugs?.resource || {};
      store.pageSlugs.software = slugs?.software || {};
    } else {
      console.warn("Detail-page registry unavailable", qidResult.reason);
    }

    if (manifestResult.status === "fulfilled") {
      setFreshnessMetadata(manifestResult.value.payload);
    } else {
      setFreshnessMetadata(null);
      console.warn("Catalog freshness metadata unavailable", manifestResult.reason);
    }

    state = normalizeState(parseStateFromUrl());
    bindEvents();
    applyState(state, "replace");
  }

  init();
})();
