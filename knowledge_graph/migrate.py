"""
Migration script: old containment graph → new State-Action schema.

Steps:
  1. Export entire existing Neo4j graph to a timestamped JSON backup.
  2. Drop old containment nodes/edges (Page, Feature, Flow, Component, Element,
     State and their relationships) that belong to the old schema.
  3. Apply new constraints and indexes via setup_schema().

Run once before the first ingestion with the new pipeline:
  python -m knowledge_graph.migrate
"""

import json
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from .neo4j_client import _get_driver, setup_schema


# ── Export ────────────────────────────────────────────────────────────────────

def export_old_graph(driver) -> dict:
    """Read every node and relationship and return as plain dicts."""
    with driver.session() as session:
        node_rows = session.run(
            "MATCH (n) RETURN labels(n) AS labels, properties(n) AS props"
        )
        nodes = [
            {"labels": list(r["labels"]), "props": dict(r["props"])}
            for r in node_rows
        ]

        rel_rows = session.run(
            """
            MATCH (a)-[r]->(b)
            RETURN type(r)          AS type,
                   properties(r)    AS props,
                   labels(a)        AS start_labels,
                   properties(a)    AS start_props,
                   labels(b)        AS end_labels,
                   properties(b)    AS end_props
            """
        )
        relationships = [
            {
                "type": r["type"],
                "props": dict(r["props"]),
                "start": {"labels": list(r["start_labels"]), "props": dict(r["start_props"])},
                "end": {"labels": list(r["end_labels"]), "props": dict(r["end_props"])},
            }
            for r in rel_rows
        ]

    return {"nodes": nodes, "relationships": relationships}


# ── Drop old containment graph ────────────────────────────────────────────────

_OLD_REL_TYPES = [
    "HAS_FEATURE",   # Page → Feature
    "USES",          # Feature → Flow
    "STEPS",         # Flow → Element / Component
    "TRIGGERS",      # Element → Flow
    "CONTAINS",      # Component → Element
    "LEADS_TO",      # Flow → State
    # Old DEPENDS_ON was Feature → Component; keep rel type name but drop old instances
    "DEPENDS_ON",
]

_OLD_NODE_LABELS = [
    "Feature",
    "Flow",
    "Component",
    "Element",
    "State",
    "Page",
]


def drop_old_containment_graph(driver):
    """
    Remove all nodes/edges that were written by the old containment pipeline.
    New State nodes (keyed on .signature) and new Page nodes (keyed on
    .template_url) do not exist yet, so this is a full wipe of those labels.
    """
    with driver.session() as session:
        # Delete old relationship types first (avoids constraint violations)
        for rel in _OLD_REL_TYPES:
            session.run(f"MATCH ()-[r:{rel}]->() DELETE r")

        # Delete old TRANSITIONS_TO edges (old schema stored only trigger_element_id/action)
        session.run("MATCH ()-[r:TRANSITIONS_TO]->() DELETE r")

        # Delete all old-schema node labels
        for label in _OLD_NODE_LABELS:
            session.run(f"MATCH (n:{label}) DETACH DELETE n")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    driver = _get_driver()

    print("Step 1/3 — Exporting existing graph as JSON backup…")
    backup = export_old_graph(driver)
    backup_path = Path(f"graph_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    backup_path.write_text(json.dumps(backup, indent=2))
    print(
        f"  Saved: {backup_path} "
        f"({len(backup['nodes'])} nodes, {len(backup['relationships'])} edges)"
    )

    print("Step 2/3 — Dropping old containment graph…")
    drop_old_containment_graph(driver)
    print("  Done.")

    print("Step 3/3 — Applying new schema constraints and indexes…")
    setup_schema(driver)

    driver.close()
    print(
        "\nMigration complete. "
        "Re-run the exploration pipeline to populate the new State-Action graph."
    )


if __name__ == "__main__":
    main()
