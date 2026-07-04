"""scanner/browser.py -- Playwright/nodriver browser fetch primitives."""
import asyncio
import glob
import shutil
import subprocess
import time
from typing import Optional

from scanner.config import log, _PW_UA, _PW_CTX_HEADERS


def _pw_fetch_page(
    page,
    url: str,
    wait_ms: int = 2500,
    goto_timeout_ms: int = 60_000,
) -> Optional[str]:
    """Navigate an existing Playwright page and return HTML. Caller owns page lifecycle."""
    from playwright.sync_api import TimeoutError as PWTimeout, Error as PWError
    content = None
    try:
        page.goto(url, timeout=goto_timeout_ms, wait_until="domcontentloaded")
        page.wait_for_timeout(wait_ms)
        content = page.content()
    except PWTimeout:
        try:
            content = page.content()
        except Exception:
            pass
    except PWError as exc:
        log.warning("Playwright page error for %s: %s", url, exc)
    except Exception as exc:
        log.warning("Playwright page navigation failed for %s: %s", url, exc)
    return content


class _ChromeLaunchError(RuntimeError):
    """Raised when nodriver cannot start/connect to Chrome at all.
    Distinct from page-level failures (403, empty content, timeout) so the
    caller can decide whether to clean up orphaned processes and retry."""


async def _nd_fetch(
    url: str,
    wait_ms: int = 2000,
    goto_timeout_ms: int = 60_000,
    wait_selector: Optional[str] = None,
    user_data_dir: Optional[str] = None,
) -> Optional[str]:
    """Async nodriver fetch -- launches real Chrome, returns HTML string.

    Raises _ChromeLaunchError if Chrome itself cannot be started so the
    caller (_pw_fetch) can clean up and retry.  All other failures return None.
    """
    import nodriver as uc
    browser = None
    try:
        try:
            # Build browser_args list; inject --user-data-dir as a flag rather than
            # as a uc.start() kwarg -- the kwarg path triggers a StopIteration bug
            # in some nodriver versions when the profile dir already exists.
            _nd_browser_args = ["--disable-http2", "--no-sandbox"]
            if user_data_dir:
                _nd_browser_args.append(f"--user-data-dir={user_data_dir}")
            browser = await uc.start(
                headless=False,
                browser_args=_nd_browser_args,
                sandbox=False,
            )
        except Exception as launch_exc:
            # Re-raise as a typed error so _pw_fetch can handle it specifically
            raise _ChromeLaunchError(str(launch_exc)) from launch_exc

        tab = await asyncio.wait_for(
            browser.get(url),
            timeout=goto_timeout_ms / 1000,
        )

        if wait_selector:
            # Wait for a real page element -- confirms challenge has resolved
            try:
                await asyncio.wait_for(
                    tab.select(wait_selector),
                    timeout=min(goto_timeout_ms / 1000, 30.0),
                )
                log.debug("nodriver: VDP element found, grabbing content: %s",
                          url.split("/")[-2][:12])
            except asyncio.TimeoutError:
                log.debug("nodriver: VDP element wait timed out, grabbing partial: %s",
                          url.split("/")[-2][:12])
                # Fall back to time-based wait
                if wait_ms > 0:
                    await asyncio.sleep(wait_ms / 1000)
            except Exception:
                # selector unsupported or other error -- fall back
                if wait_ms > 0:
                    await asyncio.sleep(wait_ms / 1000)
        else:
            if wait_ms > 0:
                await asyncio.sleep(wait_ms / 1000)

        content = await tab.get_content()
        if content and len(content) < 5000:
            try:
                http_status = await asyncio.wait_for(
                    tab.evaluate(
                        "(performance.getEntriesByType('navigation')[0]||{}).responseStatus||'?'"
                    ),
                    timeout=2.0,
                )
            except Exception:
                http_status = "?"
            _short_log = log.debug if "autotrader.com" in url else log.warning
            _short_log(
                "nodriver: short response (HTTP %s, %d bytes) for %s -- first 500 chars: %s",
                http_status, len(content), url, content[:500],
            )
            # Return the short content rather than None so callers (e.g. cars.com VDP
            # parser) can still extract the page <title> and apply trim filters.
            # _parse_cars_vdp_soup already handles sparse/bot-challenge pages gracefully.
            return content
        return content if content else None
    except _ChromeLaunchError:
        raise  # propagate upward -- do NOT catch here
    except Exception as exc:
        exc_str = str(exc)
        if "ERR_EMPTY_RESPONSE" in exc_str or "ERR_CONNECTION_REFUSED" in exc_str:
            log.warning("nodriver: connection refused (likely IP blocked): %s",
                        url.split("/")[-2][:12])
        else:
            log.warning("nodriver fetch failed for %s: %s", url, exc)
        return None
    finally:
        if browser:
            try:
                await browser.stop()
            except Exception:
                pass


def _quiet_exception_handler(loop, context):
    exc = context.get("exception")
    if exc is None:
        return
    if isinstance(exc, Exception) and exc.__class__.__name__ in (
        "ConnectionClosedError", "ConnectionClosedOK", "TimeoutError"
    ):
        return
    msg = context.get("message", "")
    if "Task exception was never retrieved" in msg:
        exc_type = type(exc).__name__ if exc else ""
        if exc_type in ("ConnectionClosedError", "ConnectionClosedOK", "TimeoutError"):
            return
    loop.default_exception_handler(context)


def _pw_fetch(
    url: str,
    wait_ms: int = 2000,
    wait_selector: Optional[str] = None,
    wait_until: str = "domcontentloaded",
    headless: bool = True,
    goto_timeout_ms: int = 60_000,
    user_data_dir: Optional[str] = None,
) -> Optional[str]:
    """Fetch page content using nodriver (real Chrome binary, bot-detection stripped).

    If Chrome fails to start (_ChromeLaunchError), cleans up stale temp-profile
    dirs and retries exactly once before giving up.
    """
    def _run_once() -> Optional[str]:
        loop = asyncio.new_event_loop()
        loop.set_exception_handler(_quiet_exception_handler)
        try:
            return loop.run_until_complete(
                _nd_fetch(url, wait_ms=wait_ms, goto_timeout_ms=goto_timeout_ms,
                          wait_selector=wait_selector, user_data_dir=user_data_dir)
            )
        finally:
            # Cancel any lingering nodriver background tasks before closing
            try:
                pending = asyncio.all_tasks(loop)
                if pending:
                    for task in pending:
                        task.cancel()
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:
                pass
            loop.close()

    try:
        return _run_once()
    except _ChromeLaunchError as exc:
        log.warning(
            "nodriver: Chrome failed to start (%s) -- cleaning up orphans and retrying once...",
            exc,
        )
        _kill_orphaned_browsers()
        try:
            return _run_once()
        except _ChromeLaunchError as exc2:
            log.warning("nodriver: Chrome still won't start after cleanup: %s", exc2)
            return None
        except Exception as exc2:
            log.warning("nodriver fetch failed (retry) for %s: %s", url, exc2)
            return None
    except Exception as exc:
        log.warning("nodriver fetch failed for %s: %s", url, exc)
        return None


def _kill_orphaned_browsers() -> None:
    """Kill Chrome instances launched by nodriver and wipe stale temp profile dirs.

    Targets only nodriver-spawned temp-profile processes (user-data-dir=/tmp/uc_*),
    not the user's regular Chrome.  Also removes stale /tmp/uc_* dirs whose lock
    files would prevent Chrome from starting a fresh session.
    """
    # 1. Kill orphaned Chrome processes
    killed = 0
    try:
        result = subprocess.run(
            ["pgrep", "-f", "user-data-dir=/tmp/uc_"],
            capture_output=True, text=True,
        )
        pids = [p.strip() for p in result.stdout.strip().split("\n") if p.strip()]
        for pid in pids:
            try:
                subprocess.run(["kill", "-9", pid], capture_output=True)
                killed += 1
            except Exception:
                pass
    except Exception:
        pass
    if killed:
        log.debug("Killed %d orphaned nodriver Chrome process(es)", killed)
        # Give OS time to release file handles before wiping dirs
        time.sleep(1.0)

    # 2. Wipe stale temp profile dirs -- Chrome refuses to start if a
    #    SingletonLock or SingletonCookie is left behind in these dirs.
    wiped = 0
    for pattern in ("/tmp/uc_*", "/private/tmp/uc_*"):
        for d in glob.glob(pattern):
            try:
                shutil.rmtree(d, ignore_errors=True)
                wiped += 1
            except Exception:
                pass
    if wiped:
        log.debug("Wiped %d stale nodriver temp profile dir(s)", wiped)

    time.sleep(1.5)
