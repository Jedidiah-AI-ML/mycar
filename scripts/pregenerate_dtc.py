"""Pre-generate the top-50 generic explanations (Phase 4 step 4) and the review sheet.

    docker compose up -d db
    cd backend
    uv run python ../scripts/pregenerate_dtc.py            # real calls: about $0.05
    uv run python ../scripts/pregenerate_dtc.py --sheet    # only rewrite the review sheet

- Uses LLM_MODEL / LLM_API_KEY from .env. Calls are logged in the **local** database's
  `llm_calls` (and count against the monthly cap there). It refuses a non-local database
  unless --allow-remote is given.
- Keeps entries that are still valid (no re-buying); --force regenerates everything.
- Writes backend/app/config/dtc/explanations.v1.yaml and docs/review/dtc_explanations_v1.md.
Exit code 1 if any explanation failed validation or grounding (those are never stored).
"""

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOCAL_DB = "postgresql+asyncpg://autocare:autocare@localhost:5433/autocare"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="regenerate valid entries too")
    ap.add_argument("--sheet", action="store_true", help="only rewrite the review sheet")
    ap.add_argument("--codes", nargs="+", help="only these codes (a trial run)")
    ap.add_argument("--allow-remote", action="store_true", help="use .env's DATABASE_URL")
    args = ap.parse_args()
    if not args.allow_remote:
        os.environ["DATABASE_URL"] = LOCAL_DB  # before the settings are first read
    sys.path.insert(0, str(REPO / "backend"))
    return asyncio.run(run(args))


async def run(args) -> int:
    from sqlalchemy import func, select

    from app.db.session import get_sessionmaker
    from app.llm import client
    from app.models import LlmCall
    from app.services.dtc import definitions as dd
    from app.services.dtc import pregenerated as pg
    from app.services.dtc.review_sheet import write_sheet

    sheet = REPO / "docs" / "review" / "dtc_explanations_v1.md"
    if args.sheet:
        write_sheet(sheet)
        print(f"wrote {sheet.relative_to(REPO)}")
        return 0
    if not client.enabled():
        print("LLM_API_KEY is not set: nothing to do.")
        return 1

    started = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        report = await pg.pregenerate(session, args.codes or dd.top50(), force=args.force)
        await session.commit()
        n, tin, tout, cost = (
            await session.execute(
                select(func.count(), func.coalesce(func.sum(LlmCall.tokens_in), 0),
                       func.coalesce(func.sum(LlmCall.tokens_out), 0),
                       func.coalesce(func.sum(LlmCall.cost_usd), 0))
                .where(LlmCall.ts >= started)
            )
        ).one()  # fmt: skip
    write_sheet(sheet)

    print(f"generated {len(report.generated)}, kept {len(report.kept)}, "
          f"failed {len(report.failed)}")  # fmt: skip
    print(f"calls {n}, tokens in {tin} / out {tout}, cost ${float(cost):.4f}")
    for (code, status), problems in report.failed.items():
        print(f"  FAILED {code} {status}: {'; '.join(problems)}")
    if report.stopped:
        print(f"stopped early: {report.stopped} {report.stop_reason or ''}".strip())
    print(f"wrote {pg.FILE.relative_to(REPO)} and {sheet.relative_to(REPO)}")
    return 1 if report.failed or report.stopped else 0


if __name__ == "__main__":
    sys.exit(main())
