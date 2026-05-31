import logging
import sys

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import auth, players, roster_picks, transactions, boxscores, bets, proposals, misc, tips, perry, strikes

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

app = FastAPI()

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
