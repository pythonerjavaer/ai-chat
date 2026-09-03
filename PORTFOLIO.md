# AI Product Portfolio | Frostfire · Future Radar

> AI product workspace, Agent workflows, evidence-driven decision support, and a campus-recruitment intelligence product built from 0→1.

## 1. Product Overview

**Frostfire（冰焰）** is an AI product workspace that combines evidence-driven document research, AI-assisted workflows, and lightweight AI Space outputs. The repository contains Web, iOS and Android projects. The backend is built with **FastAPI**, **PostgreSQL / Supabase** (SQLite for local development), and the **OpenAI API**; the frontend uses **Vite + HTML/CSS/JavaScript + Capacitor**.

Within Frostfire, **Future Radar（未来雷达）** is the core product I use to explore AI-driven information aggregation and decision support for campus recruitment. It was designed to address several recurring user problems:

- recruitment information is scattered across many sources;
- the same opportunity may appear repeatedly with inconsistent descriptions;
- deadlines and status changes are difficult to track;
- users need help deciding which opportunities deserve attention first;
- AI-generated results need provenance, verification and human review rather than opaque conclusions.

## 2. My Role

**AI Product Lead / Product Design & Development**

I am responsible for the product from problem definition to implementation, including:

- user problem identification and requirement analysis;
- MVP scope and product structure;
- workflow and information architecture design;
- prioritisation and decision rules;
- data model and API-level product logic;
- human–AI division of work;
- acceptance criteria, exception handling and review mechanisms;
- implementation and iterative validation with AI-assisted development tools.

The goal is not simply to add an LLM chat box to an application, but to design a workflow in which AI performs bounded tasks while important product decisions remain explainable and reviewable.

## 3. Future Radar | Product Workflow

The main workflow is:

```text
Data / source intake
        ↓
Structured extraction
        ↓
Rule validation
        ↓
Stable identity & deduplication
        ↓
Opportunity / job upsert
        ↓
Priority evaluation
        ↓
Evidence & result explanation
        ↓
Status tracking / incremental events
        ↓
Human review when needed
```

The server-side intelligence layer uses a **Source Registry** and supports multiple public-source adapters. Data is normalised into stable identities and semantic hashes before being written into recruitment programs, jobs, provenance links and change events. The UI reads persisted incremental events rather than pretending browser polling is a background crawler.

## 4. Product Decisions I Focused On

### Evidence before confidence

A source can provide a **discovery lead** without automatically becoming a verified job. Official or explicitly trusted verification sources are treated differently from lower-trust discovery sources. This prevents an AI or aggregator result from being presented as an official confirmation without evidence.

### Deduplication without deleting real differences

Future Radar distinguishes stable job identity from superficial differences such as locale or URL presentation. At the same time, different official job IDs or genuinely different recruitment scopes are not merged just because titles look similar.

### Failure isolation

A failed source should not invalidate the rest of a scan or automatically close previously active jobs. Runs can be successful, partially successful or failed; source failures are isolated and closure requires sufficient evidence.

### Explainable prioritisation

The product evaluates **specific opportunities rather than company logos**. Current dimensions include platform level, job quality, background fit, career direction, long-term upside, transferability, eligibility, compensation/benefits, sustainability, city/location and continuing-education compatibility. The interface can display the underlying dimensions and calibration instead of only showing a final tier label.

### Human–AI collaboration

I use AI for option generation, information processing, coding assistance, debugging and structured extraction. I retain responsibility for problem definition, product rules, prioritisation logic, acceptance criteria and final judgement. The product therefore includes deterministic rules, source provenance, fallback paths and manual-review mechanisms where appropriate.

## 5. AI & Engineering Stack

**AI / Data**  
LLM API · AI Agent workflows · RAG-related retrieval patterns · structured extraction · Python · SQL · data cleaning and evaluation

**Backend / Data**  
FastAPI · PostgreSQL / Supabase · SQLite (local development) · REST APIs · JWT authentication

**Frontend / Delivery**  
Vite · HTML/CSS/JavaScript · Capacitor · Web / iOS / Android project structure · Render deployment configuration

**AI-assisted development workflow**  
ChatGPT · OpenAI Codex · Grok · other AI assistants are used for requirements exploration, solution comparison, coding assistance, debugging and iteration. AI-generated suggestions are reviewed against product goals, rules and actual system behaviour before adoption.

## 6. Current Implemented Capabilities

The repository currently includes, among other capabilities:

- account registration/login, JWT authentication and user-data isolation;
- document/conversation persistence with PostgreSQL/Supabase support;
- multiple AI workspaces for legal/compliance, financial research and general documents;
- AI Space outputs with local / lean / deep execution modes and token-budget controls;
- Future Radar source registry, programs, jobs, provenance, run records and incremental change events;
- public-source discovery and official-source verification separation;
- structured opportunity pool, filters, employer grouping and job-level prioritisation;
- deployment configuration for Render and persistent PostgreSQL storage.

## 7. Why This Project Matters as an AI Product Case

Future Radar is the project I use to practise the full AI-product loop:

```text
User problem
→ requirement definition
→ MVP
→ AI / non-AI boundary
→ workflow design
→ data & API design
→ implementation
→ verification
→ iteration
```

The most important learning is that an AI product is not defined by how many model calls it makes. Product quality comes from deciding **where AI adds value, where deterministic rules are safer, how evidence is preserved, how failures are handled, and how users can understand the result**.

## 8. Repository Navigation

- Main project: [`README.md`](README.md)
- Future Radar architecture: [`docs/FUTURE_RADAR.md`](docs/FUTURE_RADAR.md)
- Future Radar backend: [`backend/future_radar`](backend/future_radar)
- Backend: [`backend`](backend)
- Frontend: [`frontend`](frontend)
- Deployment configuration: [`render.yaml`](render.yaml)

---

**Repository:** `pythonerjavaer/ai-chat`  
**Primary portfolio focus:** AI Product · Agent Workflow · Decision Support · Future Radar
