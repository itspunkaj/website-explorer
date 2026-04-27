import hashlib
import time
from playwright.async_api import async_playwright
from .models import (
    ActionLog, DOMElement, DOMExtractionResult,
    DOMState, ExplorationResult, StateTransition,
)

# Injected before page load. Observes DOM mutations and proxies fetch/XHR.
_OBSERVER_SCRIPT = """
window.__mutations = [];
window.__networkCalls = [];

(function() {
    // MutationObserver — started once body exists
    var obs = new MutationObserver(function(mutations) {
        mutations.forEach(function(m) {
            var added = Array.from(m.addedNodes)
                .filter(function(n){ return n.nodeType === 1; })
                .map(function(n){
                    return n.tagName.toLowerCase()
                        + (n.id ? '#' + n.id : '')
                        + (n.className && typeof n.className === 'string'
                            ? '.' + n.className.trim().split(/\s+/)[0] : '');
                });
            window.__mutations.push({
                type: m.type,
                targetTag: (m.target.tagName || 'unknown').toLowerCase(),
                targetId: m.target.id || '',
                addedCount: m.addedNodes.length,
                removedCount: m.removedNodes.length,
                addedSelectors: added,
                timestamp: Date.now(),
            });
        });
    });
    function startObs() {
        if (document.body) {
            obs.observe(document.body, { childList: true, subtree: true, attributes: true });
        } else {
            setTimeout(startObs, 20);
        }
    }
    startObs();

    // Fetch proxy
    var origFetch = window.fetch;
    window.fetch = function() {
        var url = typeof arguments[0] === 'string' ? arguments[0] : (arguments[0] && arguments[0].url) || '';
        window.__networkCalls.push({ url: url, method: (arguments[1] && arguments[1].method) || 'GET', via: 'fetch' });
        return origFetch.apply(this, arguments);
    };

    // XHR proxy
    var origOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(method, url) {
        window.__networkCalls.push({ url: url, method: method, via: 'xhr' });
        return origOpen.apply(this, arguments);
    };
})();
"""

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Test values for form inputs
_FILL_VALUES = {
    "email": "test@example.com",
    "password": "TestPass123!",
    "search": "test query",
    "tel": "+1234567890",
    "url": "https://example.com",
    "number": "42",
    "text": "Test Input",
    "textarea": "Test content for exploration.",
}


async def _snapshot(page, counter: list[int]) -> DOMState:
    url = page.url
    title = await page.title()
    visible_text = await page.evaluate(
        "() => (document.body?.innerText || '').trim().slice(0, 3000)"
    )
    elem_count: int = await page.evaluate(
        "() => document.querySelectorAll('button,a,input,select,textarea').length"
    )
    dom_hash = hashlib.md5(visible_text.encode()).hexdigest()[:12]
    counter[0] += 1
    state_id = f"state_{str(counter[0]).zfill(3)}"
    return DOMState(
        state_id=state_id,
        url=url,
        title=title,
        dom_hash=dom_hash,
        visible_element_count=elem_count,
        description=f"{title} — {url}",
    )


async def _reset_logs(page) -> None:
    await page.evaluate("() => { window.__mutations = []; window.__networkCalls = []; }")


async def _collect_logs(page) -> tuple[list[dict], list[dict]]:
    mutations: list[dict] = await page.evaluate("() => window.__mutations.splice(0)")
    network: list[dict] = await page.evaluate("() => window.__networkCalls.splice(0)")
    return mutations, network


async def explore_dom(url: str, dom_result: DOMExtractionResult) -> ExplorationResult:
    action_logs: list[ActionLog] = []
    state_transitions: list[StateTransition] = []
    states: dict[str, DOMState] = {}  # keyed by dom_hash
    counter = [0]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=_UA,
            viewport={"width": 1280, "height": 900},
        )
        await context.add_init_script(_OBSERVER_SCRIPT)

        page = await context.new_page()
        await page.goto(url, wait_until="networkidle", timeout=30_000)

        initial = await _snapshot(page, counter)
        states[initial.dom_hash] = initial

        visible = [el for el in dom_result.interactive_elements if el.is_visible]

        for element in visible:
            # Re-navigate to base URL if we ended up somewhere else
            if page.url.rstrip("/") != url.rstrip("/"):
                try:
                    await page.goto(url, wait_until="networkidle", timeout=15_000)
                    await page.wait_for_timeout(300)
                except Exception:
                    continue

            await _reset_logs(page)
            before = await _snapshot(page, counter)
            if before.dom_hash not in states:
                states[before.dom_hash] = before

            action = "click"
            try:
                tag = element.tag
                input_type = element.attributes.get("type", "").lower()

                if tag == "select":
                    options: list[str] = await page.evaluate(
                        f"""() => {{
                            var el = document.querySelector({repr(element.selector)});
                            return el ? Array.from(el.options).map(o => o.value).filter(Boolean) : [];
                        }}"""
                    )
                    if options:
                        await page.select_option(element.selector, options[0], timeout=3_000)
                        action = "select"
                    else:
                        continue

                elif tag == "textarea":
                    await page.fill(element.selector, _FILL_VALUES["textarea"], timeout=3_000)
                    action = "type"

                elif tag == "input":
                    if input_type in ("submit", "button", "reset", "image"):
                        await page.click(element.selector, timeout=3_000)
                        action = "click"
                    elif input_type in ("checkbox", "radio"):
                        await page.click(element.selector, timeout=3_000)
                        action = "click"
                    else:
                        fill_key = next(
                            (k for k in _FILL_VALUES if k in (input_type or element.text.lower())),
                            "text",
                        )
                        await page.fill(element.selector, _FILL_VALUES[fill_key], timeout=3_000)
                        action = "type"

                else:
                    await page.click(element.selector, timeout=3_000)
                    action = "click"

                await page.wait_for_timeout(600)

            except Exception:
                # Element stale, not interactable, or timed out — skip
                continue

            # Wait for any navigation triggered by the action to settle before
            # evaluating JS.  A navigation destroys the old execution context, so
            # we must let the new one become ready first.
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=4_000)
            except Exception:
                pass

            mutations: list[dict] = []
            network: list[dict] = []
            try:
                mutations, network = await _collect_logs(page)
            except Exception:
                pass  # context destroyed mid-navigation — skip JS collection

            new_elems = [s for m in mutations for s in m.get("addedSelectors", [])]

            after_url = page.url
            after = await _snapshot(page, counter)
            if after.dom_hash not in states:
                states[after.dom_hash] = after

            action_logs.append(ActionLog(
                element_id=element.elem_id,
                action=action,
                selector=element.selector,
                url_before=before.url,
                url_after=after_url,
                state_before_id=before.state_id,
                state_after_id=after.state_id,
                mutations_count=len(mutations),
                new_elements_added=new_elems[:10],
                network_calls=[c.get("url", "") for c in network[:5]],
                timestamp=time.time(),
            ))

            if before.dom_hash != after.dom_hash:
                state_transitions.append(StateTransition(
                    from_state_id=before.state_id,
                    to_state_id=after.state_id,
                    trigger_element_id=element.elem_id,
                    trigger_action=action,
                ))

            # Navigate back if the action caused a page change
            if after_url.rstrip("/") != url.rstrip("/"):
                try:
                    await page.go_back(wait_until="networkidle", timeout=8_000)
                    await page.wait_for_timeout(300)
                except Exception:
                    try:
                        await page.goto(url, wait_until="networkidle", timeout=15_000)
                        await page.wait_for_timeout(300)
                    except Exception:
                        break

        await browser.close()

    return ExplorationResult(
        url=url,
        action_logs=action_logs,
        state_transitions=state_transitions,
        states=list(states.values()),
    )
