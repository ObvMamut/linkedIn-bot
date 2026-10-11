import asyncio
import logging

from pydantic import ValidationError

from linkedin_bot.adapters.ats import ATSSource, Board
from linkedin_bot.adapters.public_http import AdapterError, PublicHTTP
from linkedin_bot.domain.models import now
from linkedin_bot.storage.database import Database


async def run_one(database: Database, http: PublicHTTP) -> bool:
    work = await database.claim(now())
    if work is None:
        return False
    try:
        board = Board.model_validate_json(work.payload)
        async with asyncio.timeout(max(1, (work.deadline - now()).total_seconds())):
            page = await ATSSource(http, board).collect_board(cursor=work.checkpoint)
        await database.persist_page(work, page, now())
        logging.info("", extra={"event": "read_finished", "count": len(page.items)})
    except AdapterError as exc:
        await database.fail_work(work, exc.code, exc.transient, now())
        logging.warning("", extra={"event": "read_failed"})
    except ValidationError:
        await database.fail_work(work, "invalid_work_payload", False, now())
    except TimeoutError:
        await database.fail_work(work, "deadline", True, now())
    return True
