from browser_use import Agent, ChatAnthropic
from dotenv import load_dotenv
import asyncio

load_dotenv()

async def main():
    llm = ChatAnthropic(model='claude-sonnet-4-6', temperature=0.0)
    task = "Explore https://endee.io/ and identify as many user interactions/flows as possible on just the Homepage (do not go to any other routes. just homepage i.e. '/' route only) and create ende to end test screnarios for the all the interactions. Do not go ahead if there are external links to other websites. Store all the interactions in the root in an suitable format."
    agent = Agent(task=task, llm=llm)
    history = await agent.run()
    print(history.final_result())

if __name__ == "__main__":
    asyncio.run(main())
