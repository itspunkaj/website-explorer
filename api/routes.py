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
    save_kg_data,
    update_website_status,
)
from knowledge_graph.dom_extractor import extract_dom
from knowledge_graph.dom_explorer import explore_dom
from knowledge_graph.hybrid_agent import run_hybrid_agent
from knowledge_graph.neo4j_client import ingest_to_neo4j

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


async def _run_pipeline(website_id: int, url: str):
    try:
        # ── PHASE 1: DOM Extraction (Playwright) ──────────────────────────────
        # Comment out to disable Playwright DOM extraction.
        dom_result = await extract_dom(url)
        update_website_status(website_id, "running", title=dom_result.page_title)
        # ──────────────────────────────────────────────────────────────────────

        # ── PHASE 2: DOM Exploration (Playwright) ─────────────────────────────
        # Comment out to disable interactive element exploration.
        exploration_result = await explore_dom(url, dom_result)
        # ──────────────────────────────────────────────────────────────────────

        # ── PHASE 3: Agent Exploration — OLD LLM visual browser ───────────────
        # To re-enable: uncomment the block below AND add these imports at the top:
        #   from knowledge_graph.exploration_agent import run_exploration_agent
        #   from knowledge_graph.db import save_agent_exploration
        #
        # exploration = await run_exploration_agent(url)
        # update_website_status(website_id, "running", title=exploration.page_title)
        # save_agent_exploration(website_id, exploration)
        # ──────────────────────────────────────────────────────────────────────

        # ── PHASE 4: KG Creation — HYBRID (Playwright data + LLM) ────────────
        # Comment out to disable the hybrid KG agent.
        kg = await run_hybrid_agent(dom_result, exploration_result)
        # ──────────────────────────────────────────────────────────────────────

        # ── PHASE 5: KG Creation — OLD LLM visual browser ────────────────────
        # To re-enable: uncomment the line below AND add this import at the top:
        #   from knowledge_graph.agent import run_agent
        # Also comment out Phase 4 above so only one KG agent runs.
        #
        # kg = await run_agent(url)
        # ──────────────────────────────────────────────────────────────────────

        # ── PHASE 6: Neo4j ingestion ──────────────────────────────────────────
        # exploration_result adds State nodes + LEADS_TO / TRANSITIONS_TO edges.
        # When running old pipeline (phases 3+5), use: ingest_to_neo4j(kg)
        ingest_to_neo4j(kg, exploration=exploration_result)
        # ──────────────────────────────────────────────────────────────────────

        agent_flows = get_agent_exploration(website_id)["flows"]
        save_kg_data(website_id, kg, agent_flows)
        update_website_status(website_id, "done", title=dom_result.page_title)

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
