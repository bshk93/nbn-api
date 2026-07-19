import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import auth, players, roster_picks, transactions, boxscores, bets, proposals, misc, tips, perry, poeltl, strikes, draft, invest, news, discord, picks_preview
from routers.picks_scheduler import start_picks_horizon_scheduler

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

logger = logging.getLogger("nbn-api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_picks_horizon_scheduler()
    yield


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://nbn.today", "https://news.nbn.today"],
    allow_methods=["GET", "PUT", "POST", "DELETE", "PATCH"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(auth.router)
app.include_router(players.router)
app.include_router(roster_picks.router)
app.include_router(picks_preview.router)   # additive: GET /api/picks-preview (conveyance model)
app.include_router(transactions.router)
app.include_router(boxscores.router)
app.include_router(bets.router)
app.include_router(proposals.router)
app.include_router(misc.router)
app.include_router(tips.router)
app.include_router(perry.router)
app.include_router(poeltl.router)
app.include_router(strikes.router)
app.include_router(draft.router)
app.include_router(invest.router)
app.include_router(news.router)
app.include_router(discord.router)
