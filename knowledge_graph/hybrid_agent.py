import json
import os
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()
from .models import (
    DOMElement, DOMExtractionResult, Element,
    ExplorationResult, WebsiteKnowledgeGraph,
)

_SYSTEM_PROMPT = """You are a UI analyst. You receive structured data extracted by a real multi-page browser session (Playwright) and must produce a knowledge graph of the entire website's structure.

Output valid JSON matching this exact schema:
{
  "url": "<string — root URL of the website>",
  "page_title": "<string — title of the root/home page>",
  "pages": [
    {
      "url": "<page URL>",
      "title": "<page title>",
      "element_ids": ["elem_001"]
    }
  ],
  "elements": [
    {
      "id": "<elem_XXX — use exact IDs from the input data>",
      "tag": "<html tag>",
      "text": "<visible label or placeholder>",
      "selector": "<CSS selector from input>",
      "element_type": "<button|link|input|text|image|nav|form|section|icon>",
      "page_region": "<header|hero|main|footer|sidebar|modal|nav>",
      "page_url": "<URL of the page this element belongs to>",
      "attributes": "<JSON string of relevant attributes>"
    }
  ],
  "components": [
    {
      "id": "comp_001",
      "name": "<human-readable name>",
      "description": "<what this component does>",
      "component_type": "<navigation|form|hero|section|footer|cta|card|modal|banner>",
      "element_ids": ["elem_001", "elem_002"]
    }
  ],
  "flows": [
    {
      "id": "flow_001",
      "name": "<short name e.g. Complete Checkout>",
      "description": "<end-to-end description, may span multiple pages>",
      "component_ids": ["comp_001"],
      "steps": [
        { "step_number": 1, "element_id": "elem_001", "action": "<click|type|hover|scroll|submit|focus|select>", "description": "<what the user does, include page context if navigating>" }
      ]
    }
  ],
  "features": [
    {
      "id": "feat_001",
      "name": "<high-level capability>",
      "description": "<what this feature lets the user do>",
      "flow_ids": ["flow_001"]
    }
  ]
}

STRICT RULES:
- Only reference element IDs that exist in the provided interactive_elements list.
- Base flows on actual observed state transitions and action logs — do not invent interactions.
- Flows may span multiple pages; model cross-page navigation steps explicitly.
- element_type must be one of: button | link | input | text | image | nav | form | section | icon
- page_region must be one of: header | hero | main | footer | sidebar | modal | nav
- component_type must be one of: navigation | form | hero | section | footer | cta | card | modal | banner
- action must be one of: click | type | hover | scroll | submit | focus | select
- attributes field must be a JSON-encoded string, not an object.
- page_url on each element must match an actual visited page URL from the input."""

_TASK_TEMPLATE = """Analyze this multi-page website and build a complete knowledge graph.

Root URL: {url}
Home Page Title: {page_title}
Pages Visited: {page_count}

── PAGES VISITED ─────────────────────────────────────────────────────────────
{pages_json}

── INTERACTIVE ELEMENTS ({elem_count} visible across all pages) ──────────────
{elements_json}

── ACTION LOGS (observed interactions) ──────────────────────────────────────
{action_logs_json}

── STATE TRANSITIONS (meaningful DOM/URL changes) ────────────────────────────
{transitions_json}

Instructions:
1. Include ALL interactive elements in the elements list (use exact elem_ids from above); set page_url on each element.
2. Populate the pages list with all visited URLs and their element IDs.
3. Group elements into logical components (shared components like nav/footer can appear on multiple pages).
4. Create flows grounded in the observed transitions — cross-page flows should chain navigation steps.
5. Group flows into high-level features."""

_VALID_ELEMENT_TYPES = {"button", "link", "input", "text", "image", "nav", "form", "section", "icon"}
_VALID_REGIONS = {"header", "hero", "main", "footer", "sidebar", "modal", "nav"}
_VALID_ACTIONS = {"click", "type", "hover", "scroll", "submit", "focus", "select"}
_VALID_COMP_TYPES = {"navigation", "form", "hero", "section", "footer", "cta", "card", "modal", "banner"}


def _infer_element_type(el: DOMElement) -> str:
    tag = el.tag
    role = el.attributes.get("role", "")
    input_type = el.attributes.get("type", "")
    if tag == "a" or role == "link":
        return "link"
    if tag in ("input", "textarea", "select"):
        return "input"
    if tag == "img":
        return "image"
    if tag == "nav" or role == "navigation":
        return "nav"
    if tag == "form":
        return "form"
    if role == "button" or tag == "button" or input_type in ("submit", "button", "reset"):
        return "button"
    return "button"


def _parse_element(raw: dict, elem_id_map: dict[str, DOMElement]) -> Element:
    elem_id = raw.get("id", "")
    # If the LLM referenced a real DOM element, use its real selector
    if elem_id in elem_id_map:
        dom_el = elem_id_map[elem_id]
        raw.setdefault("selector", dom_el.selector)
        raw.setdefault("tag", dom_el.tag)
        raw.setdefault("text", dom_el.text)

    # Normalise attributes to JSON string
    attrs = raw.get("attributes", {})
    if isinstance(attrs, dict):
        attrs = json.dumps(attrs)
    raw["attributes"] = attrs

    # Validate / fix controlled vocabulary fields
    if raw.get("element_type") not in _VALID_ELEMENT_TYPES:
        dom_el = elem_id_map.get(elem_id)
        raw["element_type"] = _infer_element_type(dom_el) if dom_el else "button"
    if raw.get("page_region") not in _VALID_REGIONS:
        dom_el = elem_id_map.get(elem_id)
        raw["page_region"] = dom_el.page_region if dom_el else "main"

    allowed = set(Element.model_fields.keys())
    return Element(**{k: v for k, v in raw.items() if k in allowed})


def _elements_from_dom(dom_result: DOMExtractionResult) -> list[dict]:
    """Fallback: build element list directly from DOM extraction (no LLM)."""
    return [
        {
            "id": el.elem_id,
            "tag": el.tag,
            "text": el.text,
            "selector": el.selector,
            "element_type": _infer_element_type(el),
            "page_region": el.page_region,
            "attributes": json.dumps({k: v for k, v in el.attributes.items() if v}),
        }
        for el in dom_result.interactive_elements
        if el.is_visible
    ]


async def run_hybrid_agent(
    dom_result: DOMExtractionResult,
    exploration_result: ExplorationResult,
) -> WebsiteKnowledgeGraph:
    client = AsyncOpenAI()

    visible = [el for el in dom_result.interactive_elements if el.is_visible]
    elem_id_map: dict[str, DOMElement] = {el.elem_id: el for el in visible}

    # Build compact representations for the prompt
    elements_data = [
        {
            "elem_id": el.elem_id,
            "tag": el.tag,
            "text": el.text,
            "selector": el.selector,
            "page_region": el.page_region,
            "attrs": {k: v for k, v in el.attributes.items() if v},
            "listeners": el.event_listeners,
        }
        for el in visible
    ]

    action_data = [
        {
            "elem_id": log.element_id,
            "action": log.action,
            "url_changed": log.url_before != log.url_after,
            "url_after": log.url_after if log.url_before != log.url_after else None,
            "dom_mutations": log.mutations_count,
            "new_elements": log.new_elements_added[:4],
            "api_calls": log.network_calls[:3],
        }
        for log in exploration_result.action_logs
    ]

    transition_data = [
        {
            "trigger_elem": t.trigger_element_id,
            "action": t.trigger_action,
            "from": t.from_state_id,
            "to": t.to_state_id,
        }
        for t in exploration_result.state_transitions
    ]

    elements_json = json.dumps(elements_data, indent=2)
    action_logs_json = json.dumps(action_data, indent=2)
    transitions_json = json.dumps(transition_data, indent=2)

    # Keep total data under ~14k chars to fit context comfortably
    if len(elements_json) > 7_000:
        elements_json = elements_json[:7_000] + "\n... (truncated)"
    if len(action_logs_json) > 5_000:
        action_logs_json = action_logs_json[:5_000] + "\n... (truncated)"
    if len(transitions_json) > 2_000:
        transitions_json = transitions_json[:2_000] + "\n... (truncated)"

    # Build pages list from visited URLs tracked in dom_result / exploration_result
    visited_urls: list[str] = getattr(dom_result, "visited_urls", None) or [dom_result.url]
    pages_data = [
        {
            "url": page_url,
            "title": dom_result.page_title if page_url == dom_result.url else page_url,
            "element_ids": [
                el.elem_id for el in visible
                if getattr(el, "page_url", dom_result.url) == page_url
            ],
        }
        for page_url in visited_urls
    ]
    pages_json = json.dumps(pages_data, indent=2)
    if len(pages_json) > 2_000:
        pages_json = pages_json[:2_000] + "\n... (truncated)"

    task = _TASK_TEMPLATE.format(
        url=dom_result.url,
        page_title=dom_result.page_title,
        page_count=len(visited_urls),
        pages_json=pages_json,
        elem_count=len(elements_data),
        elements_json=elements_json,
        action_logs_json=action_logs_json,
        transitions_json=transitions_json,
    )

    response = await client.chat.completions.create(
        model="gpt-4.1",
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )

    raw = json.loads(response.choices[0].message.content)

    # Build elements: prefer LLM output enriched with real DOM data; fall back to
    # pure DOM data if the LLM returned nothing useful.
    raw_elements = raw.get("elements") or []
    if not raw_elements:
        raw_elements = _elements_from_dom(dom_result)

    parsed_elements = [_parse_element(e, elem_id_map) for e in raw_elements]

    return WebsiteKnowledgeGraph.model_validate({
        "url": raw.get("url") or dom_result.url,
        "page_title": raw.get("page_title") or dom_result.page_title,
        "pages": raw.get("pages") or pages_data,
        "elements": [e.model_dump() for e in parsed_elements],
        "components": raw.get("components") or [],
        "flows": raw.get("flows") or [],
        "features": raw.get("features") or [],
    })
