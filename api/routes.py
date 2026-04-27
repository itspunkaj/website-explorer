import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from knowledge_graph.db import (
    create_website,
    get_agent_exploration,
    get_all_websites,
    get_kg_data,
    get_website,
    save_agent_exploration,
    save_kg_data,
    save_state_graph,
    update_website_status,
)
from knowledge_graph.dom_extractor import extract_dom
from knowledge_graph.dom_explorer import explore_dom
from knowledge_graph.exploration_agent import run_exploration_agent
from knowledge_graph.hybrid_agent import run_hybrid_agent
from knowledge_graph.neo4j_client import ingest_to_neo4j

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


async def _run_pipeline(website_id: int, url: str):
    try:
        # ── PHASE 1: Agent Exploration (browser-use + GPT-4.1-mini) ──────────
        # The LLM visually browses the page and identifies user flows/interactions.
        # Comment out to skip agent exploration.
        exploration = await run_exploration_agent(url)
        update_website_status(website_id, "running", title=exploration.page_title)
        save_agent_exploration(website_id, exploration)
        # ──────────────────────────────────────────────────────────────────────

        # ── PHASE 2a: DOM Extraction (Playwright) ────────────────────────────
        # Deterministic structural snapshot — real elements, selectors, attributes.
        # Comment out to skip Playwright DOM extraction.
        dom_result = await extract_dom(url)
        # ──────────────────────────────────────────────────────────────────────

        # ── PHASE 2b: DOM Exploration (Playwright) ───────────────────────────
        # Click every interactive element, track mutations, state transitions.
        # Comment out to skip interactive exploration.
        exploration_result = await explore_dom(url, dom_result)
        # ──────────────────────────────────────────────────────────────────────

        # ── PHASE 2c: Hybrid KG Creation (Playwright data + GPT-4.1) ─────────
        # LLM receives structured DOM + action logs → outputs the knowledge graph.
        # Comment out to skip KG creation.
        kg = await run_hybrid_agent(dom_result, exploration_result)
        # ──────────────────────────────────────────────────────────────────────

        # ── PHASE 2d: Neo4j ingestion (new State-Action schema) ──────────────
        graph = ingest_to_neo4j(kg, exploration=exploration_result)
        save_state_graph(
            website_id,
            graph["page"],
            graph["state_nodes"],
            graph["elem_nodes"],
            graph["action_nodes"],
            exploration_result.state_transitions,
            graph["state_sig_map"],
            graph["action_map"],
        )
        # ──────────────────────────────────────────────────────────────────────

        # Compare KG flows against what the agent found → marks missed_by_agent
        agent_flows = get_agent_exploration(website_id)["flows"]
        save_kg_data(website_id, kg, agent_flows)

        update_website_status(website_id, "done", title=exploration.page_title)

    except Exception as exc:
        update_website_status(website_id, "error", error=str(exc))
        raise


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    websites = get_all_websites()
    return templates.TemplateResponse(request, "index.html", {"websites": websites})


@router.post("/explore")
async def explore(request: Request, background_tasks: BackgroundTasks, url: str = Form(...)):
    url = url.strip().rstrip("/")
    if not url.startswith("http"):
        url = "https://" + url
    website_id = create_website(url)
    update_website_status(website_id, "running")
    background_tasks.add_task(_run_pipeline, website_id, url)
    return RedirectResponse(url=f"/website/{website_id}", status_code=303)


@router.get("/website/{website_id}", response_class=HTMLResponse)
async def dashboard(request: Request, website_id: int):
    website = get_website(website_id)
    if not website:
        return HTMLResponse("Website not found", status_code=404)

    agent_data = get_agent_exploration(website_id) if website["status"] in ("done", "running") else {}
    kg_data = get_kg_data(website_id) if website["status"] == "done" else {}

    return templates.TemplateResponse(request, "dashboard.html", {
        "website": website,
        "agent_data": agent_data,
        "kg_data": kg_data,
    })


@router.get("/api/website/{website_id}/status")
async def website_status(website_id: int):
    website = get_website(website_id)
    if not website:
        return JSONResponse({"error": "not found"}, status_code=404)
    agent_data = get_agent_exploration(website_id)
    return JSONResponse({
        "status": website["status"],
        "title": website["title"],
        "error": website["error_message"],
        "agent_flow_count": len(agent_data.get("flows", [])),
    })


@router.get("/api/websites")
async def list_websites():
    return JSONResponse(get_all_websites())
