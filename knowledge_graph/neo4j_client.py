import hashlib
import json
import os
import re
import time
from urllib.parse import parse_qs, urlencode, urlparse

from neo4j import GraphDatabase

from .models import (
    ActionNode, ElementNode, ExplorationResult,
    PageNode, StateNode, WebsiteKnowledgeGraph,
)

_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "msclkid", "_ga", "ref",
})


def _get_driver():
    return GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]),
    )


# ── URL helpers ───────────────────────────────────────────────────────────────

def _canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    path = re.sub(r"/\d+(?=/|$)", "/{id}", parsed.path)
    path = re.sub(
        r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?=/|$)",
        "/{id}", path, flags=re.I,
    )
    if parsed.query:
        params = {k: v for k, v in parse_qs(parsed.query).items()
                  if k not in _TRACKING_PARAMS}
        query = urlencode(params, doseq=True)
    else:
        query = ""
    base = f"{parsed.scheme}://{parsed.netloc}{path}"
    return f"{base}?{query}" if query else base


def _page_id(template_url: str) -> str:
    return hashlib.sha256(template_url.encode()).hexdigest()[:16]


# ── State helpers ─────────────────────────────────────────────────────────────

def _state_signature(url_path: str, dom_hash: str, auth_flag: bool, modal_flag: bool) -> str:
    raw = f"{url_path}|{dom_hash}|{int(auth_flag)}|{int(modal_flag)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


# ── Element selector helpers ──────────────────────────────────────────────────

def _selector_info(attrs: dict, selector: str, xpath: str = "") -> tuple[str, str, str, float]:
    """Return (testid_sel, aria_sel, best_selector, stability_score)."""
    testid = attrs.get("data-testid", "")
    role = attrs.get("role", "")
    aria = attrs.get("aria-label", "")

    if testid:
        ts = f'[data-testid="{testid}"]'
        return ts, "", ts, 1.0
    if role and aria:
        ar = f'[role="{role}"][aria-label="{aria}"]'
        return "", ar, ar, 0.8
    if selector and not selector.startswith("//"):
        return "", "", selector, 0.5
    if xpath:
        return "", "", xpath, 0.2
    return "", "", selector or "", 0.5


def _selector_id(selector: str) -> str:
    return hashlib.sha256(selector.encode()).hexdigest()[:16]


# ── Schema setup (constraints + indexes) ─────────────────────────────────────

def setup_schema(driver=None):
    """Apply unique constraints and indexes for the new State-Action schema."""
    _close = driver is None
    if _close:
        driver = _get_driver()
    try:
        with driver.session() as session:
            for cypher in [
                "CREATE CONSTRAINT IF NOT EXISTS FOR (p:Page) REQUIRE p.template_url IS UNIQUE",
                "CREATE CONSTRAINT IF NOT EXISTS FOR (s:State) REQUIRE s.signature IS UNIQUE",
                "CREATE CONSTRAINT IF NOT EXISTS FOR (e:Element) REQUIRE e.selector_id IS UNIQUE",
                "CREATE CONSTRAINT IF NOT EXISTS FOR (a:Action) REQUIRE a.id IS UNIQUE",
                "CREATE INDEX IF NOT EXISTS FOR (s:State) ON (s.page_id)",
                "CREATE INDEX IF NOT EXISTS FOR (e:Element) ON (e.state_id)",
                "CREATE INDEX IF NOT EXISTS FOR ()-[r:TRANSITIONS_TO]-() ON (r.observed_count)",
            ]:
                session.run(cypher)
        print("Neo4j: schema constraints and indexes applied")
    finally:
        if _close:
            driver.close()


# ── Clear ─────────────────────────────────────────────────────────────────────

def _clear_page(tx, template_url: str):
    tx.run(
        """
        MATCH (p:Page {template_url: $template_url})
        OPTIONAL MATCH (p)-[:HAS_STATE]->(s:State)
        OPTIONAL MATCH (s)-[:HAS_ELEMENT]->(e:Element)
        OPTIONAL MATCH (e)-[:PART_OF]->(comp:Component)
        OPTIONAL MATCH (comp)-[:BELONGS_TO]->(feat:Feature)
        DETACH DELETE p, s, e, comp, feat
        """,
        template_url=template_url,
    )


# ── Page ──────────────────────────────────────────────────────────────────────

def _create_page(tx, page: PageNode):
    tx.run(
        """
        MERGE (p:Page {template_url: $template_url})
        SET p.original_url = $original_url, p.title = $title
        """,
        template_url=page.template_url,
        original_url=page.original_url,
        title=page.title,
    )


# ── States ────────────────────────────────────────────────────────────────────

def _create_states(tx, state_nodes: list[StateNode], template_url: str):
    for s in state_nodes:
        tx.run(
            """
            MERGE (st:State {signature: $signature})
            SET st.page_id       = $page_id,
                st.url_path      = $url_path,
                st.dom_hash      = $dom_hash,
                st.auth_flag     = $auth_flag,
                st.modal_flag    = $modal_flag,
                st.description   = $description
            WITH st
            MATCH (p:Page {template_url: $template_url})
            MERGE (p)-[:HAS_STATE]->(st)
            """,
            signature=s.signature,
            page_id=s.page_id,
            url_path=s.url_path,
            dom_hash=s.dom_hash,
            auth_flag=s.auth_flag,
            modal_flag=s.modal_flag,
            description=s.description,
            template_url=template_url,
        )


# ── Elements ──────────────────────────────────────────────────────────────────

def _create_elements(tx, elem_nodes: list[ElementNode]):
    for e in elem_nodes:
        tx.run(
            """
            MERGE (el:Element {selector_id: $selector_id})
            SET el.state_id                  = $state_id,
                el.tag                       = $tag,
                el.text                      = $text,
                el.testid_selector           = $testid_selector,
                el.aria_selector             = $aria_selector,
                el.css_selector              = $css_selector,
                el.xpath_selector            = $xpath_selector,
                el.selector_stability_score  = $score
            WITH el
            MATCH (s:State {signature: $state_id})
            MERGE (s)-[:HAS_ELEMENT]->(el)
            """,
            selector_id=e.selector_id,
            state_id=e.state_id,
            tag=e.tag,
            text=e.text,
            testid_selector=e.testid_selector,
            aria_selector=e.aria_selector,
            css_selector=e.css_selector,
            xpath_selector=e.xpath_selector,
            score=e.selector_stability_score,
        )


# ── Components ────────────────────────────────────────────────────────────────

def _create_components(tx, components, elem_selector_map: dict[str, str]):
    """Write Component nodes. elem_selector_map: old elem_id → selector_id."""
    for comp in components:
        tx.run(
            """
            MERGE (c:Component {id: $id})
            SET c.name = $name, c.description = $description, c.component_type = $component_type
            """,
            id=comp.id, name=comp.name,
            description=comp.description, component_type=comp.component_type,
        )
        for elem_id in comp.element_ids:
            sel_id = elem_selector_map.get(elem_id)
            if not sel_id:
                continue
            tx.run(
                """
                MATCH (el:Element {selector_id: $sel_id})
                MATCH (c:Component {id: $comp_id})
                MERGE (el)-[:PART_OF]->(c)
                """,
                sel_id=sel_id, comp_id=comp.id,
            )


# ── Features ──────────────────────────────────────────────────────────────────

def _create_features(tx, features, flows):
    flow_to_comps: dict[str, list[str]] = {f.id: f.component_ids for f in flows}
    for feat in features:
        tx.run(
            """
            MERGE (f:Feature {id: $id})
            SET f.name = $name, f.description = $description
            """,
            id=feat.id, name=feat.name, description=feat.description,
        )
        comp_ids: set[str] = set()
        for flow_id in feat.flow_ids:
            comp_ids.update(flow_to_comps.get(flow_id, []))
        for comp_id in comp_ids:
            tx.run(
                """
                MATCH (c:Component {id: $comp_id})
                MATCH (f:Feature {id: $feat_id})
                MERGE (c)-[:BELONGS_TO]->(f)
                """,
                comp_id=comp_id, feat_id=feat.id,
            )


# ── Flows ─────────────────────────────────────────────────────────────────────

def _create_flows(tx, flows, elem_selector_map: dict[str, str]):
    for flow in flows:
        tx.run(
            """
            MERGE (f:Flow {id: $id})
            SET f.name = $name, f.description = $description
            """,
            id=flow.id, name=flow.name, description=flow.description,
        )
        for step in sorted(flow.steps, key=lambda s: s.step_number):
            sel_id = elem_selector_map.get(step.element_id)
            if not sel_id:
                continue
            tx.run(
                """
                MATCH (f:Flow {id: $flow_id})
                OPTIONAL MATCH (a:Action {element_selector_id: $sel_id})
                FOREACH (_ IN CASE WHEN a IS NOT NULL THEN [1] ELSE [] END |
                    MERGE (f)-[r:CONTAINS {order: $order}]->(a)
                )
                """,
                flow_id=flow.id,
                sel_id=sel_id,
                order=step.step_number,
            )


# ── Actions ───────────────────────────────────────────────────────────────────

def _create_actions(tx, action_nodes: list[ActionNode]):
    for act in action_nodes:
        tx.run(
            """
            MERGE (a:Action {id: $id})
            SET a.verb                  = $verb,
                a.element_selector_id   = $sel_id,
                a.state_before_id       = $before,
                a.state_after_id        = $after,
                a.observed_count        = $count,
                a.last_seen             = $last_seen,
                a.dom_diff_hash         = $diff_hash
            WITH a
            OPTIONAL MATCH (el:Element {selector_id: $sel_id})
            FOREACH (_ IN CASE WHEN el IS NOT NULL THEN [1] ELSE [] END |
                MERGE (a)-[:PERFORMED_ON]->(el)
            )
            WITH a
            OPTIONAL MATCH (sb:State {signature: $before})
            FOREACH (_ IN CASE WHEN sb IS NOT NULL THEN [1] ELSE [] END |
                MERGE (a)-[:REQUIRES]->(sb)
            )
            WITH a
            OPTIONAL MATCH (sa:State {signature: $after})
            FOREACH (_ IN CASE WHEN sa IS NOT NULL THEN [1] ELSE [] END |
                MERGE (a)-[:CAUSES]->(sa)
            )
            """,
            id=act.id,
            verb=act.verb,
            sel_id=act.element_selector_id,
            before=act.state_before_id,
            after=act.state_after_id,
            count=act.observed_count,
            last_seen=act.last_seen,
            diff_hash=act.dom_diff_hash,
        )


# ── TRANSITIONS_TO ────────────────────────────────────────────────────────────

def _create_transitions(tx, transitions, state_sig_map: dict[str, str], action_map: dict[str, str]):
    """
    state_sig_map: old state_id (e.g. state_001) → new signature
    action_map: old element_id → action node id
    """
    now = time.time()
    for t in transitions:
        from_sig = state_sig_map.get(t.from_state_id)
        to_sig = state_sig_map.get(t.to_state_id)
        if not (from_sig and to_sig):
            continue
        act_id = action_map.get(t.trigger_element_id, "")
        diff_hash = hashlib.md5(f"{from_sig}->{to_sig}".encode()).hexdigest()[:12]
        tx.run(
            """
            MATCH (from:State {signature: $from_sig})
            MATCH (to:State {signature: $to_sig})
            MERGE (from)-[r:TRANSITIONS_TO]->(to)
            ON CREATE SET r.observed_count = 1,
                          r.last_seen      = $now,
                          r.action_id      = $act_id,
                          r.probability    = 1.0,
                          r.dom_diff_hash  = $diff_hash
            ON MATCH  SET r.observed_count = r.observed_count + 1,
                          r.last_seen      = $now,
                          r.action_id      = $act_id
            """,
            from_sig=from_sig,
            to_sig=to_sig,
            act_id=act_id,
            now=now,
            diff_hash=diff_hash,
        )


# ── Public entry point ────────────────────────────────────────────────────────

def ingest_to_neo4j(kg: WebsiteKnowledgeGraph, exploration: ExplorationResult | None = None):
    driver = _get_driver()

    template_url = _canonicalize_url(kg.url)
    pid = _page_id(template_url)
    page = PageNode(template_url=template_url, original_url=kg.url, title=kg.page_title)

    # ── Build StateNode list ──────────────────────────────────────────────────
    state_nodes: list[StateNode] = []
    state_sig_map: dict[str, str] = {}  # old state_id → new signature

    if exploration and exploration.states:
        for s in exploration.states:
            parsed = urlparse(s.url)
            sig = _state_signature(parsed.path, s.dom_hash, False, False)
            state_sig_map[s.state_id] = sig
            state_nodes.append(StateNode(
                signature=sig,
                page_id=pid,
                url_path=parsed.path,
                dom_hash=s.dom_hash,
                description=s.description,
            ))
    else:
        parsed = urlparse(kg.url)
        sig = _state_signature(parsed.path, "", False, False)
        state_nodes.append(StateNode(
            signature=sig, page_id=pid,
            url_path=parsed.path, dom_hash="",
            description=kg.page_title,
        ))
        state_sig_map["state_001"] = sig

    initial_sig = state_nodes[0].signature

    # Build a url_path → state signature map for assigning elements to their page state
    path_to_sig: dict[str, str] = {s.url_path: s.signature for s in state_nodes}

    # ── Build ElementNode list + elem_id → selector_id map ───────────────────
    elem_selector_map: dict[str, str] = {}  # old elem_id → selector_id
    elem_nodes: list[ElementNode] = []

    for el in kg.elements:
        if isinstance(el.attributes, str):
            try:
                attrs = json.loads(el.attributes)
            except Exception:
                attrs = {}
        else:
            attrs = el.attributes or {}

        testid_sel, aria_sel, best_sel, score = _selector_info(attrs, el.selector)
        sel_id = _selector_id(best_sel or el.id)
        elem_selector_map[el.id] = sel_id

        # Assign element to the state matching its source page, fallback to initial
        el_page_url = getattr(el, "page_url", "") or kg.url
        el_path = urlparse(el_page_url).path
        assigned_sig = path_to_sig.get(el_path, initial_sig)

        elem_nodes.append(ElementNode(
            selector_id=sel_id,
            state_id=assigned_sig,
            tag=el.tag,
            text=el.text,
            testid_selector=testid_sel,
            aria_selector=aria_sel,
            css_selector=el.selector,
            xpath_selector="",
            selector_stability_score=score,
            attributes=attrs,
        ))

    # ── Build ActionNode list ─────────────────────────────────────────────────
    action_nodes: list[ActionNode] = []
    action_map: dict[str, str] = {}  # old elem_id → action id

    if exploration:
        for i, log in enumerate(exploration.action_logs):
            act_id = f"act_{str(i + 1).zfill(4)}"
            from_sig = state_sig_map.get(log.state_before_id, "")
            to_sig = state_sig_map.get(log.state_after_id, "")
            sel_id = elem_selector_map.get(log.element_id, "")
            diff = hashlib.md5(f"{from_sig}->{to_sig}".encode()).hexdigest()[:12]
            action_nodes.append(ActionNode(
                id=act_id,
                verb=log.action,
                element_selector_id=sel_id,
                state_before_id=from_sig,
                state_after_id=to_sig,
                last_seen=log.timestamp,
                dom_diff_hash=diff,
            ))
            action_map[log.element_id] = act_id

    # ── Write to Neo4j ────────────────────────────────────────────────────────
    with driver.session() as session:
        session.execute_write(_clear_page, template_url)
        session.execute_write(_create_page, page)
        session.execute_write(_create_states, state_nodes, template_url)
        session.execute_write(_create_elements, elem_nodes)
        session.execute_write(_create_components, kg.components, elem_selector_map)
        session.execute_write(_create_features, kg.features, kg.flows)
        session.execute_write(_create_flows, kg.flows, elem_selector_map)

        if exploration:
            session.execute_write(_create_actions, action_nodes)
            session.execute_write(
                _create_transitions,
                exploration.state_transitions,
                state_sig_map,
                action_map,
            )

    driver.close()
    print(f"Neo4j: State-Action graph ingested for {kg.url} → template: {template_url}")
    return {
        "page": page,
        "state_nodes": state_nodes,
        "elem_nodes": elem_nodes,
        "action_nodes": action_nodes,
        "state_sig_map": state_sig_map,
        "elem_selector_map": elem_selector_map,
        "action_map": action_map,
    }
