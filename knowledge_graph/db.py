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
                page_region TEXT
            );
        """)


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
        for table in ("kg_elements", "kg_components", "kg_flows", "kg_features"):
            conn.execute(f"DELETE FROM {table} WHERE website_id=?", (website_id,))

        for el in kg.elements:
            conn.execute(
                "INSERT INTO kg_elements (website_id,element_id,tag,text,selector,element_type,page_region) VALUES (?,?,?,?,?,?,?)",
                (website_id, el.id, el.tag, el.text, el.selector, el.element_type, el.page_region),
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

        return {
            "features": feature_list,
            "component_count": len(components),
            "element_count": len(elements),
            "flow_count": sum(len(f["flows"]) for f in feature_list),
            "missed_count": missed_count,
        }
