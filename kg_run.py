import asyncio
import json
import re
from datetime import datetime
from dotenv import load_dotenv

from knowledge_graph.agent import run_agent
from knowledge_graph.neo4j_client import ingest_to_neo4j

load_dotenv()

TARGET_URL = "https://endee.io/"


def save_json(kg_data, url: str):
    domain = re.sub(r"[^\w]", "_", url.rstrip("/").split("//")[-1])
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{domain}_{timestamp}.json"
    with open(filename, "w") as f:
        json.dump(kg_data.model_dump(), f, indent=2)
    print(f"JSON saved: {filename}")
    return filename


async def main():
    print(f"Starting knowledge graph extraction for: {TARGET_URL}")

    kg_data = await run_agent(TARGET_URL)

    json_file = save_json(kg_data, TARGET_URL)

    ingest_to_neo4j(kg_data)

    print(
        f"\nKnowledge graph built successfully:"
        f"\n  Features:   {len(kg_data.features)}"
        f"\n  Flows:      {len(kg_data.flows)}"
        f"\n  Components: {len(kg_data.components)}"
        f"\n  Elements:   {len(kg_data.elements)}"
        f"\n  JSON:       {json_file}"
    )
    print("\nOpen Neo4j Browser at http://localhost:7474 and run:")
    print("  MATCH (n) RETURN n LIMIT 100")


if __name__ == "__main__":
    asyncio.run(main())
