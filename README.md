# Open Knowledge Graph Resources

A lightweight catalog for **discovering knowledge graph resources** — including **ontologies, controlled vocabularies, and semantic / KG tools** — powered by live queries to **Wikidata**.

👉 **Live app:** *https://kgresources.streamlit.app/*

---

## What this is

- A **Streamlit app** for exploring KG-related resources documented in Wikidata  
- Includes both **open and proprietary** resources  
- Designed for **human-friendly discovery**, not SPARQL users  
- Surfaces better-documented resources first (e.g. those with websites and licenses)

Inspired by *Scholia*, but focused on **knowledge graph infrastructure**.

---

## What’s included

- Ontologies & controlled vocabularies  
- Semantic / knowledge graph software and platforms  
- Clickable links to Wikidata pages and official websites  
- Client-side filtering and ranking  

---

## How it works

- Runs live **SPARQL queries** against Wikidata  
- Caches results to reduce load  
- Cleans and ranks data client-side  
- Displays results in a sortable, clickable table  

---

## Running locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
