import hashlib
from playwright.async_api import async_playwright
from .models import DOMElement, DOMExtractionResult

# Injected before page load to proxy addEventListener calls and record which
# event types each element registers. Keyed by a best-effort element identifier.
_EVENT_LISTENER_PROXY = """
window.__listenerMap = {};
(function() {
    var orig = EventTarget.prototype.addEventListener;
    EventTarget.prototype.addEventListener = function(type, fn, opts) {
        try {
            var el = this;
            var key = el.id ? ('#' + el.id)
                : (el.getAttribute && el.getAttribute('data-testid') ? '[data-testid="' + el.getAttribute('data-testid') + '"]'
                : (el.className ? el.tagName.toLowerCase() + '.' + el.className.toString().trim().split(/\s+/)[0]
                : el.tagName ? el.tagName.toLowerCase()
                : '__doc__'));
            if (!window.__listenerMap[key]) window.__listenerMap[key] = [];
            if (window.__listenerMap[key].indexOf(type) === -1) window.__listenerMap[key].push(type);
        } catch(e) {}
        return orig.call(this, type, fn, opts);
    };
})();
"""

# Serializes the DOM tree to a plain JSON object (no live references).
_SERIALIZE_DOM = """
() => {
    var SKIP = {script:1, style:1, noscript:1, svg:1, path:1, meta:1, link:1, head:1};
    var KEEP_ATTRS = ['id','class','href','src','type','role','aria-label',
                      'aria-describedby','placeholder','name','value',
                      'data-action','data-testid','tabindex'];
    function sel(el) {
        if (el.id) return '#' + el.id;
        if (el.getAttribute('data-testid')) return '[data-testid="' + el.getAttribute('data-testid') + '"]';
        var parts = [], cur = el;
        while (cur && cur.tagName && cur.tagName !== 'BODY') {
            var s = cur.tagName.toLowerCase();
            if (cur.id) { s += '#' + cur.id; parts.unshift(s); break; }
            var cls = Array.from(cur.classList)
                .filter(function(c){ return !/^[a-z0-9]{8,}$/.test(c); })
                .slice(0,2).join('.');
            if (cls) s += '.' + cls;
            parts.unshift(s);
            cur = cur.parentElement;
        }
        return parts.slice(-3).join(' > ');
    }
    function node(el, depth) {
        if (!el || el.nodeType !== 1 || depth > 7) return null;
        if (SKIP[el.tagName.toLowerCase()]) return null;
        var attrs = {};
        for (var i = 0; i < el.attributes.length; i++) {
            var a = el.attributes[i];
            if (KEEP_ATTRS.indexOf(a.name) !== -1) attrs[a.name] = a.value;
        }
        var children = Array.from(el.children)
            .map(function(c){ return node(c, depth+1); })
            .filter(Boolean).slice(0, 15);
        var tn = el.childNodes[0];
        var text = (tn && tn.nodeType === 3) ? (tn.textContent || '').trim().slice(0,150) : '';
        return { tag: el.tagName.toLowerCase(), text: text, attrs: attrs,
                 selector: sel(el), children: children };
    }
    return node(document.body, 0);
}
"""

# Enumerates all interactive elements and returns them as plain objects.
_EXTRACT_INTERACTIVE = """
() => {
    function sel(el) {
        if (el.id) return '#' + el.id;
        if (el.getAttribute('data-testid')) return '[data-testid="' + el.getAttribute('data-testid') + '"]';
        var parts = [], cur = el;
        while (cur && cur.tagName && cur.tagName !== 'BODY') {
            var s = cur.tagName.toLowerCase();
            if (cur.id) { s += '#' + cur.id; parts.unshift(s); break; }
            var cls = Array.from(cur.classList)
                .filter(function(c){ return !/^[a-z0-9]{8,}$/.test(c); })
                .slice(0,2).join('.');
            if (cls) s += '.' + cls;
            parts.unshift(s);
            cur = cur.parentElement;
        }
        return parts.slice(-3).join(' > ');
    }
    function region(el) {
        var cur = el;
        while (cur) {
            var tag = (cur.tagName || '').toLowerCase();
            var role = cur.getAttribute ? cur.getAttribute('role') : '';
            var id = (cur.id || '').toLowerCase();
            var cls = (typeof cur.className === 'string' ? cur.className : '').toLowerCase();
            if (tag === 'header' || role === 'banner') return 'header';
            if (tag === 'nav' || role === 'navigation') return 'nav';
            if (tag === 'footer' || role === 'contentinfo') return 'footer';
            if (tag === 'main' || role === 'main') return 'main';
            if (tag === 'aside' || role === 'complementary') return 'sidebar';
            if (role === 'dialog' || role === 'alertdialog') return 'modal';
            if (id.indexOf('hero') !== -1 || cls.indexOf('hero') !== -1) return 'hero';
            if (id.indexOf('modal') !== -1 || cls.indexOf('modal') !== -1) return 'modal';
            cur = cur.parentElement;
        }
        return 'main';
    }
    var query = 'button, a[href], input:not([type="hidden"]), select, textarea, ' +
                '[onclick], [role="button"], [role="link"], [role="menuitem"], ' +
                '[role="tab"], [tabindex]:not([tabindex="-1"])';
    var seen = new Set();
    return Array.from(document.querySelectorAll(query))
        .filter(function(el) {
            // deduplicate by computed selector
            var s = sel(el); if (seen.has(s)) return false; seen.add(s); return true;
        })
        .map(function(el, i) {
            var rect = el.getBoundingClientRect();
            var cs = window.getComputedStyle(el);
            var visible = rect.width > 0 && rect.height > 0
                && cs.display !== 'none' && cs.visibility !== 'hidden'
                && cs.opacity !== '0';
            var text = (el.innerText || el.value || el.placeholder
                       || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim().slice(0,200);
            return {
                elem_id: 'elem_' + String(i+1).padStart(3,'0'),
                tag: el.tagName.toLowerCase(),
                text: text,
                selector: sel(el),
                xpath: '',
                attributes: {
                    id: el.id || '',
                    class: (typeof el.className === 'string' ? el.className : '') || '',
                    href: el.href || '',
                    type: el.type || '',
                    role: el.getAttribute('role') || '',
                    'aria-label': el.getAttribute('aria-label') || '',
                    placeholder: el.placeholder || '',
                    name: el.name || '',
                    'data-testid': el.getAttribute('data-testid') || '',
                },
                event_listeners: [],
                is_visible: visible,
                page_region: region(el),
                bounding_box: { x: Math.round(rect.x), y: Math.round(rect.y),
                                 width: Math.round(rect.width), height: Math.round(rect.height) },
            };
        });
}
"""


async def extract_dom(url: str) -> DOMExtractionResult:
    network_requests: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )

        # Inject event-listener proxy before any page script runs
        await context.add_init_script(_EVENT_LISTENER_PROXY)

        page = await context.new_page()

        # Capture XHR / fetch on initial load
        def _on_request(req):
            if req.resource_type in ("xhr", "fetch"):
                network_requests.append({
                    "url": req.url,
                    "method": req.method,
                    "resource_type": req.resource_type,
                })

        page.on("request", _on_request)

        await page.goto(url, wait_until="networkidle", timeout=30_000)

        # Scroll full page to trigger lazy-loaded elements, then return to top
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(800)
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(400)

        title = await page.title()
        dom_tree = await page.evaluate(_SERIALIZE_DOM) or {}
        raw_elements: list[dict] = await page.evaluate(_EXTRACT_INTERACTIVE)

        # Pull event-listener map captured by the proxy
        listener_map: dict = await page.evaluate("() => window.__listenerMap || {}")

        await browser.close()

    # Enrich elements with listener data from the proxy map
    for el in raw_elements:
        attrs = el.get("attributes", {})
        candidates = []
        if attrs.get("id"):
            candidates.append("#" + attrs["id"])
        if attrs.get("data-testid"):
            candidates.append(f'[data-testid="{attrs["data-testid"]}"]')
        cls_parts = (attrs.get("class") or "").strip().split()
        if cls_parts:
            candidates.append(el["tag"] + "." + cls_parts[0])
        listeners: list[str] = []
        for key in candidates:
            for lk, lv in listener_map.items():
                if lk == key or lk.endswith(key):
                    listeners.extend(lv)
        el["event_listeners"] = list(set(listeners))

    interactive_elements = [DOMElement(**el) for el in raw_elements]

    return DOMExtractionResult(
        url=url,
        page_title=title,
        dom_tree=dom_tree,
        interactive_elements=interactive_elements,
        network_requests=network_requests,
        event_listener_map=listener_map,
    )
