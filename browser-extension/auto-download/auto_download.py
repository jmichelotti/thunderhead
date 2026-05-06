"""
Proof-of-concept: Automated movie/show search & download via stealth browser.
Uses CloakBrowser (stealth Chromium 145) with the HLS capture extension loaded.

Usage:
    python auto_download.py "E.T. the Extra-Terrestrial" 1982
    python auto_download.py "E.T. the Extra-Terrestrial" 1982 --site 1movies
"""

import sys
import re
import time
import argparse
import requests
from pathlib import Path

from cloakbrowser import launch_persistent_context

EXTENSION_PATH = r"C:\dev\thunderhead\browser-extension\hls-capture"
PROFILE_PATH = r"C:\Temp_Media\_browser_profile"
SCREENSHOT_DIR = Path(__file__).parent / "screenshots"
HLS_SERVER = "http://localhost:9876"

SITES = {
    "brocoflix": "https://brocoflix.xyz",
    "1movies": "https://1moviesz.to",
}


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


def screenshot(page, name):
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOT_DIR / f"{name}.png"
    page.screenshot(path=str(path), full_page=False)
    print(f"    [screenshot] {path.name}")
    return path


def dump_inputs(page):
    """List all input elements on page for debugging."""
    inputs = page.query_selector_all("input")
    print(f"    Found {len(inputs)} <input> elements:")
    for i, inp in enumerate(inputs):
        try:
            attrs = page.evaluate(
                """el => ({
                    type: el.type, name: el.name, id: el.id,
                    placeholder: el.placeholder,
                    className: el.className,
                    visible: el.offsetParent !== null
                })""",
                inp,
            )
            vis = "visible" if attrs.get("visible") else "hidden"
            print(
                f"      [{i}] type={attrs['type']!r} name={attrs['name']!r} "
                f"id={attrs['id']!r} placeholder={attrs['placeholder']!r} "
                f"class={attrs['className']!r} ({vis})"
            )
        except Exception:
            pass


def dump_links(page, keyword=""):
    """List links on page, optionally filtered by keyword."""
    links = page.evaluate(
        """(keyword) => {
            return [...document.querySelectorAll('a[href]')]
                .map(a => ({ text: a.innerText.trim().substring(0, 80), href: a.href }))
                .filter(a => !keyword || a.text.toLowerCase().includes(keyword.toLowerCase())
                          || a.href.toLowerCase().includes(keyword.toLowerCase()))
                .slice(0, 30);
        }""",
        keyword,
    )
    if links:
        print(f"    Links matching '{keyword}' ({len(links)}):")
        for link in links[:15]:
            print(f"      {link['text'][:60]:60s}  {link['href']}")
    return links


def build_search_queries(title, year):
    """Build a list of progressively simpler search queries from a title."""
    queries = []
    clean = re.sub(r"\s*\(\d{4}\)\s*", "", title).strip()

    words = clean.split()
    no_dots = clean.replace(".", "").strip()
    no_dots_words = no_dots.split()

    stop_words = {"the", "a", "an", "of", "and", "in", "on", "at", "to", "for", "is"}
    # Shortest distinctive form first (streaming sites prefer short queries)
    if len(words) > 1 and words[0].lower() not in stop_words:
        queries.append(words[0])
    if no_dots.lower() != clean.lower() and len(no_dots_words) > 1:
        queries.append(no_dots_words[0])

    # Then progressively longer
    if ":" in clean:
        queries.append(clean.split(":")[0].strip())
    if " - " in clean:
        queries.append(clean.split(" - ")[0].strip())
    if len(words) > 2:
        queries.append(" ".join(words[:2]))
    queries.append(clean)
    if no_dots.lower() != clean.lower():
        queries.append(no_dots)

    # Deduplicate while preserving order
    seen = set()
    result = []
    for q in queries:
        q = q.strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            result.append(q)
    return result


def find_search_input(page):
    """Find the search input element on the page."""
    search_selectors = [
        "input[type='search']",
        "input[name='search']",
        "input[name='s']",
        "input[name='q']",
        "input[placeholder*='earch' i]",
        "input.search-input",
        "#search-input",
        ".search-input input",
        ".search-bar input",
        "input.form-control[placeholder*='earch' i]",
        "header input[type='text']",
        "nav input[type='text']",
    ]
    for selector in search_selectors:
        try:
            el = page.query_selector(selector)
            if el and el.is_visible():
                return el, selector
        except Exception:
            continue

    # Try clicking search icon/button to reveal input
    icon_selectors = [
        "button[aria-label*='earch' i]",
        "a[aria-label*='earch' i]",
        ".search-toggle",
        ".search-btn",
        ".fa-search",
        ".icon-search",
        "svg.search",
        "[data-toggle='search']",
    ]
    for selector in icon_selectors:
        try:
            el = page.query_selector(selector)
            if el and el.is_visible():
                print(f"[+] Found search toggle: {selector}")
                el.click()
                time.sleep(1)
                return find_search_input(page)
        except Exception:
            continue

    return None, None


def submit_search(page, search_el, query):
    """Fill and submit a search query."""
    search_el.click()
    time.sleep(0.3)
    search_el.fill("")
    time.sleep(0.2)
    search_el.fill(query)
    time.sleep(0.3)
    search_el.press("Enter")
    print(f"    Submitted: {query!r}")
    page.wait_for_load_state("domcontentloaded", timeout=10000)
    time.sleep(2)


def has_results(page):
    """Check if the search results page has actual results."""
    debug = page.evaluate("""() => {
        const all_links = [...document.querySelectorAll('a[href]')];
        const with_img = all_links.filter(a => a.querySelector('img'));
        const with_bg = all_links.filter(a => {
            const style = window.getComputedStyle(a);
            return style.backgroundImage && style.backgroundImage !== 'none';
        });
        const visible_big = all_links.filter(a => {
            const rect = a.getBoundingClientRect();
            return rect.width > 50 && rect.height > 50;
        });
        // Check for card-like containers (div/article with images)
        const cards = document.querySelectorAll(
            '.card, .movie-card, .film-poster, .result-item, ' +
            '[class*="card"], [class*="poster"], [class*="item"], [class*="result"]'
        );
        const body_text = document.body.innerText.substring(0, 500);
        return {
            total_links: all_links.length,
            with_img: with_img.length,
            with_bg: with_bg.length,
            visible_big: visible_big.length,
            cards: cards.length,
            body_preview: body_text,
            sample_classes: visible_big.slice(0, 5).map(a => ({
                cls: a.className, href: a.href.substring(0, 80),
                children: a.innerHTML.substring(0, 200)
            }))
        };
    }""")
    print(f"    [debug] links={debug['total_links']} with_img={debug['with_img']} "
          f"with_bg={debug['with_bg']} visible_big={debug['visible_big']} cards={debug['cards']}")
    for s in debug.get("sample_classes", [])[:3]:
        print(f"    [debug] sample: class={s['cls']!r} href={s['href']}")
        print(f"    [debug]   html: {s['children'][:150]}")
    if "no results" in debug["body_preview"].lower():
        print(f"    [debug] 'no results' found in body text")

    # Detect results: visible links with images OR background images, or card elements
    result_count = max(debug["with_img"], debug["with_bg"], debug["cards"])
    nav_links = 5  # approximate nav/footer links to subtract
    return result_count > nav_links or debug["visible_big"] > 8


def try_search(page, title, year):
    """Search with progressively simpler queries until results are found."""
    queries = build_search_queries(title, year)
    print(f"[*] Search queries to try: {queries}")

    search_el, selector = find_search_input(page)
    if not search_el:
        print("[-] Could not find search input")
        return False

    print(f"[+] Found search input: {selector}")
    homepage_url = page.url

    for i, query in enumerate(queries):
        print(f"\n[*] Attempt {i+1}/{len(queries)}: searching for {query!r}")

        if i > 0:
            page.goto(homepage_url, wait_until="domcontentloaded", timeout=15000)
            time.sleep(2)
            search_el, _ = find_search_input(page)
            if not search_el:
                print("[-] Lost search input after navigation")
                return False

        submit_search(page, search_el, query)
        screenshot(page, f"03-search-results-{i+1}")

        if has_results(page):
            print(f"[+] Got results for: {query!r}")
            return True
        else:
            print(f"    No results for: {query!r}")

    print("[-] All search queries exhausted, no results found")
    return False


def find_result(page, title, year):
    """Find result cards (any clickable element with text), score by title/year match."""
    results = page.evaluate(
        """(args) => {
            const [year] = args;
            // Strategy 1: <a> tags with href
            const links = [...document.querySelectorAll('a[href]')]
                .filter(a => {
                    const rect = a.getBoundingClientRect();
                    return rect.width > 30 && rect.height > 30;
                })
                .map(a => ({
                    text: a.innerText.trim().substring(0, 120),
                    href: a.href,
                    selector: null,
                    hasYear: a.innerText.includes(year) || a.href.includes(year),
                }));

            // Strategy 2: card-like elements (divs with class containing card/poster/item)
            const cardSels = [
                '[class*="card"]', '[class*="poster"]', '[class*="item"]',
                '[class*="result"]', '[class*="movie"]', '[class*="film"]',
            ];
            const cards = [];
            for (const sel of cardSels) {
                for (const el of document.querySelectorAll(sel)) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width < 50 || rect.height < 50) continue;
                    const text = el.innerText.trim();
                    if (!text) continue;
                    // Find link inside card, or use card itself
                    const link = el.querySelector('a[href]');
                    const href = link ? link.href : '';
                    // Avoid duplicates with link results
                    if (href && links.some(l => l.href === href)) continue;
                    cards.push({
                        text: text.substring(0, 120),
                        href: href,
                        selector: `${el.tagName}.${el.className.split(' ')[0]}`,
                        hasYear: text.includes(year) || href.includes(year),
                    });
                }
            }

            return [...links, ...cards]
                .filter(r => r.text.length > 1)
                .slice(0, 40);
        }""",
        [str(year)],
    )

    if not results:
        print("[-] No result cards found on page")
        return []

    # Score results: prefer year match + title similarity
    title_lower = title.lower().replace(".", "")
    title_words = {w for w in title_lower.split() if len(w) > 2}

    def score(r):
        text = r["text"].lower().replace(".", "")
        href = r["href"].lower() if r["href"] else ""
        s = 0
        if r["hasYear"]:
            s += 10
        matched = sum(1 for w in title_words if w in text or w in href)
        s += matched * 3
        if title_lower in text:
            s += 20
        # Penalize nav/footer items
        if any(nav in text for nav in ["home", "dmca", "discord", "movies", "tv shows", "welcome"]):
            s -= 50
        return s

    results.sort(key=score, reverse=True)
    # Filter out negatively scored items
    results = [r for r in results if score(r) > 0]

    print(f"[+] Found {len(results)} result cards (sorted by relevance):")
    for i, r in enumerate(results[:10]):
        yr = " [year]" if r["hasYear"] else ""
        print(f"      [{i}] {r['text'][:70]}{yr}")
        if r["href"]:
            print(f"           {r['href']}")
    return results


def click_result(page, results, index=0):
    """Click on a search result by index."""
    if index >= len(results):
        print(f"[-] Index {index} out of range (have {len(results)} results)")
        return False

    target = results[index]
    print(f"[*] Clicking result [{index}]: {target['text'][:60]}")

    if target.get("href"):
        page.goto(target["href"], wait_until="domcontentloaded", timeout=30000)
    else:
        # Click the card element directly using arg passing (avoids JS injection)
        page.evaluate(
            """(snippet) => {
                const sels = '[class*="card"], [class*="item"], [class*="poster"]';
                const els = [...document.querySelectorAll(sels)];
                const match = els.find(el => el.innerText.trim().startsWith(snippet));
                if (match) {
                    const link = match.querySelector('a') || match;
                    link.click();
                }
            }""",
            target["text"][:40],
        )
        page.wait_for_load_state("domcontentloaded", timeout=15000)

    time.sleep(3)
    screenshot(page, "04-movie-page")
    print(f"    URL: {page.url}")
    print(f"    Title: {page.title()}")
    return True


def get_extension_worker(ctx):
    """Find the extension's service worker from browser context."""
    for w in ctx.service_workers:
        if "chrome-extension" in w.url:
            return w
    return None


def click_watch_now(page, server_num=2):
    """Find and click Watch Now, select server, and click play in the video iframe."""
    # Step 1: Click Watch Now
    watch_selectors = [
        "text=Watch Now",
        "text=WATCH NOW",
        "a:has-text('Watch Now')",
        "button:has-text('Watch Now')",
        ".watch-btn",
        "a[href*='watch']",
        "button:has-text('Play')",
        "a:has-text('Play Now')",
    ]
    clicked = False
    for sel in watch_selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                print(f"[+] Found watch button: {sel}")
                el.click()
                clicked = True
                time.sleep(3)
                break
        except Exception:
            continue
    if not clicked:
        print("[-] Could not find Watch Now button")
        return False

    # Step 2: Select server
    server_selectors = [
        f"text=Server {server_num}",
        f"button:has-text('Server {server_num}')",
        f".server-button:nth-child({server_num})",
        f"button.server-btn:nth-child({server_num})",
    ]
    for sel in server_selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                print(f"[+] Selecting Server {server_num}: {sel}")
                el.click()
                time.sleep(3)
                break
        except Exception:
            continue

    # Step 3: Click play button inside video iframe
    time.sleep(3)
    screenshot(page, "06a-before-play-click")

    iframe_el = page.query_selector(
        "iframe[src*='embed'], iframe[src*='vidsrc'], "
        "iframe[src*='player'], iframe#video-player, "
        "#video-player iframe, iframe"
    )
    if iframe_el:
        iframe_src = iframe_el.get_attribute("src") or "none"
        print(f"[+] Found video iframe: {iframe_src[:100]}")
        box = iframe_el.bounding_box()

        frame = iframe_el.content_frame()
        if frame:
            print("    iframe content_frame accessible (same-origin or permitted)")
            play_clicked = False
            play_selectors = [
                "button.player-btn",
                ".play-button",
                "[aria-label='Play']",
                "button[title='Play']",
                ".vjs-big-play-button",
                ".jw-icon-display",
                "#player button",
            ]
            for sel in play_selectors:
                try:
                    play_el = frame.query_selector(sel)
                    if play_el and play_el.is_visible():
                        print(f"    Clicking iframe play: {sel}")
                        play_el.click()
                        play_clicked = True
                        time.sleep(2)
                        break
                except Exception:
                    continue
            if not play_clicked and box:
                print("    No play button found in frame, clicking center of iframe...")
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                time.sleep(2)
        elif box:
            print("    iframe cross-origin (content_frame=None), clicking center...")
            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            time.sleep(3)
            # Sometimes need a second click after an ad/overlay
            screenshot(page, "06b-after-first-click")
            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            time.sleep(2)
    else:
        print("[*] No iframe found, clicking center of video area...")
        page.mouse.click(760, 450)
        time.sleep(2)

    return True


def wait_for_pending_capture(ctx, timeout=45):
    """Wait for extension to intercept an m3u8 URL (check via service worker)."""
    print(f"[*] Waiting up to {timeout}s for m3u8 interception...")
    worker = get_extension_worker(ctx)
    if not worker:
        print("[-] Extension service worker not found")
        return False

    start = time.time()
    while time.time() - start < timeout:
        try:
            count = worker.evaluate("() => pendingCaptures.length")
            if count > 0:
                info = worker.evaluate(
                    "() => pendingCaptures.map(c => ({url: c.m3u8_url?.substring(0, 80), page: c.page_url?.substring(0, 80)}))"
                )
                print(f"[+] Extension captured {count} m3u8 URL(s):")
                for c in info:
                    print(f"      m3u8: {c.get('url', '?')}")
                    print(f"      page: {c.get('page', '?')}")
                return True
        except Exception as e:
            if "Target closed" in str(e):
                print("[-] Service worker closed, retrying...")
                worker = get_extension_worker(ctx)
                if not worker:
                    break
        time.sleep(2)

    # Debug: check what the extension has seen
    if worker:
        try:
            seen = worker.evaluate("() => [...seenM3u8].slice(0, 10)")
            print(f"    [debug] Extension seenM3u8: {seen}")
        except Exception:
            pass
        try:
            pending = worker.evaluate("() => pendingCaptures.length")
            print(f"    [debug] pendingCaptures.length: {pending}")
        except Exception:
            pass
    print(f"[-] No m3u8 captured after {timeout}s")
    return False


def confirm_capture(ctx):
    """Confirm the first pending capture via the extension's service worker."""
    worker = get_extension_worker(ctx)
    if not worker:
        print("[-] Extension service worker not found")
        return False

    try:
        worker.evaluate("""() => {
            chrome.runtime.sendMessage({action: "confirmDownload", index: 0});
        }""")
        print("[+] Confirmed capture — download triggered")
        return True
    except Exception as e:
        print(f"[-] Failed to confirm capture: {e}")
        return False


def monitor_downloads(timeout=300):
    """Monitor HLS server downloads until complete or timeout."""
    print(f"[*] Monitoring downloads (timeout {timeout}s)...")
    start = time.time()
    last_status = ""
    while time.time() - start < timeout:
        try:
            r = requests.get(f"{HLS_SERVER}/downloads", timeout=3)
            downloads = r.json()
            if not downloads:
                time.sleep(3)
                continue

            active = [d for d in downloads if d.get("status") in ("downloading", "queued")]
            done = [d for d in downloads if d.get("status") == "done"]
            failed = [d for d in downloads if d.get("status") == "error"]

            status = f"active={len(active)} done={len(done)} failed={len(failed)}"
            if status != last_status:
                print(f"    [{int(time.time()-start)}s] {status}")
                for d in active:
                    prog = d.get("progress", "?")
                    print(f"      DL: {d.get('filename', '?')} — {prog}")
                for d in done:
                    print(f"      DONE: {d.get('filename', '?')}")
                for d in failed:
                    print(f"      FAIL: {d.get('filename', '?')} — {d.get('error', '?')}")
                last_status = status

            if not active and (done or failed):
                print(f"\n[+] All downloads finished: {len(done)} done, {len(failed)} failed")
                return True
        except Exception:
            pass
        time.sleep(3)

    print(f"\n[-] Download monitoring timed out after {timeout}s")
    return False


def main():
    parser = argparse.ArgumentParser(description="Auto-download movies/shows via stealth browser")
    parser.add_argument("title", nargs="?", default="E.T. the Extra-Terrestrial")
    parser.add_argument("year", nargs="?", default="1982")
    parser.add_argument("--site", choices=list(SITES.keys()), default="brocoflix")
    parser.add_argument("--server", type=int, default=2, help="Server number to use (default: 2)")
    parser.add_argument("--no-search", action="store_true", help="Just open the site, don't search")
    parser.add_argument("--keep-open", action="store_true", help="Keep browser open after done")
    args = parser.parse_args()

    print(f"=== Auto-Download PoC ===")
    print(f"  Title: {args.title}")
    print(f"  Year:  {args.year}")
    print(f"  Site:  {args.site} ({SITES[args.site]})")
    print()

    check_hls_server()
    print()

    print(f"[*] Launching CloakBrowser (stealth Chromium 145)...")
    print(f"    Extension: {EXTENSION_PATH}")
    print(f"    Profile:   {PROFILE_PATH}")
    ctx = launch_persistent_context(
        PROFILE_PATH,
        headless=False,
        humanize=True,
        human_preset="careful",
        args=[
            f"--disable-extensions-except={EXTENSION_PATH}",
            f"--load-extension={EXTENSION_PATH}",
        ],
    )

    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        site_url = SITES[args.site]
        print(f"\n[*] Navigating to {site_url}...")
        page.goto(site_url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        screenshot(page, "01-homepage")
        print(f"    Page title: {page.title()}")
        print(f"    URL: {page.url}")

        if args.no_search:
            print("\n[*] --no-search: skipping search, dumping page info")
            dump_inputs(page)
            dump_links(page, "search")
        else:
            print(f"\n[*] Searching for: {args.title} ({args.year})")
            found = try_search(page, args.title, args.year)

            if found:
                print(f"\n[*] Search results page: {page.url}")
                results = find_result(page, args.title, args.year)
                if results:
                    if not click_result(page, results, index=0):
                        raise SystemExit("Failed to navigate to movie page")
                else:
                    raise SystemExit("No matching results found")
            else:
                print("\n[-] Could not find search box. Dumping page info:")
                dump_inputs(page)
                raise SystemExit("Search failed")

            # Step 5: Click Watch Now
            print(f"\n{'='*50}")
            print(f"[*] Step 5: Starting playback...")
            screenshot(page, "05-before-watch")
            if not click_watch_now(page, server_num=args.server):
                raise SystemExit("Could not find Watch Now button")

            time.sleep(5)
            screenshot(page, "06-player-loaded")
            print(f"    URL: {page.url}")

            # Step 6: Wait for extension to capture m3u8
            print(f"\n[*] Step 6: Waiting for extension to capture stream...")
            if not wait_for_pending_capture(ctx, timeout=45):
                screenshot(page, "07-no-capture")
                print("[!] No m3u8 captured. The video player may need interaction.")
                print("[!] Check if there's a play button inside the video iframe.")
                if args.keep_open:
                    print("[*] Browser staying open for manual inspection.")
                    while True:
                        time.sleep(1)
                raise SystemExit("No m3u8 captured")

            # Step 7: Confirm capture to trigger download
            print(f"\n[*] Step 7: Confirming capture to start download...")
            if not confirm_capture(ctx):
                raise SystemExit("Failed to confirm capture")

            time.sleep(2)

            # Step 8: Monitor download progress
            print(f"\n[*] Step 8: Monitoring download...")
            monitor_downloads(timeout=600)

        if args.keep_open:
            print("\n[*] Browser staying open. Press Ctrl+C to close.")
            while True:
                time.sleep(1)
        else:
            print("\n[*] Done! Closing in 5s...")
            time.sleep(5)

    except KeyboardInterrupt:
        print("\n[*] Interrupted")
    finally:
        print("[*] Closing browser...")
        ctx.close()


if __name__ == "__main__":
    import functools
    print = functools.partial(print, flush=True)
    main()
