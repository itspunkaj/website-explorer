# Website Knowledge Graph Explorer

## Getting Started

### Prerequisites

- Python 3.11+
- [Neo4j Desktop](https://neo4j.com/download/) (or any Neo4j instance) running locally on `bolt://localhost:7687`
- OpenAI API key (required for Phase 2 synthesis via GPT-4.1)

### 1. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in the required values:

```
OPENAI_API_KEY=sk-...
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your-neo4j-password
```

Add `ANTHROPIC_API_KEY` if you want to use the standalone `main.py` agent with Claude.

### 3. Run the web app (recommended)

```bash
python app.py
```

Open [http://localhost:8000](http://localhost:8000) in your browser, enter a URL, and the full pipeline runs via the UI.

### 4. Run the full KG pipeline from the CLI

Edit the `TARGET_URL` at the top of `kg_run.py`, then:

```bash
python kg_run.py
```

This runs Phase 1 (agent exploration) + Phase 2 (Playwright DOM extraction + synthesis) and writes the result to Neo4j and a local JSON file.

### 5. Run the simple browser agent only

```bash
python main.py
```

This runs a standalone Browser-Use agent with Claude and prints the raw exploration result — no KG or Neo4j involved.

### 6. Explore the graph in Neo4j Browser

Open [http://localhost:7474](http://localhost:7474) and run:

```cypher
MATCH (n) RETURN n LIMIT 100
```

---

## 1. Approach & Architecture

The system automates the discovery of user flows, UI interactions, and test scenarios across any website by combining two complementary exploration strategies and persisting findings in a queryable graph database.

### High-Level Pipeline

```
User submits URL
      │
      ▼
┌─────────────────────────────────────────────────────────┐
│  PHASE 1 — Visual Agent Exploration                     │
│  Browser-Use agent (GPT-4.1-mini) browses the site,     │
│  follows internal links, scrolls each page, and         │
│  extracts flows, interactions, and page summaries.      │
└─────────────────────┬───────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────┐
│  PHASE 2a — DOM Extraction (Playwright)                 │
│  Headless browser serializes the DOM tree, captures     │
│  every interactive element with its selectors,          │
│  attributes, bounding boxes, and event listeners.       │
└─────────────────────┬───────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────┐
│  PHASE 2b — Interactive DOM Exploration (Playwright)    │
│  Clicks every element, records DOM mutations, network   │
│  calls, and URL changes, producing action logs and      │
│  state-transition pairs.                                │
└─────────────────────┬───────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────┐
│  PHASE 2c — Hybrid KG Synthesis (GPT-4.1)               │
│  LLM receives structured DOM data + action logs and     │
│  outputs a validated WebsiteKnowledgeGraph: elements,   │
│  components, flows, and features.                       │
└─────────────────────┬───────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────┐
│  PHASE 2d — Neo4j Ingestion (State-Action Schema)       │
│  Graph written to Neo4j. KG flows are compared against  │
│  agent flows; flows missed by the agent are flagged.    │
└─────────────────────────────────────────────────────────┘
```

### Rationale for Dual Exploration

The visual agent (Phase 1) mimics real user behaviour — it understands context, follows meaningful links, and captures multi-step journeys. However it is non-deterministic and can miss elements that are not immediately visible. The Playwright-based pipeline (Phase 2) is deterministic — it enumerates every interactive element regardless of visual prominence and records exact state transitions. Together they provide both semantic understanding and structural completeness.

---

## 2. Tools and Frameworks Used

| Layer | Tool / Library | Purpose |
|---|---|---|
| Browser automation | **Browser-Use** | Agentic, multi-step website crawling with visual understanding |
| Headless browser | **Playwright** (async) | DOM serialization, element interaction, network interception |
| LLM — exploration | **GPT-4.1-mini** (OpenAI) | Low-cost visual browsing agent |
| LLM — synthesis | **GPT-4.1** (OpenAI) | High-quality KG generation from structured DOM data |
| Web framework | **FastAPI** | Async REST API + Jinja2 HTML rendering |
| Graph database | **Neo4j** | Persistent, queryable knowledge graph |
| Relational store | **SQLite** | Lightweight result cache and status tracking |
| Data validation | **Pydantic v2** | Typed models across the entire pipeline |
| HTTP server | **Uvicorn** | ASGI server for FastAPI |
| Templating | **Jinja2** | Server-rendered dashboard and home page |

---

## 3. How the Browser Agent Was Configured

The agent runs in `knowledge_graph/exploration_agent.py` using the **Browser-Use** framework with `GPT-4.1-mini` as the underlying LLM.

```python
llm  = ChatOpenAI(model="gpt-4.1-mini")
agent = Agent(
    task=task,
    llm=llm,
    output_model_schema=AgentExploration,   # enforces structured JSON output
)
history = await agent.run(max_steps=50)
```

### Task Prompt (key instructions given to the agent)

- Start at the provided root URL and follow **internal links only** — no external domains, no subdomains.
- **Scroll each page top-to-bottom** before navigating away to ensure dynamic content is captured.
- Track the URL for every interaction found.
- Extract three artefacts per run:
  1. **Page summaries** — URL + 1-2 sentence description.
  2. **Interactions** — every nav link, button, input, form, dropdown, toggle, and icon labelled by type (e.g. `Nav: Home`, `Button: Get Started`, `Input: Email`).
  3. **Flows** — named multi-step journeys a user would actually perform, each with ordered steps and 2–3 QA test cases.

### Structured Output Schema

The agent is constrained to return `AgentExploration`:

```
AgentExploration
├── url            — root URL explored
├── page_title     — title of the root page
├── summary        — 1-2 sentence site overview
├── pages[]        — AgentPageSummary (url, title, summary)
├── flows[]        — AgentFlow (name, description, steps[], test_cases[])
└── interactions[] — flat list of labelled interactive elements
```

---

## 4. How the Knowledge Graph Schema Was Designed

The schema is a **State-Action model** — it separates structural page hierarchy from behavioural state transitions. This design was chosen because it mirrors how test automation actually works: a selector on a specific DOM state, an action performed, and the resulting state.

### Node Types

| Node | Key Properties | Description |
|---|---|---|
| `Page` | `template_url`, `original_url`, `title` | Canonical route. Dynamic IDs in URLs are normalised (`/products/123` → `/products/{id}`). |
| `State` | `signature` (SHA-256[:24]), `page_id`, `dom_hash`, `auth_flag`, `modal_flag` | Unique snapshot of a page at a point in time. Two visits to the same URL with different DOM content produce different states. |
| `Element` | `selector_id`, `tag`, `text`, `testid_selector`, `aria_selector`, `css_selector`, `xpath_selector`, `selector_stability_score` | A single interactable DOM node observed in a specific state. |
| `Component` | `id`, `name`, `component_type`, `element_ids` | A logical cluster of elements (e.g. Navigation Bar, Contact Form). |
| `Action` | `id`, `verb`, `element_selector_id`, `state_before_id`, `state_after_id`, `observed_count` | A recorded verb performed on an element that produced a state change. |
| `Feature` | `id`, `name`, `description`, `flow_ids` | A broad capability grouping one or more flows. |
| `Flow` | `id`, `name`, `description`, `steps[]` | An ordered sequence of actions forming a user journey. |

### Relationship Types

```cypher
(Page)-[:HAS_STATE]->(State)
(State)-[:HAS_ELEMENT]->(Element)
(Element)-[:PART_OF]->(Component)
(Component)-[:BELONGS_TO]->(Feature)
(State)-[:TRANSITIONS_TO {action_id, observed_count, dom_diff_hash}]->(State)
(Action)-[:PERFORMED_ON]->(Element)
(Action)-[:REQUIRES]->(State)
(Action)-[:CAUSES]->(State)
(Flow)-[:CONTAINS {order}]->(Action)
(Feature)-[:DEPENDS_ON]->(Feature)
```

The `TRANSITIONS_TO` edge is the most important: it records every observed state change, enabling graph traversal to reconstruct any user journey and surface paths the agent never explicitly described.

### URL Canonicalization

To avoid treating `/products/1` and `/products/2` as different pages, all URLs are normalised before creating `Page` nodes:

- Numeric path segments replaced: `/products/123` → `/products/{id}`
- UUIDs replaced: `/users/abc-123-def` → `/users/{id}`
- Tracking query params stripped: `utm_source`, `fbclid`, `gclid`, `_ga`, `ref`
- Semantic params preserved: `?category=`, `?sort=`

### State Signature

```
signature = SHA-256( url_path | dom_hash | auth_flag | modal_flag )[:24]
```

This ensures that the same URL with a modal open or the user logged in produces a distinct state node.

### Selector Stability Scoring

Elements are stored with all available selectors ranked by test robustness:

| Score | Selector Type | Example |
|---|---|---|
| 1.0 | `data-testid` attribute | `[data-testid="submit-btn"]` |
| 0.8 | `aria-label` + `role` | `[role="button"][aria-label="Close"]` |
| 0.5 | CSS selector | `.hero__cta` |
| 0.2 | XPath fallback | `//div[2]/button[1]` |

---

## 5. Sample Discovered Scenarios (Agent — Phase 1)

The following are representative flows the visual agent typically discovers on a marketing or SaaS website:

### Flow 1 — Submit Contact Form
**Steps:**
1. Navigate to the homepage
2. Click the "Contact" navigation link
3. Fill in the Name field
4. Fill in the Email field
5. Fill in the Message textarea
6. Click the "Send" / "Submit" button

**Test cases generated:**
- Valid submission with all required fields populated
- Attempt submission with the email field left blank — expect inline validation error
- Submit with an invalid email format — expect format validation message

---

### Flow 2 — Navigate to Product / Service Page
**Steps:**
1. Land on the homepage
2. Click a featured product or service card in the hero or main section
3. Read the detail page (features, pricing, CTA)
4. Click the primary call-to-action (e.g. "Get Started", "Book a Demo")

**Test cases generated:**
- Verify all navigation links resolve to the correct pages
- Verify CTA button is present and clickable on the detail page
- Verify page title and meta description match the product

---

### Flow 3 — Social / External Link Engagement
**Steps:**
1. Scroll to the footer or header social media icons
2. Click a social link (Twitter, LinkedIn, GitHub)

**Test cases generated:**
- Verify each social link opens in a new tab
- Verify no broken links (4xx/5xx responses)

---

### Flow 4 — Primary CTA (Homepage Conversion)
**Steps:**
1. Land on homepage hero section
2. Click the primary CTA button ("Get Started", "Try Free", "Book Demo")
3. Complete the resulting action (sign-up form, calendar embed, or redirect)

**Test cases generated:**
- Verify CTA is above the fold on desktop and mobile
- Verify clicking CTA navigates to or opens the expected destination
- Verify form submission succeeds with valid inputs

---

## 6. Additional Scenarios Found via the Knowledge Graph (Phase 2)

The Playwright-based pipeline records every element interaction and the resulting DOM state changes. By traversing `TRANSITIONS_TO` edges in Neo4j the system identifies paths the visual agent never explicitly described.

### How Missed Flows Are Surfaced

After Phase 2 completes, the pipeline compares the KG's `Flow` nodes against the agent's `AgentFlow` list. Flows with no semantic match in the agent output are flagged as `missed_by_agent = true` and highlighted in the dashboard.

### Example Neo4j Queries to Explore Additional Scenarios

**Find all state transitions not covered by any agent flow:**
```cypher
MATCH (s1:State)-[t:TRANSITIONS_TO]->(s2:State)
WHERE NOT EXISTS {
  MATCH (f:Flow)-[:CONTAINS]->(a:Action)-[:CAUSES]->(s2)
}
RETURN s1.url_path, s2.url_path, t.action_id
LIMIT 20
```

**Find elements with high interaction counts that appear in no flow:**
```cypher
MATCH (e:Element)
WHERE NOT EXISTS { MATCH (:Action)-[:PERFORMED_ON]->(e) }
RETURN e.tag, e.text, e.css_selector
ORDER BY e.selector_stability_score DESC
```

**Trace a full user journey through state transitions:**
```cypher
MATCH path = (s:State {url_path: '/'})-[:TRANSITIONS_TO*1..5]->(end:State)
RETURN path
LIMIT 10
```

### Categories of Additional Scenarios Typically Found

| Category | Example | Why the agent misses it |
|---|---|---|
| Hover / tooltip interactions | Dropdown menus revealed on hover | Agent focuses on clicks; hover-only triggers are invisible without mouse events |
| Keyboard-accessible flows | Modal dismissed via Escape key | Visual agent uses mouse navigation |
| Dynamic content loads | Infinite scroll, lazy-loaded sections | Agent moves on before scroll-triggered content appears |
| Error / validation states | Form submission with missing fields | Agent generally submits valid data |
| Multi-state modal journeys | Open modal → fill form → close without submitting | Agent treats modals as one step |
| API-triggered UI changes | Button click → fetch → DOM update (no URL change) | Agent sees no navigation so may not record the state change |

---

## 7. Key Challenges, Limitations, and Next Improvements

### Challenges Encountered

**State explosion**
Dynamic SPAs can produce hundreds of unique DOM hash values for what is conceptually the same page. The state signature (URL path + DOM hash + auth/modal flags) reduces duplicates but cannot fully collapse content-driven variation (e.g. paginated search results).

**Agent non-determinism**
The visual agent (GPT-4.1-mini) does not always follow the same path on repeated runs. The number and naming of flows can vary, making direct comparison with the deterministic KG output imperfect.

**Prompt length limits**
The hybrid agent (Phase 2c) receives the full DOM element list and action logs in a single prompt. For large pages this approaches or exceeds context limits. A hard truncation is applied at ~14 000 characters, which can drop elements near the end of the list.

**Iframe and shadow DOM**
The Playwright extractor does not pierce into `<iframe>` elements or shadow DOM roots. Any interactions inside embedded widgets (chat, video players, third-party forms) are invisible to the pipeline.

**Re-navigation after destructive actions**
When clicking a link navigates away from the current page, the explorer re-loads the base URL before continuing. This breaks any multi-step state (e.g. an in-progress form) and means chained interactions within a single page session are not fully captured.

**Neo4j uniqueness on re-run**
Unique constraints on `template_url`, `signature`, and `selector_id` mean that re-running the pipeline for the same URL MERGEs existing nodes rather than replacing them. Stale data from earlier runs can persist alongside new observations.

---

### Current Limitations

- Only publicly accessible pages are explored; authenticated flows behind login walls are not reached.
- Subdomains are explicitly excluded from agent crawling (e.g. `app.example.com` vs `example.com`).
- No support for multi-tab or multi-window interactions.
- Mobile viewport testing is not performed — all exploration uses a 1280×900 desktop viewport.
- The pipeline is sequential; a single slow page load in Phase 2b blocks the entire run.

---

### Next Improvements

**Authentication support**
Accept optional username/password (or session cookie) at submission time so the pipeline can explore authenticated flows and compare the logged-in vs. logged-out state graphs.

**Multi-page DOM exploration**
Extend Phase 2b beyond the root URL — follow links discovered in Phase 1 and run interactive exploration on each internal page, not just the homepage.

**Incremental / delta runs**
Track `last_seen` timestamps on `Action` nodes to support incremental re-runs: only re-explore pages whose DOM hash has changed since the last crawl.

**Parallel phase execution**
Phase 1 (agent) and Phase 2a–2b (Playwright) are independent. Running them concurrently would halve total pipeline time.

**Iframe / shadow DOM piercing**
Use Playwright's `frame_locator` and `evaluate_handle` to extract elements from embedded iframes and open shadow roots.

**Richer scenario generation**
Use the `TRANSITIONS_TO` graph to automatically generate Playwright test scripts: each path from a start state to an end state becomes a runnable `test_*` function, ready to drop into a test suite.

**Confidence scoring on flows**
Weight flows by `observed_count` on their constituent `TRANSITIONS_TO` edges: high-count paths are core happy paths; low-count paths are edge cases. Surface this in the dashboard to help QA teams prioritise.
