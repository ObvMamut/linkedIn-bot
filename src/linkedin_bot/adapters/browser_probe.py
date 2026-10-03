"""Local synthetic browser smoke check. Never accesses a LinkedIn session."""

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from linkedin_bot.adapters.public_http import AdapterError


async def browser_smoke(executable: str | None = None) -> dict[str, object]:
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                executable_path=executable,
                headless=True,
                timeout=15000,
            )
        except PlaywrightError as exc:
            raise AdapterError("browser_unavailable") from exc
        try:
            page = await browser.new_page()
            await page.route("**/*", lambda route: route.abort())
            await page.set_content(
                "<html><body><label>Name<input name='name'></label>"
                "<button type='button' disabled>Submit</button></body></html>"
            )
            await page.get_by_label("Name").fill("Synthetic candidate")
            pdf = await page.pdf()
            return {
                "browser_version": browser.version,
                "input_roundtrip": await page.get_by_label("Name").input_value()
                == "Synthetic candidate",
                "submit_disabled": await page.get_by_role("button").is_disabled(),
                "pdf_header": pdf.startswith(b"%PDF"),
                "pdf_bytes": len(pdf),
                "scope": "local synthetic page; no live form or LinkedIn capability verified",
            }
        finally:
            await browser.close()
