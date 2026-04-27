from browser_use import Agent, ChatOpenAI
from .models import AgentExploration

TASK_TEMPLATE = """
You are a QA analyst and UX researcher. Your job is to explore the homepage of {url} and document every user interaction and flow you can find.

STRICT RULES:
- Stay on the homepage only (the '/' route). Do NOT navigate to any other pages or external links.
- Scroll through the ENTIRE page top to bottom before extracting anything.
- Do NOT click links that navigate away from the homepage.

WHAT TO EXTRACT:

1. PAGE SUMMARY — Write 1-2 sentences describing what this page is and what it offers users.

2. INTERACTIONS — List every interactive element you can see on the page:
   - Navigation links (label them "Nav: <name>")
   - Buttons (label them "Button: <name>")
   - Input fields (label them "Input: <name/placeholder>")
   - Forms (label them "Form: <name>")
   - Dropdowns, toggles, accordions (label them by type + name)
   - Social media links, icons with actions

3. FLOWS — Group interactions into named user flows. Each flow is a sequence a user would actually perform:
   - Name: Short descriptive name (e.g. "Submit Contact Form")
   - Description: What the user achieves by completing this flow
   - Steps: Ordered plain-language steps (e.g. "1. Click 'Contact Us' button", "2. Fill in name field")
   - Test cases: 2-3 test scenarios for QA (e.g. "Submit with valid data", "Leave required field empty")

Be thorough. Every button, link, and input should appear in at least one flow.
""".strip()


async def run_exploration_agent(url: str) -> AgentExploration:
    llm = ChatOpenAI(model="gpt-4.1-mini")
    task = TASK_TEMPLATE.format(url=url)

    agent = Agent(
        task=task,
        llm=llm,
        output_model_schema=AgentExploration,
    )

    history = await agent.run(max_steps=20)
    result = history.structured_output

    if result is None:
        raise RuntimeError("Exploration agent did not return structured output.")

    return result
