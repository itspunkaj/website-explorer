import os
from neo4j import GraphDatabase
from .models import ExplorationResult, Flow, WebsiteKnowledgeGraph


def _get_driver():
    return GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]),
    )


# ── Clear ─────────────────────────────────────────────────────────────────────

def _clear_page(tx, url: str):
    tx.run(
        """
        MATCH (p:Page {url: $url})
        OPTIONAL MATCH (p)-[:HAS_FEATURE]->(feat:Feature)
        OPTIONAL MATCH (feat)-[:USES|CONTAINS_FLOW]->(flow:Flow)
        OPTIONAL MATCH (flow)-[:LEADS_TO]->(state:State)
        OPTIONAL MATCH (feat)-[:DEPENDS_ON]->(comp:Component)
        OPTIONAL MATCH (comp)-[:CONTAINS|HAS_ELEMENT]->(elem:Element)
        DETACH DELETE p, feat, flow, state, comp, elem
        """,
        url=url,
    )


# ── Page ──────────────────────────────────────────────────────────────────────

def _create_page(tx, url: str, title: str):
    tx.run(
        "MERGE (p:Page {url: $url}) SET p.title = $title",
        url=url, title=title,
    )


# ── Elements ──────────────────────────────────────────────────────────────────

def _create_elements(tx, elements):
    for el in elements:
        tx.run(
            """
            MERGE (e:Element {id: $id})
            SET e.tag = $tag,
                e.text = $text,
                e.selector = $selector,
                e.element_type = $element_type,
                e.page_region = $page_region,
                e.attributes = $attributes
            """,
            id=el.id, tag=el.tag, text=el.text,
            selector=el.selector, element_type=el.element_type,
            page_region=el.page_region, attributes=el.attributes,
        )


# ── Components — CONTAINS (Component → Element) ───────────────────────────────

def _create_components(tx, components):
    for comp in components:
        tx.run(
            """
            MERGE (c:Component {id: $id})
            SET c.name = $name,
                c.description = $description,
                c.component_type = $component_type
            """,
            id=comp.id, name=comp.name,
            description=comp.description, component_type=comp.component_type,
        )
        for elem_id in comp.element_ids:
            tx.run(
                """
                MATCH (c:Component {id: $comp_id})
                MATCH (e:Element {id: $elem_id})
                MERGE (c)-[:CONTAINS]->(e)
                """,
                comp_id=comp.id, elem_id=elem_id,
            )


# ── Flows — STEPS + TRIGGERS ──────────────────────────────────────────────────

def _create_flows(tx, flows):
    for flow in flows:
        tx.run(
            """
            MERGE (f:Flow {id: $id})
            SET f.name = $name, f.description = $description
            """,
            id=flow.id, name=flow.name, description=flow.description,
        )
        _create_flow_edges(tx, flow)


def _create_flow_edges(tx, flow: Flow):
    steps = sorted(flow.steps, key=lambda s: s.step_number)

    for step in steps:
        # STEPS (Flow → Element) — one edge per step, annotated with metadata
        tx.run(
            """
            MATCH (f:Flow {id: $flow_id})
            MATCH (e:Element {id: $elem_id})
            MERGE (f)-[r:STEPS {step_number: $step_number}]->(e)
            SET r.action = $action, r.description = $description
            """,
            flow_id=flow.id,
            elem_id=step.element_id,
            step_number=step.step_number,
            action=step.action,
            description=step.description,
        )

    # TRIGGERS (Element → Flow) — only the first step's element triggers the flow
    if steps:
        tx.run(
            """
            MATCH (e:Element {id: $elem_id})
            MATCH (f:Flow {id: $flow_id})
            MERGE (e)-[:TRIGGERS]->(f)
            """,
            elem_id=steps[0].element_id,
            flow_id=flow.id,
        )

    # STEPS (Flow → Component) — for each component involved in the flow
    for comp_id in flow.component_ids:
        tx.run(
            """
            MATCH (f:Flow {id: $flow_id})
            MATCH (c:Component {id: $comp_id})
            MERGE (f)-[:STEPS]->(c)
            """,
            flow_id=flow.id, comp_id=comp_id,
        )


# ── Features — USES + DEPENDS_ON ──────────────────────────────────────────────

def _create_features(tx, features, flows, page_url: str):
    # Build a quick lookup: flow_id → flow object
    flow_map = {f.id: f for f in flows}

    for feat in features:
        tx.run(
            """
            MERGE (f:Feature {id: $id})
            SET f.name = $name, f.description = $description
            """,
            id=feat.id, name=feat.name, description=feat.description,
        )
        # Page → Feature (keep HAS_FEATURE for top-level traversal)
        tx.run(
            """
            MATCH (p:Page {url: $url})
            MATCH (f:Feature {id: $feat_id})
            MERGE (p)-[:HAS_FEATURE]->(f)
            """,
            url=page_url, feat_id=feat.id,
        )
        # USES (Feature → Flow)
        for flow_id in feat.flow_ids:
            tx.run(
                """
                MATCH (feat:Feature {id: $feat_id})
                MATCH (flow:Flow {id: $flow_id})
                MERGE (feat)-[:USES]->(flow)
                """,
                feat_id=feat.id, flow_id=flow_id,
            )
        # DEPENDS_ON (Feature → Component) — derived from the flows' component lists
        comp_ids: set[str] = set()
        for flow_id in feat.flow_ids:
            flow = flow_map.get(flow_id)
            if flow:
                comp_ids.update(flow.component_ids)
        for comp_id in comp_ids:
            tx.run(
                """
                MATCH (feat:Feature {id: $feat_id})
                MATCH (comp:Component {id: $comp_id})
                MERGE (feat)-[:DEPENDS_ON]->(comp)
                """,
                feat_id=feat.id, comp_id=comp_id,
            )


# ── States — TRANSITIONS_TO + LEADS_TO ────────────────────────────────────────

def _create_states(tx, states):
    for state in states:
        tx.run(
            """
            MERGE (s:State {state_id: $state_id})
            SET s.url = $url,
                s.title = $title,
                s.dom_hash = $dom_hash,
                s.visible_element_count = $visible_element_count,
                s.description = $description
            """,
            state_id=state.state_id,
            url=state.url,
            title=state.title,
            dom_hash=state.dom_hash,
            visible_element_count=state.visible_element_count,
            description=state.description,
        )


def _create_state_transitions(tx, transitions):
    # TRANSITIONS_TO (State → State)
    for t in transitions:
        tx.run(
            """
            MATCH (from:State {state_id: $from_id})
            MATCH (to:State {state_id: $to_id})
            MERGE (from)-[r:TRANSITIONS_TO]->(to)
            SET r.trigger_element_id = $trigger_elem,
                r.trigger_action = $trigger_action
            """,
            from_id=t.from_state_id,
            to_id=t.to_state_id,
            trigger_elem=t.trigger_element_id,
            trigger_action=t.trigger_action,
        )


def _create_flow_state_edges(tx, flows, exploration: ExplorationResult):
    # Build map: element_id → state_after_id (from last recorded action on that element)
    elem_to_state: dict[str, str] = {}
    for log in exploration.action_logs:
        elem_to_state[log.element_id] = log.state_after_id

    # LEADS_TO (Flow → State) — determined by the last step's resulting state
    for flow in flows:
        if not flow.steps:
            continue
        last_step = max(flow.steps, key=lambda s: s.step_number)
        state_id = elem_to_state.get(last_step.element_id)
        if state_id:
            tx.run(
                """
                MATCH (f:Flow {id: $flow_id})
                MATCH (s:State {state_id: $state_id})
                MERGE (f)-[:LEADS_TO]->(s)
                """,
                flow_id=flow.id, state_id=state_id,
            )


# ── Public entry point ────────────────────────────────────────────────────────

def ingest_to_neo4j(kg: WebsiteKnowledgeGraph, exploration: ExplorationResult | None = None):
    driver = _get_driver()
    with driver.session() as session:
        session.execute_write(_clear_page, kg.url)
        session.execute_write(_create_page, kg.url, kg.page_title)
        session.execute_write(_create_elements, kg.elements)
        session.execute_write(_create_components, kg.components)
        session.execute_write(_create_flows, kg.flows)
        session.execute_write(_create_features, kg.features, kg.flows, kg.url)

        if exploration:
            session.execute_write(_create_states, exploration.states)
            session.execute_write(_create_state_transitions, exploration.state_transitions)
            session.execute_write(_create_flow_state_edges, kg.flows, exploration)

    driver.close()
    print(f"Neo4j: graph ingested for {kg.url}")
