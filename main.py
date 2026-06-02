import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import auth, players, roster_picks, transactions, boxscores, bets, proposals, misc, tips, perry, strikes, draft

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

logger = logging.getLogger("nbn-api")


async def _draft_scheduler():
    while True:
        await asyncio.sleep(30)
        try:
            draft.auto_submit_loop_sync()
        except Exception as e:
            logger.error("Draft scheduler error: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_draft_scheduler())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://nbn.today"],
    allow_methods=["GET", "PUT", "POST", "DELETE", "PATCH"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(auth.router)
app.include_router(players.router)
app.include_router(roster_picks.router)
app.include_router(transactions.router)
app.include_router(boxscores.router)
app.include_router(bets.router)
app.include_router(proposals.router)
app.include_router(misc.router)
app.include_router(tips.router)
app.include_router(perry.router)
app.include_router(strikes.router)
app.include_router(draft.router)
