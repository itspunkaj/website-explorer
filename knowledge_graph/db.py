import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "explorer.db"


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _apply_migrations(conn):
    """Apply incremental schema changes that CREATE TABLE IF NOT EXISTS won't handle."""
    existing_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(kg_elements)").fetchall()
    }
    if "page_url" not in existing_cols:
        conn.execute("ALTER TABLE kg_elements ADD COLUMN page_url TEXT DEFAULT ''")

    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if "kg_visited_pages" not in tables:
        conn.execute("""
            CREATE TABLE kg_visited_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                website_id INTEGER REFERENCES websites(id),
                url TEXT,
                title TEXT
            )
        """)


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS websites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE NOT NULL,
                title TEXT,
                status TEXT DEFAULT 'pending',
                explored_at TIMESTAMP,
                error_message TEXT
            );

            CREATE TABLE IF NOT EXISTS agent_summary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                website_id INTEGER REFERENCES websites(id),
                summary TEXT
            );

            CREATE TABLE IF NOT EXISTS agent_flows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                website_id INTEGER REFERENCES websites(id),
                name TEXT,
                description TEXT,
                steps TEXT,
                test_cases TEXT
            );

            CREATE TABLE IF NOT EXISTS agent_interactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                website_id INTEGER REFERENCES websites(id),
                interaction TEXT
            );

            CREATE TABLE IF NOT EXISTS kg_features (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                website_id INTEGER REFERENCES websites(id),
                feature_id TEXT,
                name TEXT,
                description TEXT
            );

            CREATE TABLE IF NOT EXISTS kg_flows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                website_id INTEGER REFERENCES websites(id),
                kg_feature_id INTEGER REFERENCES kg_features(id),
                flow_id TEXT,
                name TEXT,
                description TEXT,
                missed_by_agent INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS kg_components (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                website_id INTEGER REFERENCES websites(id),
                component_id TEXT,
                name TEXT,
                description TEXT,
                component_type TEXT
            );

            CREATE TABLE IF NOT EXISTS kg_elements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                website_id INTEGER REFERENCES websites(id),
                element_id TEXT,
                tag TEXT,
                text TEXT,
                selector TEXT,
                element_type TEXT,
                page_region TEXT,
                page_url TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS kg_visited_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                website_id INTEGER REFERENCES websites(id),
                url TEXT,
                title TEXT
            );

            -- New State-Action schema tables (v2) ---------------------------------

            CREATE TABLE IF NOT EXISTS kg_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                website_id INTEGER REFERENCES websites(id),
                template_url TEXT,
                original_url TEXT,
                title TEXT
            );

            CREATE TABLE IF NOT EXISTS kg_states (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                website_id INTEGER REFERENCES websites(id),
                signature TEXT,
                page_id TEXT,
                url_path TEXT,
                dom_hash TEXT,
                auth_flag INTEGER DEFAULT 0,
                modal_flag INTEGER DEFAULT 0,
                description TEXT
            );

            CREATE TABLE IF NOT EXISTS kg_elements_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                website_id INTEGER REFERENCES websites(id),
                selector_id TEXT,
                state_id TEXT,
                tag TEXT,
                text TEXT,
                testid_selector TEXT,
                aria_selector TEXT,
                css_selector TEXT,
                xpath_selector TEXT,
                selector_stability_score REAL
            );

            CREATE TABLE IF NOT EXISTS kg_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                website_id INTEGER REFERENCES websites(id),
                action_id TEXT,
                verb TEXT,
                element_selector_id TEXT,
                state_before_id TEXT,
                state_after_id TEXT,
                observed_count INTEGER DEFAULT 1,
                last_seen REAL,
                dom_diff_hash TEXT
            );

            CREATE TABLE IF NOT EXISTS kg_state_transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                website_id INTEGER REFERENCES websites(id),
                from_state_id TEXT,
                to_state_id TEXT,
                action_id TEXT,
                observed_count INTEGER DEFAULT 1
            );
        """)
        _apply_migrations(conn)


# ── Website ────────────────────────────────────────────────────────────────────

def create_website(url: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO websites (url, status) VALUES (?, 'pending')",
            (url,),
        )
        if cur.lastrowid:
            return cur.lastrowid
        row = conn.execute("SELECT id FROM websites WHERE url = ?", (url,)).fetchone()
        conn.execute("UPDATE websites SET status='pending', error_message=NULL WHERE id=?", (row["id"],))
        return row["id"]


def update_website_status(website_id: int, status: str, title: str = None, error: str = None):
    with get_conn() as conn:
        conn.execute(
            """UPDATE websites SET status=?, title=COALESCE(?,title),
               explored_at=CASE WHEN ?='done' THEN ? ELSE explored_at END,
               error_message=? WHERE id=?""",
            (status, title, status, datetime.utcnow().isoformat(), error, website_id),
        )


def get_website(website_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM websites WHERE id=?", (website_id,)).fetchone()
        return dict(row) if row else None


def get_all_websites() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM websites ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]


# ── Agent results ──────────────────────────────────────────────────────────────

def save_agent_exploration(website_id: int, exploration):
    with get_conn() as conn:
        conn.execute("DELETE FROM agent_summary WHERE website_id=?", (website_id,))
        conn.execute("DELETE FROM agent_flows WHERE website_id=?", (website_id,))
        conn.execute("DELETE FROM agent_interactions WHERE website_id=?", (website_id,))

        conn.execute(
            "INSERT INTO agent_summary (website_id, summary) VALUES (?,?)",
            (website_id, exploration.summary),
        )
        for flow in exploration.flows:
            conn.execute(
                "INSERT INTO agent_flows (website_id,name,description,steps,test_cases) VALUES (?,?,?,?,?)",
                (website_id, flow.name, flow.description,
                 json.dumps(flow.steps), json.dumps(flow.test_cases)),
            )
        for interaction in exploration.interactions:
            conn.execute(
                "INSERT INTO agent_interactions (website_id,interaction) VALUES (?,?)",
                (website_id, interaction),
            )


def get_agent_exploration(website_id: int) -> dict:
    with get_conn() as conn:
        summary_row = conn.execute(
            "SELECT summary FROM agent_summary WHERE website_id=?", (website_id,)
        ).fetchone()
        flow_rows = conn.execute(
            "SELECT * FROM agent_flows WHERE website_id=?", (website_id,)
        ).fetchall()
        interaction_rows = conn.execute(
            "SELECT interaction FROM agent_interactions WHERE website_id=?", (website_id,)
        ).fetchall()

        flows = []
        for r in flow_rows:
            flows.append({
                "name": r["name"],
                "description": r["description"],
                "steps": json.loads(r["steps"]),
                "test_cases": json.loads(r["test_cases"]),
            })

        return {
            "summary": summary_row["summary"] if summary_row else "",
            "flows": flows,
            "interactions": [r["interaction"] for r in interaction_rows],
        }


# ── KG results ─────────────────────────────────────────────────────────────────

def save_kg_data(website_id: int, kg, agent_flows: list):
    agent_names = {f["name"].lower() for f in agent_flows}

    def is_missed(flow_name: str) -> bool:
        fn = flow_name.lower()
        return not any(fn in an or an in fn for an in agent_names)

    with get_conn() as conn:
        for table in ("kg_elements", "kg_components", "kg_flows", "kg_features", "kg_visited_pages"):
            conn.execute(f"DELETE FROM {table} WHERE website_id=?", (website_id,))

        for page in getattr(kg, "pages", []):
            conn.execute(
                "INSERT INTO kg_visited_pages (website_id,url,title) VALUES (?,?,?)",
                (website_id, page.url, page.title),
            )

        for el in kg.elements:
            conn.execute(
                "INSERT INTO kg_elements (website_id,element_id,tag,text,selector,element_type,page_region,page_url) VALUES (?,?,?,?,?,?,?,?)",
                (website_id, el.id, el.tag, el.text, el.selector, el.element_type, el.page_region, getattr(el, "page_url", "")),
            )

        for comp in kg.components:
            conn.execute(
                "INSERT INTO kg_components (website_id,component_id,name,description,component_type) VALUES (?,?,?,?,?)",
                (website_id, comp.id, comp.name, comp.description, comp.component_type),
            )

        for feat in kg.features:
            cur = conn.execute(
                "INSERT INTO kg_features (website_id,feature_id,name,description) VALUES (?,?,?,?)",
                (website_id, feat.id, feat.name, feat.description),
            )
            feat_pk = cur.lastrowid

            for flow_id in feat.flow_ids:
                flow = next((f for f in kg.flows if f.id == flow_id), None)
                if flow:
                    missed = is_missed(flow.name)
                    conn.execute(
                        "INSERT INTO kg_flows (website_id,kg_feature_id,flow_id,name,description,missed_by_agent) VALUES (?,?,?,?,?,?)",
                        (website_id, feat_pk, flow.id, flow.name, flow.description, int(missed)),
                    )


def get_kg_data(website_id: int) -> dict:
    with get_conn() as conn:
        features = conn.execute(
            "SELECT * FROM kg_features WHERE website_id=?", (website_id,)
        ).fetchall()
        components = conn.execute(
            "SELECT * FROM kg_components WHERE website_id=?", (website_id,)
        ).fetchall()
        elements = conn.execute(
            "SELECT * FROM kg_elements WHERE website_id=?", (website_id,)
        ).fetchall()

        feature_list = []
        for feat in features:
            flows = conn.execute(
                "SELECT * FROM kg_flows WHERE kg_feature_id=?", (feat["id"],)
            ).fetchall()
            feature_list.append({
                "name": feat["name"],
                "description": feat["description"],
                "flows": [dict(f) for f in flows],
            })

        missed_count = conn.execute(
            "SELECT COUNT(*) FROM kg_flows WHERE website_id=? AND missed_by_agent=1", (website_id,)
        ).fetchone()[0]

        # Flat list of flows the agent missed, enriched with their feature name
        missed_flows = []
        for feat in feature_list:
            for flow in feat["flows"]:
                if flow.get("missed_by_agent"):
                    missed_flows.append({
                        "name": flow["name"],
                        "description": flow["description"],
                        "feature_name": feat["name"],
                    })

        visited_pages = conn.execute(
            "SELECT url, title FROM kg_visited_pages WHERE website_id=?", (website_id,)
        ).fetchall()

        return {
            "features": feature_list,
            "component_count": len(components),
            "element_count": len(elements),
            "flow_count": sum(len(f["flows"]) for f in feature_list),
            "missed_count": missed_count,
            "missed_flows": missed_flows,
            "visited_pages": [dict(r) for r in visited_pages],
            "page_count": len(visited_pages),
        }


# ── New State-Action schema persistence ────────────────────────────────────────

def save_state_graph(
    website_id: int,
    page,          # PageNode
    states,        # list[StateNode]
    elements,      # list[ElementNode]
    actions,       # list[ActionNode]
    transitions,   # list[StateTransition] (original model, for from/to ids)
    state_sig_map: dict,   # old state_id → signature
    action_map: dict,      # old elem_id → action_id
):
    with get_conn() as conn:
        for table in ("kg_pages", "kg_states", "kg_elements_v2", "kg_actions", "kg_state_transitions"):
            conn.execute(f"DELETE FROM {table} WHERE website_id=?", (website_id,))

        conn.execute(
            "INSERT INTO kg_pages (website_id, template_url, original_url, title) VALUES (?,?,?,?)",
            (website_id, page.template_url, page.original_url, page.title),
        )

        for s in states:
            conn.execute(
                """INSERT INTO kg_states
                   (website_id, signature, page_id, url_path, dom_hash, auth_flag, modal_flag, description)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (website_id, s.signature, s.page_id, s.url_path,
                 s.dom_hash, int(s.auth_flag), int(s.modal_flag), s.description),
            )

        for e in elements:
            conn.execute(
                """INSERT INTO kg_elements_v2
                   (website_id, selector_id, state_id, tag, text,
                    testid_selector, aria_selector, css_selector, xpath_selector,
                    selector_stability_score)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (website_id, e.selector_id, e.state_id, e.tag, e.text,
                 e.testid_selector, e.aria_selector, e.css_selector, e.xpath_selector,
                 e.selector_stability_score),
            )

        for a in actions:
            conn.execute(
                """INSERT INTO kg_actions
                   (website_id, action_id, verb, element_selector_id,
                    state_before_id, state_after_id, observed_count, last_seen, dom_diff_hash)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (website_id, a.id, a.verb, a.element_selector_id,
                 a.state_before_id, a.state_after_id, a.observed_count,
                 a.last_seen, a.dom_diff_hash),
            )

        for t in transitions:
            from_sig = state_sig_map.get(t.from_state_id, "")
            to_sig = state_sig_map.get(t.to_state_id, "")
            act_id = action_map.get(t.trigger_element_id, "")
            if from_sig and to_sig:
                conn.execute(
                    """INSERT INTO kg_state_transitions
                       (website_id, from_state_id, to_state_id, action_id)
                       VALUES (?,?,?,?)""",
                    (website_id, from_sig, to_sig, act_id),
                )


def get_state_graph(website_id: int) -> dict:
    with get_conn() as conn:
        page_row = conn.execute(
            "SELECT * FROM kg_pages WHERE website_id=?", (website_id,)
        ).fetchone()
        state_rows = conn.execute(
            "SELECT * FROM kg_states WHERE website_id=?", (website_id,)
        ).fetchall()
        elem_rows = conn.execute(
            "SELECT * FROM kg_elements_v2 WHERE website_id=?", (website_id,)
        ).fetchall()
        action_rows = conn.execute(
            "SELECT * FROM kg_actions WHERE website_id=?", (website_id,)
        ).fetchall()
        transition_rows = conn.execute(
            "SELECT * FROM kg_state_transitions WHERE website_id=?", (website_id,)
        ).fetchall()

        return {
            "page": dict(page_row) if page_row else {},
            "states": [dict(r) for r in state_rows],
            "elements": [dict(r) for r in elem_rows],
            "actions": [dict(r) for r in action_rows],
            "transitions": [dict(r) for r in transition_rows],
            "state_count": len(state_rows),
            "element_count": len(elem_rows),
            "action_count": len(action_rows),
            "transition_count": len(transition_rows),
        }
