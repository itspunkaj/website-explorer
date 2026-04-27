from browser_use import Agent, ChatOpenAI
from .models import AgentExploration

TASK_TEMPLATE = """
You are a QA analyst and UX researcher. Your job is to crawl the entire website starting at {url} and document every user interaction and flow you can find across ALL internal pages.

STRICT RULES:
- Start at {url} and follow internal links to explore every reachable page on the same domain (do not go to subdomains).
- Do NOT follow links that navigate to external domains (e.g. social media, third-party sites or even sub domains of the same website).
- Scroll through EACH page top to bottom before moving on.
- Track which page each interaction belongs to (record the URL).
- Visit as many unique internal pages as possible before finishing.

WHAT TO EXTRACT (per page visited):

1. PAGE SUMMARY — For each page: its URL and 1-2 sentences describing what it offers users.

2. INTERACTIONS — List every interactive element across all pages:
   - Navigation links (label them "Nav: <name>")
   - Buttons (label them "Button: <name>")
   - Input fields (label them "Input: <name/placeholder>")
   - Forms (label them "Form: <name>")
   - Dropdowns, toggles, accordions (label them by type + name)
   - Social media links, icons with actions
   Include the page URL where each element was found.

3. FLOWS — Group interactions into named user flows spanning one or more pages. Each flow is a sequence a user would actually perform:
   - Name: Short descriptive name (e.g. "Complete Checkout", "Submit Contact Form")
   - Description: What the user achieves by completing this flow
   - Steps: Ordered plain-language steps including page navigations (e.g. "1. Click 'Shop' nav link", "2. Select a product", "3. Click 'Add to Cart'")
   - Test cases: 2-3 test scenarios for QA

Be thorough. Every button, link, and input across ALL visited pages should appear in at least one flow.
""".strip()


async def run_exploration_agent(url: str) -> AgentExploration:
    llm = ChatOpenAI(model="gpt-4.1-mini")
    task = TASK_TEMPLATE.format(url=url)

    agent = Agent(
        task=task,
        llm=llm,
        output_model_schema=AgentExploration,
    )

    history = await agent.run(max_steps=50)
    result = history.structured_output

    if result is None:
        raise RuntimeError("Exploration agent did not return structured output.")

    return result
