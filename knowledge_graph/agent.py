from browser_use import Agent, ChatOpenAI
from .models import WebsiteKnowledgeGraph

TASK_TEMPLATE = """
You are a UI analyst. Your job is to explore the homepage of {url} and extract its complete structure into a knowledge graph.

STRICT RULES:
- Stay on the homepage only (the '/' route). Do NOT navigate to any other pages or external links.
- Do NOT click any links that go to other routes or external sites.
- Scroll through the entire page to see all sections before extracting.

WHAT TO EXTRACT:

1. ELEMENTS — Every interactive or meaningful UI element:
   - Buttons (CTA, submit, nav toggles)
   - Links (internal anchors, nav links — note their href)
   - Inputs (text fields, email, search boxes)
   - Images with meaning (hero image, logos, icons)
   - Headings and key text blocks
   - Form elements
   Assign each a unique ID like elem_001, elem_002, ...

2. COMPONENTS — Logical groups of related elements:
   - Navigation bar, hero section, feature cards, testimonials, footer, forms, etc.
   - Each component groups the element IDs that belong to it.
   Assign each a unique ID like comp_001, comp_002, ...

3. FLOWS — User interaction sequences (from trigger to completion):
   - Examples: "Submit contact form", "Open mobile nav menu", "Click CTA button", "Scroll to section via anchor link"
   - Each flow has ordered steps referencing element IDs and the action taken.
   - A flow must have at least 1 step.
   Assign each a unique ID like flow_001, flow_002, ...

4. FEATURES — Broad capabilities grouping one or more flows:
   - Examples: "Contact", "Navigation", "Social Proof", "Call to Action"
   - Each feature groups the flow IDs that implement it.
   Assign each a unique ID like feat_001, feat_002, ...

Be thorough. Capture every interactive element, every section, every possible user action visible on the homepage.

Output your findings as structured JSON matching the required schema exactly.
""".strip()


async def run_agent(url: str) -> WebsiteKnowledgeGraph:
    llm = ChatOpenAI(model="gpt-4.1")
    task = TASK_TEMPLATE.format(url=url)

    agent = Agent(
        task=task,
        llm=llm,
        output_model_schema=WebsiteKnowledgeGraph,
    )

    history = await agent.run(max_steps=20)
    result = history.structured_output

    if result is None:
        raise RuntimeError("Agent did not return structured output. Check agent logs.")

    return result
