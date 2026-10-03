"""Explicit public-read probes. No private accounts, submissions, or scheduling."""

import asyncio
import json
import time

from linkedin_bot.adapters.ats import ATSSource, Board
from linkedin_bot.adapters.company import board_links
from linkedin_bot.adapters.public_http import AdapterError, PublicHTTP
from linkedin_bot.domain.models import now


async def main() -> None:
    http = PublicHTTP()
    results: dict[str, object] = {
        "observed_at": now().isoformat(),
        "adapter_version": "0.1.0",
        "scope": "bounded public GETs only",
    }
    samples = [
        (Board(provider="greenhouse", slug="astranis"), "https://www.astranis.com/careers"),
        (
            Board(provider="lever", slug="verygoodsecurity"),
            "https://www.verygoodsecurity.com/careers",
        ),
        (Board(provider="ashby", slug="Linear"), "https://linear.app/careers"),
    ]
    try:
        for board, website in samples:
            started = time.monotonic()
            record: dict[str, object] = {"board": board.model_dump(), "company_page": website}
            try:
                page = await ATSSource(http, board).collect(limit=20)
                record.update(
                    {
                        "status": "passed",
                        "count": len(page.items),
                        "coverage": page.coverage,
                        "next_cursor": page.next_cursor,
                        "latency_seconds": round(time.monotonic() - started, 3),
                        "sample_public_urls": [item.url for item in page.items[:2]],
                    }
                )
                if board.provider == "greenhouse" and page.items:
                    endpoint = (
                        f"https://boards-api.greenhouse.io/v1/boards/{board.slug}/jobs/"
                        f"{page.items[0].external_id}?questions=true"
                    )
                    details = json.loads(await http.get(endpoint))
                    record["form_metadata"] = {
                        "status": "passed_metadata_only",
                        "job_id": details.get("id"),
                        "question_count": len(details.get("questions", [])),
                        "field_types": sorted(
                            {
                                field["type"]
                                for q in details.get("questions", [])
                                for field in q.get("fields", [])
                            }
                        ),
                        "qualification": "metadata only; browser form layouts remain pending",
                    }
            except AdapterError as exc:
                record.update(
                    {"status": "failed", "reason": exc.code, "http_status": exc.http_status}
                )
            try:
                company = (await http.get(website)).decode("utf-8", errors="replace")
                markers = {
                    "greenhouse": "greenhouse.io",
                    "lever": "jobs.lever.co",
                    "ashby": "jobs.ashbyhq.com",
                }
                record["company_page_bytes"] = len(company.encode())
                links = board_links(company, website, board)
                record["official_board_link_count"] = len(links)
                record["official_board_links"] = links[:3]
                record["board_domain_mentioned"] = markers[board.provider] in company
                record["qualification"] = (
                    "exact board link verified on selected official page"
                    if links
                    else "exact board linkage remains unverified"
                )
            except AdapterError as exc:
                record["company_page_reason"] = exc.code
            results[f"{board.provider}:{board.slug}"] = record
        try:
            search = await http.get(
                "https://html.duckduckgo.com/html/?q=python+internship+startup+warsaw"
            )
            text = search.decode("utf-8", errors="replace")
            results["search"] = {
                "status": "passed" if 'class="result__a"' in text else "unverified_response",
                "bytes": len(search),
                "qualification": "generic synthetic query; goals not onboarded",
            }
        except AdapterError as exc:
            results["search"] = {
                "status": "failed",
                "reason": exc.code,
                "http_status": exc.http_status,
            }
    finally:
        await http.close()
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
