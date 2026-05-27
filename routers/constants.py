import logging
import sys
import threading
from pathlib import Path

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("nbn-api")

DATA_DIR  = Path("/var/lib/nothing-but-stats")
RULES_DIR = DATA_DIR / "rules"
PENDING_BOXSCORES_DIR  = DATA_DIR / "pending-boxscores"
MANUAL_QUEUE_FILE      = DATA_DIR / "pending-manual-queue.json"
BUILD_STATUS_FILE  = DATA_DIR / "build-status.json"
BUILD_SCRIPT       = Path("/home/skim/projects/nothing-but-stats/refresh/nbs.sh")
TOKENS_FILE        = DATA_DIR / "tokens.json"
MEMBERS_FILE       = DATA_DIR / "members.json"
TRADING_BLOCK_FILE = DATA_DIR / "trading-block.json"
PLAYER_BIOS_FILE   = DATA_DIR / "player-bios.json"
OVR_FILE           = DATA_DIR / "ovr-history.json"
CAP_LEVELS_FILE    = DATA_DIR / "cap-levels.json"
ROOKIE_SCALE_FILE  = DATA_DIR / "rookie-scale.json"
PICKS_FILE         = DATA_DIR / "draft-picks.csv"
TRANSACTIONS_FILE  = DATA_DIR / "transactions.json"
TEAM_STATE_FILE    = DATA_DIR / "team-state.json"
AWARDS_CONFIG_FILE    = DATA_DIR / "awards-config.json"
CALENDAR_EVENTS_FILE  = DATA_DIR / "calendar-events.json"
CALENDAR_GAMES_FILE   = DATA_DIR / "calendar-games.json"
BETS_FILE     = DATA_DIR / "bets.json"
BALANCES_FILE = DATA_DIR / "member-balances.json"
LEDGER_FILE   = DATA_DIR / "bets-ledger.json"
PROPOSALS_FILE = DATA_DIR / "proposals.json"
CONSTITUTION_FILE = DATA_DIR / "constitution.json"
TRIVIA_SCORES_PATH = DATA_DIR / "trivia-scores.json"

PICKS_HEADERS = ["YEAR", "ROUND", "ORIG", "OWNER", "PICK", "PLAYER", "PROTECTED", "SWAP_OWNER", "NOTES"]

_rules_lock         = threading.Lock()
_picks_lock         = threading.Lock()
_txn_lock           = threading.Lock()
_manual_queue_lock  = threading.Lock()
_ovr_lock      = threading.Lock()
_state_lock    = threading.Lock()
_deadcap_lock  = threading.Lock()

VALID_TEAMS = {
    "ATL", "BKN", "BOS", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW",
    "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN", "NOP", "NYK",
    "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR", "UTA", "WAS",
}

VALID_ROLES = {"admin", "rosters", "bod", "curator", "stats", "bookie"} | {t.lower() for t in VALID_TEAMS}

# Roles that are implicitly granted by holding another role
ROLE_IMPLIES: dict[str, set[str]] = {
    "bod": {"rosters"},
}

CURATOR_FIELDS = {
    "name", "pos", "dob", "college", "country",
    "draft_year", "draft_round", "draft_pick",
    "photo_url", "height", "weight", "wingspan", "jersey_number", "retired",
}

ROSTER_MAX = 15

# § 4.2 tier boundary constants
SALARY_MATCH_TIER1_CAP = 8_527_000
SALARY_MATCH_TIER2_CAP = 29_000_000
