"""Isolated browser automation worker node with SSRF guards, domain allowlists, and Playwright."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from pydantic import Field

from flowpilot import (
    BaseNode,
    ConfigurationError,
    ExecutionContext,
    NodeConfigModel,
    NodeMetadata,
    NodeResult,
    validate_outbound_url,
)
from flowpilot_schema import NodeKind, RiskLevel, SideEffectPolicy, WorkerPool


@dataclass(frozen=True)
class BrowserExtractResult:
    url: str
    title: str
    extracted: dict[str, str] = field(default_factory=dict)
    text_snippet: str = ""
    elapsed_ms: int = 0


class BrowserDriver(Protocol):
    async def navigate_and_extract(
        self,
        *,
        url: str,
        selectors: dict[str, str],
        wait_for_selector: str | None,
        timeout_ms: int,
    ) -> BrowserExtractResult: ...


class PlaywrightBrowserDriver:
    """Headless Chromium driver backed by Playwright (runs inside isolated browser worker pool)."""

    async def navigate_and_extract(
        self,
        *,
        url: str,
        selectors: dict[str, str],
        wait_for_selector: str | None,
        timeout_ms: int,
    ) -> BrowserExtractResult:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ConfigurationError(
                "Playwright is not installed in this environment; install 'playwright' "
                "inside the isolated browser worker image."
            ) from exc

        t0 = time.monotonic()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                if wait_for_selector:
                    await page.wait_for_selector(wait_for_selector, timeout=timeout_ms)
                title = await page.title()
                extracted: dict[str, str] = {}
                for key, css in selectors.items():
                    loc = page.locator(css).first
                    extracted[key] = (await loc.inner_text(timeout=timeout_ms)).strip()
                body_text = (await page.locator("body").inner_text(timeout=timeout_ms))[:2000]
                elapsed_ms = max(0, int((time.monotonic() - t0) * 1000))
                return BrowserExtractResult(
                    url=page.url,
                    title=title,
                    extracted=extracted,
                    text_snippet=body_text,
                    elapsed_ms=elapsed_ms,
                )
            finally:
                await browser.close()


class BrowserNavigateExtractConfig(NodeConfigModel):
    url: str = Field(..., min_length=8, max_length=4096)
    selectors: dict[str, str] = Field(default_factory=dict)
    wait_for_selector: str | None = None
    allowed_domains: list[str] = Field(default_factory=list)
    timeout_ms: int = Field(default=15_000, ge=500, le=120_000)


class BrowserAutomationNode(BaseNode[BrowserNavigateExtractConfig, Any]):
    """Browser automation node routed strictly to WorkerPool.BROWSER with SSRF protection."""

    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="browser.navigate_extract",
        name="Browser Navigate & Extract",
        description="Navigates to an allowed URL in an isolated browser worker and extracts DOM.",
        category="browser",
        kind=NodeKind.BROWSER,
        worker_pool=WorkerPool.BROWSER,
        side_effect=SideEffectPolicy.NONE,
        risk_level=RiskLevel.MEDIUM,
        required_permissions=("browser.automate",),
    )
    config_model: ClassVar[type[BrowserNavigateExtractConfig]] = BrowserNavigateExtractConfig

    async def execute(
        self,
        config: BrowserNavigateExtractConfig,
        ctx: ExecutionContext,
    ) -> NodeResult:
        # Enforce SSRF, private-network, and domain allowlist BEFORE launching browser
        driver: BrowserDriver | None = ctx.services.get("browser_driver")
        validate_outbound_url(
            config.url,
            allowed_domains=config.allowed_domains,
            allow_private_networks=False,
            resolve_dns=driver is None,
        )

        active_driver: BrowserDriver = driver or PlaywrightBrowserDriver()
        res = await active_driver.navigate_and_extract(
            url=config.url,
            selectors=config.selectors,
            wait_for_selector=config.wait_for_selector,
            timeout_ms=config.timeout_ms,
        )
        ctx.record_usage("browser_time_ms", float(res.elapsed_ms), unit="ms")
        ctx.log("INFO", f"Browser extracted {len(res.extracted)} field(s) from {res.url}")
        return NodeResult(
            output={
                "url": res.url,
                "title": res.title,
                "extracted": res.extracted,
                "text_snippet": res.text_snippet,
                "elapsed_ms": res.elapsed_ms,
            }
        )
