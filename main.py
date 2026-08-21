import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import auth, players, roster_picks, transactions, boxscores, bets, proposals, misc, tips, perry, poeltl, strikes, draft, invest, news, discord, trade_finder, picks_preview, suggestions, google_sheets, free_agency, roster_log_relay, waivers, inbox, cleanup, poext
from routers.picks_scheduler import start_picks_horizon_scheduler
from routers.roster_log_relay import start_roster_log_relay

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
    start_roster_log_relay()
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
app.include_router(trade_finder.router)
app.include_router(suggestions.router)
app.include_router(google_sheets.router)   # POST /api/trade-sheet — publish a workbook to Drive
app.include_router(free_agency.router)     # GET /api/fa/pool — PDC free-agency Phase 1 (nbn-today/docs/pdc-free-agency-spec.md)
app.include_router(roster_log_relay.router)  # mirrors the transaction channels into #roster-log
app.include_router(waivers.router)         # § 5.1 waiver wire (nbn-today/docs/waiver-wire-spec.md)
app.include_router(inbox.router)           # per-member notifications (GET /api/inbox)
app.include_router(cleanup.router)         # "Clean Up the Poo Poo" — nbn-today/docs/clean-up-the-poopoo-spec.md
app.include_router(poext.router)           # § 6.2/6.3 extension pipeline — nbn-today/docs/poext-extension-pipeline.md
