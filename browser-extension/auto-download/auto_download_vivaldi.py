"""
Automated movie/show search & download via real Vivaldi browser + nodriver CDP.
Uses the actual Vivaldi browser (not a stealth fork) with the HLS capture extension.

Setup (one-time):
    1. Run: python auto_download_vivaldi.py --setup
       This launches Vivaldi with a dedicated profile so you can load the extension.
    2. In Vivaldi, go to vivaldi://extensions → Load unpacked → select hls-capture folder.
    3. Close Vivaldi.

Usage:
    python auto_download_vivaldi.py "The Thing" 1982
    python auto_download_vivaldi.py "E.T. the Extra-Terrestrial" 1982 --server 2
"""

import asyncio
import shutil
import subprocess
import sys
import time
import argparse
import requests
from pathlib import Path

import nodriver

VIVALDI_PATH = r"C:\Users\thunderhead\AppData\Local\Vivaldi\Application\vivaldi.exe"
PROFILE_PATH = r"C:\Temp_Media\_vivaldi_automation"
EXTENSION_PATH = r"C:\dev\thunderhead\browser-extension\hls-capture"
UBLOCK_PATH = r"C:\Users\thunderhead\AppData\Local\Vivaldi\User Data\Default\Extensions\cjpalhdlnbpafiamejdnhcphjbkeiagm\1.70.0_0"
SCREENSHOT_DIR = Path(__file__).parent / "screenshots"
HLS_SERVER = "http://localhost:9876"
CDP_PORT = 9222

SITES = {
    "brocoflix": "https://brocoflix.xyz",
    "1movies": "https://1moviesz.to",
}


def launch_vivaldi(cdp_port=CDP_PORT, profile_suffix=""):
    """Launch Vivaldi with remote debugging and the automation profile."""
    base_profile = Path(PROFILE_PATH)
    if profile_suffix:
        profile = Path(f"{PROFILE_PATH}{profile_suffix}")
    else:
        profile = base_profile

    extensions = f"{EXTENSION_PATH},{UBLOCK_PATH}"
    cmd = [
        VIVALDI_PATH,
        f"--user-data-dir={str(profile)}",
        f"--remote-debugging-port={cdp_port}",
        f"--load-extension={extensions}",
        f"--disable-extensions-except={extensions}",
        "--no-first-run",
        "--disable-default-apps",
        "--disable-component-update",
        "--disable-background-networking",
    ]
    print(f"[*] Launching Vivaldi (port {cdp_port})...")
    proc = subprocess.Popen(cmd)
    return proc


def prepare_profiles(count):
    """Clone the base automation profile for additional browser instances.
    Must be called BEFORE any Vivaldi instance is launched (avoids file locks)."""
    base = Path(PROFILE_PATH)
    for i in range(1, count):
        suffix = f"_{i+1}"
        dest = Path(f"{PROFILE_PATH}{suffix}")
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        print(f"[*] Cloning profile → {dest.name}")
        shutil.copytree(base, dest, ignore=shutil.ignore_patterns(
            "SingletonLock", "SingletonCookie", "SingletonSocket",
            "lockfile", "*.lock", "DevToolsActivePort",
        ))


def check_hls_server():
    try:
        r = requests.get(f"{HLS_SERVER}/status", timeout=3)
        data = r.json()
        print(f"[+] HLS server running (dry_run={data.get('dry_run', '?')})")
        return True
    except Exception:
        print("[-] HLS server not running — downloads won't work")
        print("    Start it: browser-extension\\hls-server\\start_server.bat")
        return False


async def screenshot(tab, name):
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOT_DIR / f"{name}.png"
    await tab.save_screenshot(str(path))
    print(f"    [screenshot] {path.name}")


import re


def _unwrap(val):
    """Unwrap CDP deep-serialized value into plain Python objects."""
    if not isinstance(val, dict) or "type" not in val:
        return val
    t = val["type"]
    v = val.get("value")
    if t in ("string", "number", "boolean"):
        return v
    if t in ("null", "undefined"):
        return None
    if t == "array":
        return [_unwrap(item) for item in (v or [])]
    if t == "object":
        return {k: _unwrap(inner) for k, inner in (v or [])}
    return v


async def js(tab, expression):
    """Evaluate JS and return plain Python values (not CDP deep-serialized objects)."""
    import nodriver.cdp.runtime as runtime

    remote_object, errors = await tab.send(
        nodriver.cdp.runtime.evaluate(
            expression=expression,
            user_gesture=True,
            await_promise=False,
            return_by_value=False,
            allow_unsafe_eval_blocked_by_csp=True,
            serialization_options=nodriver.cdp.runtime.SerializationOptions(
                serialization="deep", max_depth=10,
                additional_parameters={"maxNodeDepth": 10, "includeShadowTree": "all"},
            ),
        )
    )
    if errors:
        return None
    if remote_object and remote_object.deep_serialized_value:
        dsv = remote_object.deep_serialized_value
        return _unwrap({"type": dsv.type_, "value": dsv.value})
    if remote_object and remote_object.value is not None:
        return remote_object.value
    return None


async def dismiss_popups(tab):
    """Dismiss common popups, overlays, and cookie banners."""
    dismissed = await js(tab, """(() => {
        let count = 0;
        const closeSels = [
            '.close-btn', '.modal-close', '[aria-label="Close"]',
            'button.close', '.popup-close', '[data-dismiss]',
        ];

        for (const sel of closeSels) {
            try {
                for (const el of document.querySelectorAll(sel)) {
                    if (el.offsetParent !== null) {
                        el.click();
                        count++;
                    }
                }
            } catch(e) {}
        }

        for (const btn of document.querySelectorAll('button')) {
            const text = btn.innerText.trim().toLowerCase();
            if ((text === 'close' || text === 'x' || text === '×' || text === 'dismiss'
                 || text === 'no thanks' || text === 'got it')
                && btn.offsetParent !== null) {
                btn.click();
                count++;
            }
        }

        for (const el of document.querySelectorAll('.modal, .overlay, .popup, [class*="modal"], [class*="overlay"], [class*="popup"]')) {
            const closeBtn = el.querySelector('button, .close, [class*="close"], [aria-label="Close"]');
            if (closeBtn && closeBtn.offsetParent !== null) {
                closeBtn.click();
                count++;
            }
        }

        for (const el of document.querySelectorAll('[class*="modal"], [class*="overlay"], [class*="popup"]')) {
            const style = window.getComputedStyle(el);
            if (style.position === 'fixed' || style.position === 'absolute') {
                if (el.offsetHeight > 100 && el.offsetWidth > 100) {
                    el.remove();
                    count++;
                }
            }
        }

        return count;
    })()""")
    if dismissed:
        print(f"[+] Dismissed {dismissed} popup(s)/overlay(s)")
        await tab.sleep(1)


def build_search_queries(title, year):
    queries = []
    clean = re.sub(r"\s*\(\d{4}\)\s*", "", title).strip()
    words = clean.split()
    no_dots = clean.replace(".", "").strip()
    no_dots_words = no_dots.split()
    stop_words = {"the", "a", "an", "of", "and", "in", "on", "at", "to", "for", "is"}

    if len(words) > 1 and words[0].lower() not in stop_words:
        queries.append(words[0])
    if no_dots.lower() != clean.lower() and len(no_dots_words) > 1:
        queries.append(no_dots_words[0])
    if ":" in clean:
        queries.append(clean.split(":")[0].strip())
    if " - " in clean:
        queries.append(clean.split(" - ")[0].strip())
    if len(words) > 2:
        queries.append(" ".join(words[:2]))
    queries.append(clean)
    if no_dots.lower() != clean.lower():
        queries.append(no_dots)

    seen = set()
    result = []
    for q in queries:
        q = q.strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            result.append(q)
    return result


async def try_search(tab, title, year):
    """Search with progressively simpler queries."""
    queries = build_search_queries(title, year)
    print(f"[*] Search queries to try: {queries}")

    homepage_url = tab.url

    for i, query in enumerate(queries):
        print(f"\n[*] Attempt {i+1}/{len(queries)}: searching for {query!r}")

        if i > 0:
            await tab.get(homepage_url)
            await tab
            await tab.sleep(2)

        # Dismiss any popups first
        await dismiss_popups(tab)

        # Find search input
        search_input = None
        for sel in ["input[placeholder*='earch' i]", "input[type='search']",
                     "input[name='search']", "input[name='q']"]:
            try:
                search_input = await tab.select(sel, timeout=3)
                if search_input:
                    break
            except Exception:
                continue

        if not search_input:
            print("[-] Could not find search input")
            return False

        # Clear and type query
        await search_input.click()
        await tab.sleep(0.3)
        # Clear existing text
        await search_input.apply("(el) => { el.value = ''; el.dispatchEvent(new Event('input', {bubbles: true})); }")
        await tab.sleep(0.2)
        await search_input.send_keys(query)
        await tab.sleep(0.3)

        # Submit search via Enter key event on the input element
        await search_input.apply("""(el) => {
            el.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true}));
            el.dispatchEvent(new KeyboardEvent('keypress', {key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true}));
            el.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true}));
            // Also try form submit
            const form = el.closest('form');
            if (form) form.submit();
        }""")

        print(f"    Submitted: {query!r}")
        await tab.sleep(3)
        await tab
        await screenshot(tab, f"03-search-results-{i+1}")

        # Check for results — works across BrocoFlix and 1movies
        has_results = await js(tab, """(() => {
            const text = document.body.innerText;
            if (text.includes('No results found') || text.includes('No results.')) return false;
            // BrocoFlix: "Search Results" or "Results for"
            if (text.includes('Search Results') || text.includes('Results for')) {
                return document.querySelectorAll('img').length > 3;
            }
            // 1movies: "Browser" heading + result cards with images
            if (document.querySelector('.film_list-wrap, .flw-item, [class*="film"]')) {
                return true;
            }
            // Generic: URL has search/keyword param and page has multiple images
            if (location.search.includes('keyword') || location.search.includes('query')) {
                return document.querySelectorAll('img').length > 3;
            }
            return false;
        })()""")

        if has_results:
            print(f"[+] Got results for: {query!r}")
            return True
        else:
            print(f"    No results for: {query!r}")

    print("[-] All search queries exhausted")
    return False


async def find_and_click_result(tab, title, year):
    """Find the best matching result and click it."""
    year_str = str(year)
    results = await js(tab,
        """(() => {
            const year = """ + repr(year_str) + """;
            const items = [];
            const seen_href = new Set();

            // Helper: get text from a link or its parent card container
            function getCardText(a) {
                let text = a.innerText.trim();
                if (text) return text.substring(0, 80);
                // 1movies: title is in a sibling element, look at parent card
                const card = a.closest('[class*="item"], [class*="card"], [class*="film"]');
                if (card) {
                    text = card.innerText.trim();
                    if (text) return text.substring(0, 80);
                }
                // Try next sibling
                let sib = a.nextElementSibling;
                while (sib) {
                    text = sib.innerText.trim();
                    if (text) return text.substring(0, 80);
                    sib = sib.nextElementSibling;
                }
                return '';
            }

            // Prefer links to info/detail pages
            for (const a of document.querySelectorAll('a[href*="info"], a[href*="detail"], a[href*="movie"], a[href*="watch"]')) {
                const img = a.querySelector('img');
                if (!img) continue;
                const rect = a.getBoundingClientRect();
                if (rect.width < 30 || rect.height < 30) continue;
                if (seen_href.has(a.href)) continue;
                seen_href.add(a.href);
                const text = getCardText(a);
                items.push({
                    text: text,
                    href: a.href,
                    hasYear: text.includes(year) || a.href.includes(year),
                });
            }

            // Also check card-like containers
            for (const el of document.querySelectorAll('[class*="card"], [class*="poster"], [class*="item"], [class*="result"]')) {
                const rect = el.getBoundingClientRect();
                if (rect.width < 50 || rect.height < 50) continue;
                if (rect.width > 600) continue;
                const text = el.innerText.trim();
                if (!text || text.length < 2) continue;
                const link = el.querySelector('a[href]');
                const href = link ? link.href : '';
                if (href && seen_href.has(href)) continue;
                if (href) seen_href.add(href);
                items.push({
                    text: text.substring(0, 80),
                    href: href,
                    hasYear: text.includes(year) || href.includes(year),
                });
            }

            // Fallback: any link with an image
            for (const a of document.querySelectorAll('a[href]')) {
                if (seen_href.has(a.href)) continue;
                const img = a.querySelector('img');
                if (!img) continue;
                const rect = a.getBoundingClientRect();
                if (rect.width < 30 || rect.height < 30) continue;
                seen_href.add(a.href);
                const fbText = getCardText(a);
                items.push({
                    text: fbText,
                    href: a.href,
                    hasYear: a.innerText.includes(year) || a.href.includes(year),
                });
            }

            return items.slice(0, 40);
        })()""")

    if not results:
        print("[-] No result cards found")
        return False

    # Score results
    title_lower = title.lower().replace(".", "")
    title_words = {w for w in title_lower.split() if len(w) > 2}

    def score(r):
        text = r["text"].lower().replace(".", "")
        href = (r.get("href") or "").lower()
        s = 0
        if r["hasYear"]:
            s += 10
        s += sum(3 for w in title_words if w in text or w in href)
        if title_lower in text:
            s += 20
        if "pages/info" in href:
            s += 5
        text_len = len(r["text"])
        if text_len > 80:
            s -= (text_len - 80) // 10
        if any(nav in text for nav in ["home", "dmca", "discord", "movies", "tv shows", "welcome", "brocoflix"]):
            s -= 50
        if href and href.rstrip("/").endswith((".xyz", ".com", ".to", ".org")):
            s -= 50
        return s

    results.sort(key=score, reverse=True)
    results = [r for r in results if score(r) > 0]

    print(f"[+] Found {len(results)} results (sorted by relevance):")
    for i, r in enumerate(results[:5]):
        yr = " [year]" if r["hasYear"] else ""
        print(f"      [{i}] {r['text'][:70]}{yr}")

    if not results:
        return False

    target = results[0]
    print(f"\n[*] Clicking: {target['text'][:60]}")
    if target.get("href"):
        await tab.get(target["href"])
    else:
        snippet = target["text"][:40].replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$")
        await js(tab,
            """(() => {
                const snippet = `""" + snippet + """`;
                const sels = '[class*="card"], [class*="item"], [class*="poster"]';
                const els = [...document.querySelectorAll(sels)];
                const match = els.find(el => el.innerText.trim().startsWith(snippet));
                if (match) (match.querySelector('a') || match).click();
            })()""")

    await tab
    await tab.sleep(3)
    await screenshot(tab, "04-movie-page")
    print(f"    URL: {tab.url}")
    return True


async def click_watch_and_play(tab, server_num=2):
    """Click Watch Now, select server, and start playback."""
    # Click Watch Now
    try:
        watch_btn = await tab.find("Watch Now", timeout=5)
        if watch_btn:
            print(f"[+] Clicking Watch Now")
            await watch_btn.click()
            await tab.sleep(3)
    except Exception:
        print("[-] Could not find Watch Now button")
        return False

    # Select server
    try:
        server_btn = await tab.find(f"Server {server_num}", timeout=5)
        if server_btn:
            print(f"[+] Selecting Server {server_num}")
            await server_btn.click()
            await tab.sleep(4)
    except Exception:
        print(f"[-] Could not find Server {server_num}")

    await screenshot(tab, "06a-after-server-select")

    # Find the iframe and click its center to trigger play
    iframe_info = await js(tab, """(() => {
        const iframe = document.querySelector(
            'iframe[src*="embed"], iframe[src*="vidsrc"], ' +
            'iframe[src*="player"], iframe'
        );
        if (!iframe) return null;
        const rect = iframe.getBoundingClientRect();
        return {
            src: iframe.src,
            x: rect.x + rect.width / 2,
            y: rect.y + rect.height / 2,
            width: rect.width,
            height: rect.height,
        };
    })()""")

    if iframe_info:
        print(f"[+] Found iframe: {iframe_info['src'][:80]}")
        print(f"    Size: {iframe_info['width']:.0f}x{iframe_info['height']:.0f}")

        # Click center of iframe to trigger play
        x, y = iframe_info["x"], iframe_info["y"]
        print(f"    Clicking center ({x:.0f}, {y:.0f})...")

        await tab.send(nodriver.cdp.input_.dispatch_mouse_event(
            type_="mousePressed", x=x, y=y, button=nodriver.cdp.input_.MouseButton("left"),
            click_count=1,
        ))
        await tab.send(nodriver.cdp.input_.dispatch_mouse_event(
            type_="mousePressed", x=x, y=y, button=nodriver.cdp.input_.MouseButton("left"),
            click_count=1,
        ))
        await tab.send(nodriver.cdp.input_.dispatch_mouse_event(
            type_="mouseReleased", x=x, y=y, button=nodriver.cdp.input_.MouseButton("left"),
            click_count=1,
        ))
        await tab.sleep(3)

        # Sometimes need a second click (after ad/overlay)
        await screenshot(tab, "06b-after-first-click")
        await tab.send(nodriver.cdp.input_.dispatch_mouse_event(
            type_="mousePressed", x=x, y=y, button=nodriver.cdp.input_.MouseButton("left"),
            click_count=1,
        ))
        await tab.send(nodriver.cdp.input_.dispatch_mouse_event(
            type_="mouseReleased", x=x, y=y, button=nodriver.cdp.input_.MouseButton("left"),
            click_count=1,
        ))
        await tab.sleep(2)
    else:
        print("[-] No iframe found")
        return False

    await screenshot(tab, "06-player-loaded")
    return True


def find_extension_targets(cdp_port=CDP_PORT):
    """Find the HLS capture extension's targets via CDP."""
    try:
        resp = requests.get(f"http://localhost:{cdp_port}/json", timeout=3)
        targets = resp.json()
        ublock_id = "cjpalhdlnbpafiamejdnhcphjbkeiagm"
        ext_id = None
        sw_ws = None
        for t in targets:
            url = t.get("url", "")
            if "chrome-extension://" not in url:
                continue
            if ublock_id in url:
                continue
            tid = url.split("//")[1].split("/")[0]
            ext_id = tid
            if t.get("type") == "service_worker" or "background" in url.lower():
                sw_ws = t.get("webSocketDebuggerUrl")
        return ext_id, sw_ws
    except Exception as e:
        print(f"[-] Error querying CDP targets: {e}")
    return None, None


async def confirm_via_service_worker(sw_ws_url, quiet=False):
    """Connect to the service worker via CDP websocket and call confirmDownload(0).
    Returns True on confirm, False if no pending captures, None on error."""
    import websockets
    try:
        async with websockets.connect(sw_ws_url) as ws:
            import json as json_mod
            msg = json_mod.dumps({
                "id": 1,
                "method": "Runtime.evaluate",
                "params": {"expression": "pendingCaptures.length", "returnByValue": True}
            })
            await ws.send(msg)
            resp = json_mod.loads(await ws.recv())
            pending_count = resp.get("result", {}).get("result", {}).get("value", 0)
            if not quiet:
                print(f"    Service worker pendingCaptures: {pending_count}")

            if pending_count > 0:
                msg = json_mod.dumps({
                    "id": 2,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": "confirmDownload(0).then(() => 'confirmed')",
                        "returnByValue": True,
                        "awaitPromise": True,
                    }
                })
                await ws.send(msg)
                resp = json_mod.loads(await ws.recv())
                result = resp.get("result", {}).get("result", {}).get("value")
                print(f"    confirmDownload result: {result}")
                return result == "confirmed"
            return False
    except Exception as e:
        if not quiet:
            print(f"[-] Service worker error: {e}")
        return None


async def confirm_via_popup(browser, ext_id):
    """Fallback: open extension popup as tab and click Download."""
    popup_url = f"chrome-extension://{ext_id}/popup.html"
    try:
        popup_tab = await browser.get(popup_url, new_tab=True)
        await popup_tab.sleep(3)

        # Click the Download button
        try:
            dl_btn = await popup_tab.find("Download", timeout=5)
            if dl_btn:
                await dl_btn.click()
                print("[+] Clicked Download button in popup tab")
                await popup_tab.sleep(2)
                await popup_tab.close()
                return True
        except Exception:
            pass

        await popup_tab.close()
    except Exception as e:
        print(f"    Popup fallback error: {e}")
    return False


async def wait_for_capture_and_confirm(tab, browser, timeout=180, cdp_port=CDP_PORT):
    """Wait for extension to capture m3u8, then auto-confirm the download."""

    ext_id, sw_ws = find_extension_targets(cdp_port=cdp_port)
    if ext_id:
        print(f"[+] Found HLS extension: {ext_id}")
        if sw_ws:
            print(f"    Service worker WS: {sw_ws[:60]}...")
    else:
        print("[-] Could not find HLS capture extension")
        return False

    start_time = time.time()
    last_error = None
    while time.time() - start_time < timeout:
        if sw_ws:
            confirmed = await confirm_via_service_worker(sw_ws, quiet=(last_error is not None))
            if confirmed:
                print("[+] Download confirmed via service worker!")
                return True
            elif confirmed is None and last_error is None:
                last_error = True

        await tab.sleep(5)

    print(f"[-] No capture after {timeout}s — video may not have loaded")
    return False


def monitor_downloads(timeout=600, expected=1):
    """Monitor HLS server downloads until all complete."""
    print(f"[*] Monitoring {expected} download(s) (timeout {timeout}s)...")
    start_time = time.time()
    last_output = ""
    finished_names = set()
    while time.time() - start_time < timeout:
        try:
            r = requests.get(f"{HLS_SERVER}/downloads", timeout=3)
            data = r.json()
            downloads = data.get("downloads", data) if isinstance(data, dict) else data
            if not downloads:
                time.sleep(3)
                continue

            active = [d for d in downloads if d.get("status") in ("downloading", "queued", "uploading", "muxing", "moving")]
            done = [d for d in downloads if d.get("status") == "done"]
            failed = [d for d in downloads if d.get("status") == "error"]

            for d in done:
                name = d.get('filename', '?')
                if name not in finished_names:
                    print(f"\n    DONE: {name} — {d.get('size', '?')}")
                    finished_names.add(name)

            if not active and len(done) + len(failed) >= expected:
                print(f"\n[+] All downloads finished: {len(done)} done, {len(failed)} failed")
                return True

            lines = []
            for d in active:
                pct = int(d.get('percent', 0))
                frag = d.get('frag', '?')
                total = d.get('total_frags', '?')
                size = d.get('size', '')
                status = d.get('status', '')
                name = d.get('filename', '?')
                elapsed = int(time.time() - start_time)
                bar_len = 20
                filled = int(bar_len * pct / 100)
                bar = '█' * filled + '░' * (bar_len - filled)
                if status in ("muxing", "moving"):
                    lines.append(f"    {name}: {status}...")
                else:
                    lines.append(f"    {bar} {pct:3d}% {frag}/{total} {size} — {name}")
            output = " | ".join(lines) + f" [{int(time.time() - start_time)}s]"
            if output != last_output:
                print(f"\r{output}{' ' * 10}", end="")
                last_output = output
        except Exception:
            pass
        time.sleep(3)

    print(f"\n[-] Timed out after {timeout}s")
    return False


async def download_movie(title, year, site_url, server_num, cdp_port, vivaldi_proc, label=""):
    """Drive a single Vivaldi instance to search, play, and confirm a movie download."""
    tag = f"[{label}] " if label else ""

    try:
        print(f"{tag}Connecting to Vivaldi on port {cdp_port}...")
        browser = await nodriver.start(host="localhost", port=cdp_port)
        print(f"{tag}Connected!")

        tab = browser.main_tab

        print(f"{tag}Navigating to {site_url}...")
        await tab.get(site_url)
        await tab
        await tab.sleep(3)
        await dismiss_popups(tab)

        page_title = await js(tab, "document.title")
        print(f"{tag}Page: {page_title}")

        print(f"{tag}Searching for: {title} ({year})")
        found = await try_search(tab, title, year)
        if not found:
            print(f"{tag}Search failed")
            return False

        print(f"{tag}Finding best result...")
        if not await find_and_click_result(tab, title, year):
            print(f"{tag}No matching result found")
            return False

        print(f"{tag}Starting playback (Server {server_num})...")
        if not await click_watch_and_play(tab, server_num=server_num):
            print(f"{tag}Could not start playback")
            return False

        print(f"{tag}Waiting for m3u8 capture + auto-confirm...")
        captured = await wait_for_capture_and_confirm(tab, browser, timeout=180,
                                                       cdp_port=cdp_port)

        if captured:
            print(f"{tag}Download started successfully!")
            return True
        else:
            print(f"{tag}Could not capture m3u8")
            return False

    except Exception as e:
        print(f"{tag}Error: {e}")
        return False


async def start_auto_capture_via_sw(sw_ws_url, params_js):
    """Send startAutoCapture to the service worker and activate it on the correct tab."""
    import websockets
    import json as json_mod
    try:
        async with websockets.connect(sw_ws_url) as ws:
            msg = json_mod.dumps({
                "id": 1,
                "method": "Runtime.evaluate",
                "params": {
                    "expression": f"""
                        new Promise((resolve) => {{
                            chrome.tabs.query({{active: true, currentWindow: true}}, (tabs) => {{
                                const tab = tabs[0];
                                if (!tab) {{ resolve('no_tab'); return; }}
                                const params = {params_js};
                                startAutoCapture(params, tab.id, tab.url);
                                resolve('started');
                            }});
                        }})
                    """,
                    "returnByValue": True,
                    "awaitPromise": True,
                }
            })
            await ws.send(msg)
            resp = json_mod.loads(await ws.recv())
            result = resp.get("result", {}).get("result", {}).get("value")
            return result == "started"
    except Exception as e:
        print(f"[-] Auto-capture start error: {e}")
        return False


async def get_auto_capture_state(sw_ws_url):
    """Query auto-capture progress from the service worker."""
    import websockets
    import json as json_mod
    try:
        async with websockets.connect(sw_ws_url) as ws:
            msg = json_mod.dumps({
                "id": 1,
                "method": "Runtime.evaluate",
                "params": {
                    "expression": "JSON.stringify({active: autoCapture.active, finished: autoCapture.finished, season: autoCapture.multiSeason ? autoCapture.currentSeason : autoCapture.season, currentEp: autoCapture.currentEp, endEp: autoCapture.endEp, doneCount: autoCapture.doneCount, totalCount: autoCapture.totalCount, multiSeason: autoCapture.multiSeason, endSeason: autoCapture.endSeason})",
                    "returnByValue": True,
                }
            })
            await ws.send(msg)
            resp = json_mod.loads(await ws.recv())
            val = resp.get("result", {}).get("result", {}).get("value")
            if val:
                return json_mod.loads(val)
    except Exception:
        pass
    return None


async def force_auto_capture_range(sw_ws_url, start_ep, end_ep):
    """Override auto-capture endEp after episode discovery resets it."""
    import websockets
    import json as json_mod
    try:
        async with websockets.connect(sw_ws_url) as ws:
            expr = f"autoCapture.endEp = {end_ep}; autoCapture.startEp = {start_ep}; autoCapture.totalCount = {end_ep - start_ep + 1}; 'forced'"
            msg = json_mod.dumps({
                "id": 1,
                "method": "Runtime.evaluate",
                "params": {"expression": expr, "returnByValue": True}
            })
            await ws.send(msg)
            resp = json_mod.loads(await ws.recv())
            result = resp.get("result", {}).get("result", {}).get("value")
            if result == "forced":
                print(f"    Forced episode range: EP{start_ep}-{end_ep}")
            return result == "forced"
    except Exception as e:
        print(f"    Warning: could not force range: {e}")
        return False


async def download_show(args):
    """Download TV show episodes via 1movies + extension auto-capture."""
    vivaldi_proc = launch_vivaldi(cdp_port=CDP_PORT)
    print(f"[*] Waiting for browser to start...")
    await asyncio.sleep(6)

    try:
        print(f"[*] Connecting to Vivaldi on port {CDP_PORT}...")
        browser = await nodriver.start(host="localhost", port=CDP_PORT)
        print(f"[+] Connected!")

        tab = browser.main_tab
        site_url = SITES[args.site]

        print(f"\n[*] Navigating to {site_url}...")
        await tab.get(site_url)
        await tab
        await tab.sleep(3)
        await dismiss_popups(tab)

        page_title = await js(tab, "document.title")
        print(f"    Title: {page_title}")

        # Search
        print(f"\n[*] Searching for: {args.show}")
        found = await try_search(tab, args.show, "0")
        if not found:
            print("[-] Search failed")
            return

        # Find and click the TV show result
        print(f"\n[*] Finding best result...")
        if not await find_and_click_result(tab, args.show, "0"):
            print("[-] No matching result found")
            return

        # Navigate to the correct episode (show pages default to latest episode)
        if not args.multi_season:
            target_ep = args.start_ep
            target_hash = f"#ep={args.season},{target_ep}"
        else:
            target_hash = f"#ep={args.start_season},1"

        print(f"[*] Navigating to {target_hash}...")
        await js(tab, f"(() => {{ location.hash = '{target_hash}'; location.reload(); }})()")
        await tab.sleep(5)
        print(f"    URL: {tab.url}")

        # Click the play button to start video
        print(f"[*] Clicking play button...")
        play_clicked = await js(tab, """(() => {
            // Try the play button selector from site config
            const playBtn = document.querySelector('#player button.player-btn, .jw-icon-playback, [aria-label="Play"], .play-btn');
            if (playBtn) { playBtn.click(); return 'button'; }
            // Fallback: click center of the player/iframe area
            const player = document.querySelector('#player, .player, iframe');
            if (player) {
                const rect = player.getBoundingClientRect();
                const evt = new MouseEvent('click', {
                    clientX: rect.x + rect.width / 2,
                    clientY: rect.y + rect.height / 2,
                    bubbles: true
                });
                player.dispatchEvent(evt);
                return 'center';
            }
            return null;
        })()""")
        if play_clicked:
            print(f"    Clicked: {play_clicked}")
        else:
            # Last resort: CDP mouse click on center of viewport
            await tab.send(nodriver.cdp.input_.dispatch_mouse_event(
                type_="mousePressed", x=490, y=335,
                button=nodriver.cdp.input_.MouseButton("left"), click_count=1,
            ))
            await tab.send(nodriver.cdp.input_.dispatch_mouse_event(
                type_="mouseReleased", x=490, y=335,
                button=nodriver.cdp.input_.MouseButton("left"), click_count=1,
            ))
            print(f"    Clicked center of player via CDP")

        await tab.sleep(5)
        await screenshot(tab, "05-show-page")

        # Find the extension's service worker
        ext_id, sw_ws = find_extension_targets(cdp_port=CDP_PORT)
        if not sw_ws:
            print("[-] Could not find HLS extension service worker")
            return

        print(f"[+] Found HLS extension: {ext_id}")

        if not args.multi_season and args.start_ep == args.end_ep:
            # Single episode — just wait for the m3u8 on the already-loaded page
            desc = f"S{args.season}E{args.start_ep}"
            print(f"\n[*] Waiting for {desc} m3u8 capture...")
            confirmed = await wait_for_capture_and_confirm(tab, browser, timeout=180,
                                                            cdp_port=CDP_PORT)
            if confirmed:
                print(f"\n[*] Waiting for download to complete...")
                monitor_downloads(timeout=600, expected=1)
            else:
                print(f"[-] Could not capture {desc}")
        else:
            # Multi-episode — use auto-capture
            if args.multi_season:
                params_js = f'{{multiSeason: true, startSeason: {args.start_season}, endSeason: {args.end_season}, serverNum: {args.server}}}'
                desc = f"Seasons {args.start_season}-{args.end_season}"
                expected_count = None
            else:
                params_js = f'{{season: {args.season}, startEp: {args.start_ep}, endEp: {args.end_ep}, serverNum: {args.server}}}'
                desc = f"S{args.season} EP{args.start_ep}-{args.end_ep}"
                expected_count = args.end_ep - args.start_ep + 1

            print(f"\n[*] Starting auto-capture: {desc}...")
            started = await start_auto_capture_via_sw(sw_ws, params_js)
            if not started:
                print("[-] Failed to start auto-capture")
                return
            print(f"[+] Auto-capture started!")

            # Fix: episode discovery overrides endEp — force it back after a delay
            if not args.multi_season:
                await tab.sleep(8)
                await force_auto_capture_range(sw_ws, args.start_ep, args.end_ep)

            # Monitor auto-capture progress
            print(f"\n[*] Monitoring auto-capture progress...")
            last_info = ""
            done_count = 0
            while True:
                state = await get_auto_capture_state(sw_ws)
                if state:
                    done_count = state.get("doneCount", 0)

                    if expected_count and done_count >= expected_count:
                        print(f"\n[+] Auto-capture finished! {done_count} episodes downloaded")
                        break

                    if state.get("finished") or (not state.get("active") and done_count > 0):
                        print(f"\n[+] Auto-capture finished! {done_count} episodes downloaded")
                        break

                    info = f"S{state.get('season', '?')}E{state.get('currentEp', '?')} — {done_count} done"
                    if expected_count:
                        info += f"/{expected_count}"
                    elif state.get('totalCount'):
                        info += f"/{state['totalCount']}"
                    if info != last_info:
                        print(f"\r    Auto-capture: {info}{' ' * 20}", end="")
                        last_info = info

                    if not state.get("active") and done_count == 0:
                        await tab.sleep(10)
                        state2 = await get_auto_capture_state(sw_ws)
                        if state2 and not state2.get("active") and state2.get("doneCount", 0) == 0:
                            print(f"\n[-] Auto-capture stopped without downloading anything")
                            break

                await tab.sleep(5)

            # Wait for all yt-dlp downloads to finish
            print(f"\n[*] Waiting for downloads to complete...")
            monitor_downloads(timeout=1800, expected=done_count or 1)

    except KeyboardInterrupt:
        print("\n[*] Interrupted")
    except Exception as e:
        print(f"[-] Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("[*] Closing Vivaldi...")
        try:
            vivaldi_proc.terminate()
        except Exception:
            pass


async def main_async(args):
    site_url = SITES[args.site]

    # Clear HLS server state from previous runs
    try:
        requests.get(f"{HLS_SERVER}/clear", timeout=3)
        print("[+] Cleared HLS server download history")
    except Exception:
        pass

    if args.mode == "show":
        await download_show(args)
        print("\n[*] Done!")
        return

    movies = args.movies

    # Prepare cloned profiles before launching any browser (avoids file lock issues)
    if len(movies) > 1:
        prepare_profiles(len(movies))

    # Launch all Vivaldi instances
    vivaldi_procs = []
    for i in range(len(movies)):
        port = CDP_PORT + i
        suffix = f"_{i+1}" if i > 0 else ""
        proc = launch_vivaldi(cdp_port=port, profile_suffix=suffix)
        vivaldi_procs.append(proc)

    # Wait for all browsers to initialize
    print(f"[*] Waiting for {len(movies)} browser(s) to start...")
    await asyncio.sleep(6)

    try:
        if len(movies) == 1:
            title, year = movies[0]
            success = await download_movie(
                title, year, site_url, args.server,
                cdp_port=CDP_PORT, vivaldi_proc=vivaldi_procs[0], label=title,
            )
            if success:
                print(f"\n[*] Monitoring download...")
                monitor_downloads(timeout=600, expected=1)
        else:
            print(f"\n[*] Starting {len(movies)} downloads (staggered)...\n")
            tasks = []
            for i, (title, year) in enumerate(movies):
                port = CDP_PORT + i
                task = asyncio.create_task(
                    download_movie(title, year, site_url, args.server,
                                   cdp_port=port, vivaldi_proc=vivaldi_procs[i], label=title)
                )
                tasks.append(task)
                if i < len(movies) - 1:
                    await asyncio.sleep(15)

            results = await asyncio.gather(*tasks, return_exceptions=True)
            started = sum(1 for r in results if r is True)
            failed_movies = [movies[i][0] for i, r in enumerate(results) if r is not True]
            print(f"\n[*] {started}/{len(movies)} downloads started")
            if failed_movies:
                print(f"    Failed: {', '.join(failed_movies)}")

            if started > 0:
                print(f"\n[*] Monitoring all downloads...")
                monitor_downloads(timeout=900, expected=started)
    except KeyboardInterrupt:
        print("\n[*] Interrupted")
    finally:
        for proc in vivaldi_procs:
            try:
                proc.terminate()
            except Exception:
                pass

        # Clean up cloned profiles
        for i in range(1, len(movies)):
            cloned = Path(f"{PROFILE_PATH}_{i+1}")
            if cloned.exists():
                shutil.rmtree(cloned, ignore_errors=True)

    print("\n[*] Done!")


def parse_range(s):
    """Parse '3' into (3,3) or '1-8' into (1,8)."""
    if "-" in s:
        a, b = s.split("-", 1)
        return int(a), int(b)
    n = int(s)
    return n, n


def main():
    parser = argparse.ArgumentParser(
        description="Auto-download movies/shows via Vivaldi + nodriver",
        usage="""%(prog)s "Movie" YEAR ["Movie2" YEAR2 ...]
       %(prog)s --show "Show Name" --season 2 --episode 1
       %(prog)s --show "Show Name" --season 2 --episodes 1-8
       %(prog)s --show "Show Name" --seasons 1-3""",
    )
    parser.add_argument("args", nargs="*", help="Movie title and year pairs")
    parser.add_argument("--show", type=str, help="TV show name (uses 1movies)")
    parser.add_argument("--season", type=int, help="Season number")
    parser.add_argument("--episode", type=int, help="Single episode number")
    parser.add_argument("--episodes", type=str, help="Episode range: 2-8")
    parser.add_argument("--seasons", type=str, help="Season range: 1-3")
    parser.add_argument("--site", choices=list(SITES.keys()), default=None)
    parser.add_argument("--server", type=int, default=None, help="Server number")
    parser.add_argument("--setup", action="store_true",
                        help="Launch Vivaldi for one-time extension setup")
    parsed = parser.parse_args()

    if parsed.setup:
        print("=== One-time Setup ===")
        print("Launching Vivaldi with automation profile...")
        print(f"  1. Go to vivaldi://extensions")
        print(f"  2. Enable Developer Mode")
        print(f"  3. Click 'Load unpacked' → select: {EXTENSION_PATH}")
        print(f"  4. Close Vivaldi when done")
        launch_vivaldi()
        return

    if parsed.show:
        # TV show mode (1movies)
        parsed.mode = "show"
        parsed.site = parsed.site or "1movies"
        parsed.server = parsed.server or 1
        if parsed.seasons:
            parsed.multi_season = True
            parsed.start_season, parsed.end_season = parse_range(parsed.seasons)
        elif parsed.season and parsed.episode:
            parsed.multi_season = False
            parsed.start_ep = parsed.episode
            parsed.end_ep = parsed.episode
        elif parsed.season and parsed.episodes:
            parsed.multi_season = False
            parsed.start_ep, parsed.end_ep = parse_range(parsed.episodes)
        else:
            print("[-] --show requires one of:")
            print("      --season N --episode E")
            print("      --season N --episodes X-Y")
            print("      --seasons X-Y")
            return

        print(f"=== Auto-Download TV Show (Vivaldi + nodriver) ===")
        print(f"  Show:   {parsed.show}")
        if parsed.multi_season:
            print(f"  Mode:   Seasons {parsed.start_season}-{parsed.end_season}")
        elif parsed.start_ep == parsed.end_ep:
            print(f"  Mode:   Season {parsed.season} Episode {parsed.start_ep}")
        else:
            print(f"  Mode:   Season {parsed.season} Episodes {parsed.start_ep}-{parsed.end_ep}")
        print(f"  Site:   {parsed.site} ({SITES[parsed.site]})")
        print(f"  Server: {parsed.server}")
    else:
        # Movie mode (BrocoFlix)
        parsed.mode = "movie"
        parsed.site = parsed.site or "brocoflix"
        parsed.server = parsed.server or 2
        positional = parsed.args or ["The Thing", "1982"]
        movies = []
        i = 0
        while i < len(positional):
            title = positional[i]
            year = positional[i + 1] if i + 1 < len(positional) else "0"
            if not year.isdigit():
                print(f"[-] Expected a year after \"{title}\", got \"{year}\"")
                print(f"    Usage: python {sys.argv[0]} \"Movie Title\" 1984 [\"Movie2\" 1990 ...]")
                return
            movies.append((title, year))
            i += 2
        parsed.movies = movies

        print(f"=== Auto-Download (Vivaldi + nodriver) ===")
        for title, year in movies:
            print(f"  • {title} ({year})")
        print(f"  Site:   {parsed.site} ({SITES[parsed.site]})")
        print(f"  Server: {parsed.server}")

    print()
    check_hls_server()
    print()

    asyncio.run(main_async(parsed))


if __name__ == "__main__":
    import functools
    print = functools.partial(print, flush=True)
    main()
