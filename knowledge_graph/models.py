from pydantic import BaseModel, Field


class Element(BaseModel):
    id: str = Field(description="Unique ID e.g. elem_001")
    tag: str = Field(description="HTML tag: button, input, a, div, h1, img, etc.")
    text: str = Field(description="Visible label, placeholder, or alt text")
    selector: str = Field(description="CSS selector to locate this element")
    element_type: str = Field(description="One of: button | link | input | text | image | nav | form | section | icon")
    page_region: str = Field(description="One of: header | hero | main | footer | sidebar | modal | nav")
    attributes: str = Field(default="", description='Relevant HTML attributes as a JSON string, e.g. {"href": "/about", "type": "button", "aria-label": "Close"}')


class Component(BaseModel):
    id: str = Field(description="Unique ID e.g. comp_001")
    name: str = Field(description="Human-readable name e.g. 'Navigation Bar', 'Hero Section', 'Contact Form'")
    description: str = Field(description="What this component does on the page")
    component_type: str = Field(description="One of: navigation | form | hero | section | footer | cta | card | modal | banner")
    element_ids: list[str] = Field(description="IDs of elements that belong to this component")


class FlowStep(BaseModel):
    step_number: int = Field(description="1-based step index within the flow")
    element_id: str = Field(description="ID of the element involved in this step")
    action: str = Field(description="One of: click | type | hover | scroll | submit | focus | select")
    description: str = Field(description="What the user does in this step")


class Flow(BaseModel):
    id: str = Field(description="Unique ID e.g. flow_001")
    name: str = Field(description="Short name e.g. 'Contact Form Submission', 'Menu Navigation'")
    description: str = Field(description="End-to-end description of this user flow")
    component_ids: list[str] = Field(description="IDs of components involved in this flow")
    steps: list[FlowStep] = Field(description="Ordered steps the user takes to complete this flow")


class Feature(BaseModel):
    id: str = Field(description="Unique ID e.g. feat_001")
    name: str = Field(description="Broad capability name e.g. 'Contact', 'Navigation', 'Social Links'")
    description: str = Field(description="What this feature enables the user to do")
    flow_ids: list[str] = Field(description="IDs of flows that implement this feature")


class AgentFlow(BaseModel):
    name: str = Field(description="Short flow name e.g. 'Submit Contact Form'")
    description: str = Field(description="What this flow achieves for the user")
    steps: list[str] = Field(description="Plain-language ordered steps e.g. ['Click CTA', 'Fill email', 'Submit']")
    test_cases: list[str] = Field(description="2-3 test scenarios e.g. ['Valid submission', 'Empty required field']")


class AgentExploration(BaseModel):
    url: str = Field(description="The URL explored")
    page_title: str = Field(description="Page title or main heading")
    summary: str = Field(description="1-2 sentence overview of what the page is and does")
    flows: list[AgentFlow] = Field(description="All user flows found on the page")
    interactions: list[str] = Field(description="Flat list of every interactive element found e.g. ['Nav: Home link', 'Button: Get Started', 'Input: Email field']")


class WebsiteKnowledgeGraph(BaseModel):
    url: str = Field(description="The URL of the page explored")
    page_title: str = Field(description="The <title> or main heading of the page")
    elements: list[Element] = Field(description="All individual UI elements found on the page")
    components: list[Component] = Field(description="Logical groupings of elements into components")
    flows: list[Flow] = Field(description="User interaction sequences from start to completion")
    features: list[Feature] = Field(description="Broad capabilities grouping one or more flows")


# ── Playwright / Hybrid pipeline models ───────────────────────────────────────

class DOMElement(BaseModel):
    elem_id: str = Field(description="Generated ID e.g. elem_001")
    tag: str = Field(description="HTML tag name")
    text: str = Field(description="Visible text, placeholder, or aria-label")
    selector: str = Field(description="CSS selector to locate this element")
    xpath: str = Field(default="", description="XPath expression")
    attributes: dict[str, str] = Field(default_factory=dict, description="HTML attributes (id, class, role, aria-label, href, type, etc.)")
    event_listeners: list[str] = Field(default_factory=list, description="Detected event types (click, change, submit, etc.)")
    is_visible: bool = Field(description="Whether element is visible in the viewport")
    page_region: str = Field(description="Page region: header | hero | main | footer | sidebar | modal | nav")
    bounding_box: dict | None = Field(default=None, description="Bounding box {x, y, width, height}")


class DOMState(BaseModel):
    state_id: str = Field(description="Unique state ID e.g. state_001")
    url: str = Field(description="Page URL at this state")
    title: str = Field(description="Page title at this state")
    dom_hash: str = Field(description="Short hash of visible DOM content for deduplication")
    visible_element_count: int = Field(description="Count of interactive elements visible")
    description: str = Field(description="Human-readable description of this state")


class ActionLog(BaseModel):
    element_id: str = Field(description="elem_id of the element acted upon")
    action: str = Field(description="Action performed: click | type | select | submit")
    selector: str = Field(description="CSS selector used")
    url_before: str = Field(description="Page URL before the action")
    url_after: str = Field(description="Page URL after the action")
    state_before_id: str = Field(description="State ID before the action")
    state_after_id: str = Field(description="State ID after the action")
    mutations_count: int = Field(default=0, description="Number of DOM mutations triggered")
    new_elements_added: list[str] = Field(default_factory=list, description="Selectors of new elements that appeared")
    network_calls: list[str] = Field(default_factory=list, description="API/XHR URLs triggered by this action")
    timestamp: float = Field(default=0.0, description="Unix timestamp of the action")


class StateTransition(BaseModel):
    from_state_id: str = Field(description="State ID before the transition")
    to_state_id: str = Field(description="State ID after the transition")
    trigger_element_id: str = Field(description="elem_id of the element that triggered the transition")
    trigger_action: str = Field(description="Action that triggered the transition")


class DOMExtractionResult(BaseModel):
    url: str = Field(description="URL that was extracted")
    page_title: str = Field(description="Page title")
    dom_tree: dict = Field(default_factory=dict, description="Serialized DOM tree")
    interactive_elements: list[DOMElement] = Field(description="All interactive elements found")
    network_requests: list[dict] = Field(default_factory=list, description="XHR/fetch requests observed on load")
    event_listener_map: dict = Field(default_factory=dict, description="Map of element key → detected event types")


class ExplorationResult(BaseModel):
    url: str = Field(description="URL that was explored")
    action_logs: list[ActionLog] = Field(description="Log of every action performed")
    state_transitions: list[StateTransition] = Field(description="State transitions that produced DOM changes")
    states: list[DOMState] = Field(description="All unique page states encountered")
