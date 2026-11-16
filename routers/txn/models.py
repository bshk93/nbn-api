"""The request and result models for transactions (Pydantic). Pure data: no loaders.

Moved out of routers/transactions.py; see routers/txn/__init__.py."""
from pydantic import BaseModel
from typing import Literal, NamedTuple, Optional


class CheckResult(BaseModel):
    check: str
    passed: bool
    level: str = "error"   # "error" blocks; "warning" allows force-through
    message: str


class ContractIn(BaseModel):
    type: str = "player"  # "player" or "two-way"
    salaries: dict[str, str] = {}
    # § 3.12: years of NBA experience **as of this contract's first salary
    # year**, used to place each year on the Minimum Salary Scale. Declared on
    # the contract because the scale is contract math and the bio can't supply
    # it — `draft_year` is the NBN draft for identity purposes and is null for
    # every player who entered the league before it (and forever, for UDFAs),
    # which silently priced multi-year minimums flat at the rookie tier.
    # Optional: absent, years of service (`years_of_service`) apply instead.
    years_experience: Optional[int] = None
    cap_holds: dict[str, str] = {}
    guaranteed: dict[str, str] = {}
    guarantee_dates: dict[str, str] = {}
    guarantee_schedule: dict[str, list[dict]] = {}


class SignDetails(BaseModel):
    player: str
    team: str
    contract: ContractIn
    signing_method: Optional[str] = None
    bird_rights_type: Optional[str] = None
    # § 3.10: only consulted when the contract's cap_holds imply a trailing
    # UFA/RFA hold whose season needs a Full Bird (QVFA) EAPS comparison and
    # that season has no real EAPS on file yet — see _compute_fa_hold_amount.
    # "above" or "below" (the previous salary, relative to EAPS). Ignored
    # otherwise.
    eaps_assumption: Optional[str] = None
    # Players this team releases (§ 5.1) only if the signing goes through,
    # to make roster room for it. Counted in the roster check; released right
    # after the signing is written, never before. See `_signing_release_checks`.
    releases: list[str] = []


class ExtensionDetails(BaseModel):
    """§ 6.2 / § 6.3. `contract` is the EXTENDED TERM only — never the live
    years still on the books; those are carried forward untouched and the
    extension's own years are laid on top starting the season after the
    existing deal's final guaranteed year (§ 6.2 rule 10). See
    nbn-today/docs/poext-extension-pipeline.md for the full design."""
    player: str
    team: str
    contract: ContractIn
    kind: str = "veteran"  # "rookie_scale" | "veteran" | "extend_and_trade"
    bird_rights_type: Optional[str] = None
    eaps_assumption: Optional[str] = None
    # § 4.5 six-month trade-freeze clock; defaults to the transaction date.
    announced_date: Optional[str] = None
    # The day a PO-EXT proposal was first submitted. § 6.3's windows are
    # submission deadlines, so a proposal the committee decides after its
    # window closed is still judged as of this day — the window, and the
    # league year that says whether the player is an expiring veteran.
    submitted_date: Optional[str] = None
    # Season string. Required by the UI (not the schema) only when the player
    # has no ledger acquisition record — see _extension_frame / D15
    # (nbn-today/docs/poext-extension-pipeline.md § 2.3a). Never trusted as a
    # definite basis: always warn-severity unless it contradicts other partial
    # ledger history the player does have, which promotes to error. Not
    # persisted anywhere — re-asked on every proposal, deliberately.
    attested_contract_start: Optional[str] = None


class PickIn(BaseModel):
    year: int
    round: int
    orig: str
    pick_number: Optional[int] = None


class PickDetails(BaseModel):
    player: str
    team: str
    pick: PickIn
    # A draft pick conveys only the player's draft rights — no contract. The
    # contract is attached later via a separate `sign` step once the cap is
    # finalized. Kept optional for backward compatibility with old payloads.
    contract: Optional[ContractIn] = None


class TransactionIn(BaseModel):
    type: str
    date: str
    description: str = ""
    details: dict
    force: bool = False
    # Backfill a trade that predates this transaction log (or whose effects
    # current roster/cap data already reflects) without re-applying it —
    # skips _apply_trade and validation, just logs the record for display.
    historical: bool = False
    # Whether `roster_log_relay` should mirror this transaction's Discord post
    # into #roster-log. Default False: most transactions are entered into
    # #roster-log by hand as part of working the transaction, so mirroring by
    # default would double it up — this opts in only the ones that aren't.
    relay_to_roster_log: bool = False


class OptionDetails(BaseModel):
    player: str
    decision: str       # "accept" or "decline"
    option_type: str    # "PLAYER_OPT" or "TEAM_OPT"
    year: str           # e.g. "26-27"
    # Decline only. Left out, § 3.1 decides it from his experience
    # (`_option_fa_tag`); given, it must agree with § 3.1 where that's known.
    cap_hold_type: Optional[str] = None
    # § 3.10: auto-computed from bird_tier + the last real salary when omitted
    # — see _compute_fa_hold_amount. Pass explicitly only to override.
    cap_hold_amount: Optional[str] = None
    bird_tier: Optional[str] = None  # QVFA, EQVFA, Non-QVFA — only used on decline
    eaps_assumption: Optional[str] = None  # "above" or "below" — see SignDetails
    # Only used on the historical=true backfill path (see _create_historical_option)
    # — the live path (_apply_option) always derives team itself from the current
    # roster map and ignores this field.
    team: Optional[str] = None


class GuaranteeDetails(BaseModel):
    player: str
    year: str           # e.g. "26-27"


class ReleaseDetails(BaseModel):
    player: str
    # Stretch provision (rulebook §5.1 method 2): spread the total remaining
    # dead-cap obligation evenly across this many seasons starting at the
    # release season, instead of the default original-payment-schedule method.
    # The rulebook formula is (2 * remaining contract years) + 1, but this is
    # taken as an explicit input rather than auto-derived, since "remaining
    # years" is ambiguous across NON_GTD/option contracts — the submitter
    # states the stretch length the team actually elected.
    stretch_years: Optional[int] = None


class RenounceDetails(BaseModel):
    player: str


class RescindRenounceDetails(BaseModel):
    txn_id: str   # the renounce being undone; its stored snapshot is the restore source


class QualifyingOfferDetails(BaseModel):
    """§ 3.1: the team extends or withdraws a qualifying offer. `season` is the
    hold season and is derived when omitted. `amount` overrides the formula —
    office only, for a player the formula can't price."""
    player: str
    action: Literal["extend", "withdraw"] = "extend"
    season: Optional[str] = None
    amount: Optional[int] = None


class AcceptQualifyingOfferDetails(BaseModel):
    """§ 3.1: the player signs his qualifying offer — a one-year deal at the QO
    amount with his own team. Decided by the PDC ballot, not the team."""
    player: str
    # "above" / "below" — see SignDetails. Needed only when the deal's trailing
    # UFA hold is Full Bird and next season has no EAPS on file yet (§ 3.10).
    eaps_assumption: Optional[str] = None


class OfferSheetDetails(BaseModel):
    """An offer sheet being *extended* (§ 3.15). Deliberately carries no
    `outcome`: extending the offer and the incumbent's decision to match are two
    separate acts by two different teams, and collapsing them into one record
    attributed the incumbent's decision to whoever typed the transaction.
    The outcome now lives on `offer_sheet_decision`.

    Legacy ledger entries from the combined era still carry `outcome` in their
    stored details. Those are read as raw dicts, never through this model, and
    `_open_offer_sheets` treats the presence of `outcome` as "already resolved".
    """
    player: str
    offering_team: str
    contract: ContractIn
    # What actually funds the contract for whichever team ends up signing the
    # player (§ 3.1-3.6 methods; same vocabulary as SignDetails.signing_method).
    # Threaded straight into the internal _apply_sign call so NTMLE/TMLE/BAE
    # usage on an offer sheet gets the same mle_used/hard-cap bookkeeping and
    # § 1.5/§ 1.6 funding-availability validation a plain `sign` gets — before
    # this field existed, an offer sheet had no way to record its funding
    # mechanism at all, so e.g. a Second-Apron team using the NTMLE to sign
    # one went untracked and unvalidated.
    signing_method: Optional[str] = None
    bird_rights_type: Optional[str] = None
    eaps_assumption: Optional[str] = None  # "above" or "below" — see SignDetails
    # The offering team's releases if the player joins it (see SignDetails).
    # Stored on the offer and carried out by an `offer_sheet_decision` of
    # `not_matched`; a match releases nobody.
    releases: list[str] = []


class OfferSheetDecisionDetails(BaseModel):
    offer_id: str    # the `offer_sheet` transaction being resolved
    outcome: str     # "matched" (incumbent keeps) or "not_matched" (player leaves)


class VoidPlayerDetails(BaseModel):
    player: str
    reason: str = ""
    # § 7.4: a pick from the most recent draft who signed and then went
    # overseas before opening night. The contract is voided, but the team keeps
    # him as stashed draft rights instead of losing him. `reason` names the
    # club and contract, as a § 7.4 stash's note does.
    keep_rights: bool = False


class StashDetails(BaseModel):
    # Keeps an unsigned pick's rights past the § 7.1 signing deadline. "7.1":
    # not in the current 2K and sitting out organized basketball; "7.4": under
    # an active overseas contract. `note` is the evidence a reviewer reads.
    player: str
    basis: Literal["7.1", "7.4"]
    note: str = ""


class SetHardCapDetails(BaseModel):
    team: str
    level: str          # "first_apron" | "second_apron" | "default"
    reason: str = ""


class ConvertTwoWayDetails(BaseModel):
    player: str
    contract: ContractIn
    signing_method: Optional[str] = None
    bird_rights_type: Optional[str] = None
    eaps_assumption: Optional[str] = None  # "above" or "below" — see SignDetails


class SignPickDetails(BaseModel):
    # Signs a player whose draft rights a team holds to their first contract.
    player: str
    contract: ContractIn
    bird_rights_type: Optional[str] = None
    eaps_assumption: Optional[str] = None  # "above" or "below" — see SignDetails


class TradeAsset(BaseModel):
    type: str                        # "player", "pick" or "swap"
    slug: Optional[str] = None
    year: Optional[int] = None
    round: Optional[int] = None
    orig: Optional[str] = None
    protection: Optional[int] = None
    swap_with: Optional[str] = None
    # CUTOVER STEP 9 (docs/picks-migration-worksheet.md): disambiguates which
    # of several contingent claims from_team is conveying, when it holds more
    # than one on this pick. Get valid values from GET /api/picks/{team} (a
    # contingent pick's response includes them) or the 422 error this asset
    # would otherwise get, which lists them. Only needed in that ambiguous
    # case — omit for every ordinary trade.
    leaf_id: Optional[str] = None
    # Protection ladder: a compensation trigger layered on top of this pick's
    # own conveyance (which `protection`/`swap_with`/plain re-trade already
    # decide) — not a replacement for it. If the pick doesn't convey to
    # to_team because it lands within ladder_protect_top, to_team is instead
    # owed ladder_fallback (a list of {year, round, orig} pick refs) as
    # compensation. Only set these together; only needed for a "protected,
    # else substitute pick(s)" clause explicitly stated in the trade.
    ladder_protect_top: Optional[int] = None
    ladder_fallback: Optional[list[dict]] = None
    # A `swap` asset moves no pick. from_team gives to_team the right to swap
    # picks: from_team's `orig` pick against to_team's `swap_orig` pick, same
    # year and round. to_team ends with the better of the two, from_team the
    # worse. Each team must hold its pick outright (_pick_swap_problems).
    swap_orig: Optional[str] = None
    # Server-set, never trusted from a client (`_describe_pick_claims`
    # overwrites it): what a pick asset conveys when it is one band of a
    # protected pick, e.g. "protection removed". Read by every display of
    # the trade, so none of them has to work it out from a leaf_id.
    claim: Optional[str] = None


class TradeTransfer(BaseModel):
    from_team: str
    to_team: str
    assets: list[TradeAsset]


class TradeIn(BaseModel):
    transfers: list[TradeTransfer]
    legality: str = "tbd"
    # Team abbr -> exception type ("ntmle" | "tmle" | "room_exception" | "bae")
    # used to absorb that team's incoming salary in lieu of matching outgoing
    # salary. Omit or null for teams matching normally. TPE absorption is a
    # separate field (tpe_usage, below) since it needs to name a specific
    # banked exception, not just a type.
    exceptions: dict[str, Optional[str]] = {}
    # Team abbr -> the specific incoming player slugs that team's exception is
    # absorbing (§ 4.2a: "a team uses exactly one of the four for a given
    # incoming player"). The named players are funded by the exception; every
    # other incoming player still has to satisfy ordinary salary matching
    # against outgoing salary. This is what makes a hybrid trade expressible —
    # e.g. two players matched against an outgoing contract while a third is
    # absorbed into the MLE.
    #
    # Omit (or leave a team out) to keep the original all-or-nothing behavior,
    # where the exception is tested against that team's entire incoming total.
    # Every slug must actually be incoming to that team in this trade.
    exception_players: dict[str, list[str]] = {}
    # Team abbr -> Trade Exception id (see GET /api/trade-exceptions/{team})
    # used to absorb that team's incoming salary (§ 4.1a). Mutually exclusive
    # with `exceptions[team]` for the same team, and with any outgoing salary
    # from that team in this trade — both are rejected by the validator.
    tpe_usage: dict[str, str] = {}
    # Sign-and-trade: whoever submits the trade must know and declare this —
    # nothing in the data lets us infer it (a team signing then trading a
    # player days later can be a coincidence, not a sign-and-trade). When set,
    # every team acquiring a player in this trade is hard-capped at First Apron
    # (rulebook §1.4 row C). sign_and_trade_txn_id optionally links to the
    # paired `sign` transaction's id for traceability from the log.
    is_sign_and_trade: bool = False
    sign_and_trade_txn_id: Optional[str] = None
    # Player slugs whose acquisition is the sign-and-trade portion of this
    # trade. When set, only the team(s) receiving these specific players are
    # hard-capped — needed when a multi-team trade bundles an S&T together
    # with unrelated player-for-player legs, so the cap doesn't spill onto
    # teams that aren't party to the S&T. Omit to fall back to "every team
    # acquiring any player is capped", which is correct for a plain two-team S&T.
    sign_and_trade_players: list[str] = []
    # The proposed new contract(s) being signed as part of the S&T, keyed by
    # player slug. Reuses SignDetails verbatim (player/team/contract/
    # signing_method/bird_rights_type) so the § 3.14 contract-requirement
    # checks (length, raise cap, MLE exclusion, Bird-rights eligibility) can
    # run here even though the real contract is applied via a separate `sign`
    # transaction in practice (see .claude/commands/enter-transaction.md) —
    # this only informs validation, it never itself writes a contract.
    sign_and_trade_signings: list[SignDetails] = []
    # Team abbr -> players that team releases right after the trade (§ 5.1,
    # original payment schedule only). A slug must be on that team once the
    # trade is done — a player it keeps, or one it receives here. Each is
    # applied by `apply_trade` as its own ordinary release, after the trade.
    releases: dict[str, list[str]] = {}


class TradeValidateInput(BaseModel):
    transfers: list[TradeTransfer]
    is_sign_and_trade: bool = False
    sign_and_trade_players: list[str] = []
    sign_and_trade_signings: list[SignDetails] = []
    exceptions: dict[str, Optional[str]] = {}
    exception_players: dict[str, list[str]] = {}
    tpe_usage: dict[str, str] = {}
    releases: dict[str, list[str]] = {}


class TransactionValidationResult(BaseModel):
    """Shape returned by every `/api/validate/*` endpoint: the verdict, the
    full check list (passes included), and a type-specific financial snapshot."""
    legal: bool
    checks: list[CheckResult]
    fact_sheet: dict


class SigningBooks(NamedTuple):
    """A team's books for one signing, before and after. See `_signing_books`."""
    current: int             # Team Salary, holds included (§ 3.10)
    current_ex_holds: int    # Team Salary without UFA/RFA holds
    existing_hold: int       # the signee's own figure already on the books
    is_fa_hold: bool         # ...and whether it's a UFA/RFA hold (so not in ex-holds)
    new_sal: int             # the contract's figure for this season
    other_holds: int         # UFA/RFA holds left for *other* players
    standard_after: int      # standard roster count after the signing
    erc_after: int           # § 2.1a charge on the slots still empty after it
    funding_base: int        # what the cap-room test starts from (holds in)
    funding_base_ex_holds: int  # ...and the apron/exception tests (holds out)
    projected_ex_holds: int  # ex-holds Team Salary after the signing


class TransactionPatch(BaseModel):
    description: str


class SelfQualifyingOfferIn(BaseModel):
    player: str
    action: Literal["extend", "withdraw"]
    description: str = ""


class SelfRenounceIn(BaseModel):
    player: str
    description: str = ""


class SelfOptionIn(BaseModel):
    player: str
    decision: str            # "accept" or "decline"
    cap_hold_type: Optional[str] = None  # decline only; left out, § 3.1 decides it
    eaps_assumption: Optional[str] = None  # decline only, when the hold needs it (§ 3.10)
    description: str = ""


class SelfReleaseIn(BaseModel):
    player: str
    stretch_years: Optional[int] = None
    description: str = ""


class SelfConvertTwoWayIn(BaseModel):
    player: str
    description: str = ""
    year2: str = "guaranteed"   # a SELF_CONVERT_YEAR2_TERMS key


class SelfSignPickIn(BaseModel):
    player: str
    description: str = ""


class SelfStashIn(BaseModel):
    player: str
    basis: Literal["7.1", "7.4"]
    note: str = ""
    description: str = ""
