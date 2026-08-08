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
AVATARS_DIR = DATA_DIR / "avatars"
PENDING_BOXSCORES_DIR  = DATA_DIR / "pending-boxscores"
MANUAL_QUEUE_FILE      = DATA_DIR / "pending-manual-queue.json"
BUILD_STATUS_FILE  = DATA_DIR / "build-status.json"
BUILD_SCRIPT       = Path("/home/skim/projects/nbn-today/build/build.sh")
TOKENS_FILE        = DATA_DIR / "tokens.json"
MEMBERS_FILE       = DATA_DIR / "members.json"
TRADING_BLOCK_FILE = DATA_DIR / "trading-block.json"
PLAYER_BIOS_FILE   = DATA_DIR / "player-bios.json"
OVR_FILE           = DATA_DIR / "ovr-history.json"
ATTRIBUTES_FILE    = DATA_DIR / "player-attributes.json"
CAP_LEVELS_FILE    = DATA_DIR / "cap-levels.json"
ROOKIE_SCALE_FILE  = DATA_DIR / "rookie-scale.json"
PICKS_FILE         = DATA_DIR / "draft-picks.csv"
TRANSACTIONS_FILE  = DATA_DIR / "transactions.json"
TEAM_STATE_FILE    = DATA_DIR / "team-state.json"
ROOM_ZONE_BASELINE_FILE = DATA_DIR / "room-zone-baseline.json"
TRADE_EXCEPTIONS_FILE = DATA_DIR / "trade-exceptions.json"
LEAGUE_STATE_FILE  = DATA_DIR / "league-state.json"
AWARDS_CONFIG_FILE    = DATA_DIR / "awards-config.json"
AWARDS_HISTORY_FILE   = DATA_DIR / "awards-history.json"
CALENDAR_EVENTS_FILE  = DATA_DIR / "calendar-events.json"
CALENDAR_GAMES_FILE   = DATA_DIR / "calendar-games.json"
BETS_FILE     = DATA_DIR / "bets.json"
BALANCES_FILE = DATA_DIR / "member-balances.json"
LEDGER_FILE   = DATA_DIR / "bets-ledger.json"
BIO_REWARDS_FILE = DATA_DIR / "bio-rewards.json"
INVEST_HOLDINGS_FILE = DATA_DIR / "invest-holdings.json"
INVEST_TRADES_FILE   = DATA_DIR / "invest-trades.json"
INVEST_MARKET_FILE   = DATA_DIR / "invest-market.json"
PROPOSALS_FILE = DATA_DIR / "proposals.json"
NEWS_FILE      = DATA_DIR / "news.json"
TIPS_FILE      = DATA_DIR / "tips.json"
CONSTITUTION_FILE = DATA_DIR / "constitution.json"
TRIVIA_SCORES_PATH = DATA_DIR / "trivia-scores.json"
STRIKES_FILE       = DATA_DIR / "strikes.json"
DRAFT_LIVE_FILE    = DATA_DIR / "draft-live.json"
DRAFT_SNAPSHOT_FILE = DATA_DIR / "draft-snapshot.json"
# Isolated picks file the live-draft show writes to (reassigns + selections), so the
# broadcast never mutates the permanent draft-picks.csv the transactions pipeline owns.
DRAFT_LIVE_PICKS_FILE = DATA_DIR / "draft-live-picks.csv"
# Member draft-pick grades: { "<year>": { "<slug>": { "<member>": "A" } } }
DRAFT_GRADES_FILE = DATA_DIR / "draft-grades.json"
SUGGESTIONS_FILE = DATA_DIR / "suggestions.json"
JOIN_SUBMISSIONS_FILE = DATA_DIR / "join-submissions.json"
JOIN_BLACKLIST_FILE   = DATA_DIR / "join-blacklist.json"
MEMBER_SEEN_FILE      = DATA_DIR / "member-seen.json"

PICKS_HEADERS = ["YEAR", "ROUND", "ORIG", "OWNER", "PICK", "PLAYER", "PROTECTED", "SWAP_OWNER", "NOTES", "FROZEN", "FROZEN_REASON"]

_rules_lock         = threading.Lock()
_picks_lock         = threading.Lock()
_bio_rewards_lock   = threading.Lock()
_txn_lock           = threading.Lock()
_manual_queue_lock  = threading.Lock()
_ovr_lock      = threading.Lock()
_state_lock    = threading.Lock()
_deadcap_lock  = threading.Lock()
_trade_exc_lock = threading.Lock()
_invest_lock   = threading.Lock()
_market_lock   = threading.Lock()

VALID_TEAMS = {
    "ATL", "BKN", "BOS", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW",
    "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN", "NOP", "NYK",
    "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR", "UTA", "WAS",
}

VALID_ROLES = {
    "admin", "rosters", "bod", "curator", "stats", "bookie",
    # Player Development Committee — see nbn-today/docs/pdc-free-agency-spec.md.
    # `fac`/`fac_head` gate free-agency review on pdc.nbn.today; `poext`/
    # `poext_head` are the player-option/extension committee's equivalents,
    # reserved now so membership can be granted before that side is built.
    "fac", "fac_head", "poext", "poext_head",
} | {t.lower() for t in VALID_TEAMS}

# Roles that are implicitly granted by holding another role
ROLE_IMPLIES: dict[str, set[str]] = {
    "bod": {"rosters", "curator"},
    # A committee head is also a member of their own committee, and of no other.
    # `bod` deliberately does NOT imply `fac` — sub-committee membership is a
    # specific per-player assignment, and a board-wide implication would put
    # every board member on every ballot roster.
    "fac_head": {"fac"},
    "poext_head": {"poext"},
}

CURATOR_FIELDS = {
    "name", "pos", "dob", "college", "country",
    "draft_year", "draft_round", "draft_pick",
    "photo_url", "height", "weight", "wingspan", "jersey_number", "retired",
}

# Fields eligible for the bio-fill NB¥ reward (excludes name/pos set at creation,
# jersey_number set by team owners, and retired which is a status flag)
BIO_REWARD_FIELDS = frozenset({
    "dob", "college", "country",
    "draft_year", "draft_round", "draft_pick",
    "photo_url", "height", "weight", "wingspan",
})

ROSTER_MAX = 15            # regular-season standard-roster limit (Article II)
ROSTER_OFFSEASON_MAX = 20  # offseason ceiling; teams must trim to ROSTER_MAX before the season
ROSTER_MIN = 14            # standard-roster minimum, year-round (§ 2.1) — trade legality is judged against this full floor (§ 2.1a)
ROSTER_CHARGE_MIN = 12     # real, persisted Empty Roster Charge floor (§ 2.1a) — narrower than ROSTER_MIN; below 12 the charge counts as real guaranteed salary, not just a trade-legality mock

# § 4.2 tier boundary constants
SALARY_MATCH_TIER1_CAP = 8_527_000
SALARY_MATCH_TIER2_CAP = 29_000_000
