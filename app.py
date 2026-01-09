import time
import requests
import pandas as pd
import streamlit as st
from streamlit.column_config import LinkColumn

WDQS_URL = "https://query.wikidata.org/sparql"

# IMPORTANT: WDQS asks bots/scripts to use an identifiable User-Agent with contact info. :contentReference[oaicite:0]{index=0}
USER_AGENT = "KGResourcesStreamlit/0.1 (contact: you@example.com)"

st.set_page_config(page_title="Knowledge Graph Resources", layout="wide")
st.title("Knowledge Graph Resources")
st.caption("Ontologies, controlled vocabularies, and semantic tools—queried from Wikidata.")

RESOURCE_KIND = st.sidebar.selectbox(
    "Resource type",
    ["Ontologies + Controlled Vocabularies", "Software (semantic / KG tools)"]
)

LIMIT = st.sidebar.slider("Max results", 50, 2000, 500, step=50)

REPO_URL = "https://github.com/SteveHedden/open-knowledge-graph-resources"

with st.sidebar:
    st.markdown("### About")
    st.markdown(
        f"- **Source code:** [GitHub repository]({REPO_URL})\n"
        f"- **Data:** Wikidata (live queries)"
    )

BASE_QUERY_ONTO_VOCAB = f"""
SELECT
  ?item
  ?itemLabel
  (GROUP_CONCAT(DISTINCT STR(?officialWebsite); separator=" | ") AS ?officialWebsites)
  (GROUP_CONCAT(DISTINCT ?licenseLabel; separator=" | ") AS ?licenses)
  (GROUP_CONCAT(DISTINCT ?partOfLabel; separator=" | ") AS ?partOf)
WHERE {{
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "[AUTO_LANGUAGE],mul,en". }}

  {{
    SELECT DISTINCT ?item WHERE {{
      {{
        ?item p:P31 ?statement0 .
        ?statement0 (ps:P31/(wdt:P279*)) wd:Q324254 .  # ontology
      }}
      UNION
      {{
        ?item p:P31 ?statement1 .
        ?statement1 (ps:P31/(wdt:P279*)) wd:Q1469824 . # controlled vocabulary
      }}
      UNION
      {{
        ?item p:P31 ?statement2 .
        ?statement2 (ps:P31/(wdt:P279*)) wd:Q8269924 .  # taxonomy  <-- add this
      }}
    }}
    LIMIT {LIMIT}
  }}

  OPTIONAL {{ ?item wdt:P856 ?officialWebsite . }}
  OPTIONAL {{ ?item wdt:P275 ?license . }}
  OPTIONAL {{ ?item wdt:P361 ?partOfEntity . }}

  SERVICE wikibase:label {{
    bd:serviceParam wikibase:language "[AUTO_LANGUAGE],mul,en".
    ?license rdfs:label ?licenseLabel .
    ?partOfEntity rdfs:label ?partOfLabel .
  }}
}}
GROUP BY ?item ?itemLabel
"""

# You can refine "software" class later; this is a decent starting point.
# You can refine "software" class later; this is a decent starting point.
BASE_QUERY_SOFTWARE = f"""
SELECT
  ?item
  ?itemLabel
  (GROUP_CONCAT(DISTINCT STR(?officialWebsite); separator=" | ") AS ?officialWebsites)
  (GROUP_CONCAT(DISTINCT ?licenseLabel; separator=" | ") AS ?licenses)
  (GROUP_CONCAT(DISTINCT ?partOfLabel; separator=" | ") AS ?partOf)
  (SAMPLE(?version) AS ?latestVersion)
  (MAX(?pubDate) AS ?latestReleaseDate)
WHERE {{
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "[AUTO_LANGUAGE],mul,en". }}

  {{
    SELECT DISTINCT ?item WHERE {{
      ?item wdt:P31/wdt:P279* wd:Q124653107 .  # software
    }}
    LIMIT {LIMIT}
  }}

  OPTIONAL {{ ?item wdt:P856 ?officialWebsite . }}
  OPTIONAL {{ ?item wdt:P275 ?license . }}
  OPTIONAL {{ ?item wdt:P361 ?partOfEntity . }}

  # P348 statement + P577 qualifier on that statement
  OPTIONAL {{
    ?item p:P348 ?verStmt .
    ?verStmt ps:P348 ?version .
    OPTIONAL {{ ?verStmt pq:P577 ?pubDate . }}
  }}

    # P348 statement + P577 qualifier on that statement
  OPTIONAL {{
    ?item p:P348 ?verStmt .
    ?verStmt ps:P348 ?version .
    OPTIONAL {{ ?verStmt pq:P577 ?pubDate . }}
  }}

  SERVICE wikibase:label {{
    bd:serviceParam wikibase:language "[AUTO_LANGUAGE],mul,en".
    ?license rdfs:label ?licenseLabel .
    ?partOfEntity rdfs:label ?partOfLabel .
  }}
}}
GROUP BY ?item ?itemLabel
"""


def run_wdqs(query: str) -> dict:
    headers = {
        "Accept": "application/sparql-results+json",
        "User-Agent": USER_AGENT,
    }
    r = requests.get(WDQS_URL, params={"query": query, "format": "json"}, headers=headers, timeout=60)
    if r.status_code == 429:
        # Respect Retry-After when rate limited (common WDQS pattern). :contentReference[oaicite:1]{index=1}
        retry_after = int(r.headers.get("Retry-After", "5"))
        time.sleep(retry_after)
        r = requests.get(WDQS_URL, params={"query": query, "format": "json"}, headers=headers, timeout=60)
    r.raise_for_status()
    return r.json()

@st.cache_data(ttl=60 * 60 * 6)  # cache 6 hours to be polite + fast
def query_to_df(query: str) -> pd.DataFrame:
    data = run_wdqs(query)
    rows = []
    for b in data["results"]["bindings"]:
        rows.append({
            "Item": b["itemLabel"]["value"],
            "Wikidata": b["item"]["value"],
            "Official websites": b.get("officialWebsites", {}).get("value", ""),
            "Licenses": b.get("licenses", {}).get("value", ""),
            "Part of": b.get("partOf", {}).get("value", ""),
            "latest release": b.get("latestReleaseDate", {}).get("value", ""),

        })
    return pd.DataFrame(rows)


query = BASE_QUERY_ONTO_VOCAB if RESOURCE_KIND.startswith("Ontologies") else BASE_QUERY_SOFTWARE

st.info("Tip: This app caches results for 6 hours to reduce load on Wikidata Query Service.")
df = query_to_df(query)

is_software = RESOURCE_KIND.startswith("Software")

# Common derived fields
df["Has website"] = df["Official websites"].str.strip().ne("")
df["Has license"] = df["Licenses"].str.strip().ne("")

# ---- Make links nicer for clicking (common) ----
df["Wikidata page"] = df["Wikidata"]
df["Official website"] = df["Official websites"].str.split(" | ").str[0].fillna("")

# ---- Branch: Software vs Ontologies/Vocabs ----
if is_software:
    # Parse release date only for software
    df["latest release"] = pd.to_datetime(df["latest release"], errors="coerce")

    # Sort: newest releases first, then quality signals, then name
    df = df.sort_values(
        by=["latest release", "Has website", "Has license", "Item"],
        ascending=[False, False, False, True]
    )

    # Re-order columns for software
    desired_cols = [
        "Item",
        "latest release",
        "Licenses",
        "Official website",
        "Wikidata page",
    ]
    # Only keep ones that exist (safe if you tweak later)
    df = df[[c for c in desired_cols if c in df.columns]]

    # Display config: show dates nicely
    column_config = {
        "Wikidata page": st.column_config.LinkColumn("Wikidata", display_text="Open"),
        "Official website": st.column_config.LinkColumn("Website", display_text="Visit"),
        "latest release": st.column_config.DateColumn("Latest release"),
    }

else:
    # Ontologies/Vocabs: don't treat "latest release" as meaningful
    # (it will be blank / NaT for most, and sorting by it is weird)

    # Sort: prioritize website/license coverage, then name
    df = df.sort_values(
        by=["Has website", "Has license", "Item"],
        ascending=[False, False, True]
    )

    # Re-order columns for ontologies/vocabs
    desired_cols = [
        "Item",
        "Licenses",
        "Official website",
        "Wikidata page",
        "Part of",
    ]
    df = df[[c for c in desired_cols if c in df.columns]]

    column_config = {
        "Wikidata page": st.column_config.LinkColumn("Wikidata", display_text="Open"),
        "Official website": st.column_config.LinkColumn("Website", display_text="Visit"),
    }

# Drop helper cols if they somehow survived
df = df.drop(columns=[c for c in ["Has website", "Has license", "Wikidata", "Official websites"] if c in df.columns], errors="ignore")

# Client-side filter (works for both)
search = st.text_input("Filter results (client-side)", "")
if search:
    df = df[df["Item"].str.contains(search, case=False, na=False)]

st.write(f"Results: {len(df):,}")

st.data_editor(
    df,
    use_container_width=True,
    disabled=True,
    column_config=column_config,
)
