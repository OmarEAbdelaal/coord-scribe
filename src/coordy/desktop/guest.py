"""Visible, listen-only browser guest. No bypass of sign-in, lobby or host policy."""
from pathlib import Path
from urllib.parse import urlparse


def provider_for(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("Use an HTTPS meeting invitation URL")
    if host in {"teams.microsoft.com", "teams.live.com"}:
        return "Teams"
    if host == "meet.google.com":
        return "Google Meet"
    if host == "zoom.us" or host.endswith(".zoom.us"):
        return "Zoom"
    raise ValueError("Supported browser guests: Teams, Google Meet and Zoom")


def join_guest(url, name, stop, emit):
    """Keep browser ownership on this worker until Leave guest is requested."""
    provider = provider_for(url)
    if not name.strip():
        raise ValueError("Enter a visible participant name")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        # Windows Edge is already installed; this needs no bundled browser download.
        browser = playwright.chromium.launch(channel="msedge", headless=False,
            args=['--use-fake-ui-for-media-stream=deny'])
        try:
            # Fresh, nonpersistent context: never reuse personal logins/camera permissions.
            context = browser.new_context(permissions=[], accept_downloads=False)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            emit("guest", f"{provider} guest browser open. Complete any required sign-in or host admission in that window.")
            attempted_name = False
            requested = False
            reported_joined = False
            while not stop.is_set() and browser.is_connected() and not page.is_closed():
                # Host UIs vary; missing controls are left for the user in the visible window.
                for label in ["Continue on this browser", "Join on the web instead", "Join from Your Browser"]:
                    button = page.get_by_text(label, exact=False).first
                    if button.is_visible():
                        button.click(timeout=3000)
                        break
                if not attempted_name:
                    for label in ["Your name", "Enter your name", "Type your name"]:
                        field = page.get_by_placeholder(label, exact=False).first
                        if field.is_visible():
                            field.fill(name, timeout=3000)
                            attempted_name = True
                            break
                if attempted_name and not requested:
                    for label in ["Join now", "Ask to join", "Join"]:
                        button = page.get_by_role("button", name=label, exact=True).first
                        if button.is_visible() and button.is_enabled():
                            button.click(timeout=3000)
                            requested = True
                            emit("guest", f"{provider}: admission requested as {name}. Waiting for host.")
                            break
                if not reported_joined:
                    for label in ["Leave call", "Leave", "Leave meeting"]:
                        if page.get_by_role("button", name=label, exact=True).first.is_visible():
                            reported_joined = True
                            emit("guest_joined", f"{provider}: joined as {name}. Browser guest is listen-only.")
                            break
                page.wait_for_timeout(500)
        finally:
            browser.close()
    return "Guest browser closed"
