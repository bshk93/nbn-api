"""Pieces of routers/transactions.py that live in their own files.

transactions.py was 11,500 lines. What moved here is what could move without
changing behaviour anywhere: the Pydantic models, and the pick, contract and cap
rules that are pure functions of their arguments (and of each other). None of
them reads a file through a loader a test replaces.

That last condition is the rule for adding to this package. Tests stub data by
assigning to the transactions module (`tx.load_player_bios = ...`,
`tx._build_team_map = ...`, about 40 names). A function moved here would keep
calling the real loader and read the live data dir. So a function stays in
transactions.py while it calls, directly or not, anything a test patches there;
the analysis that drew this line is in the commit that made the package
(2026-11-16).

transactions.py re-imports every name defined here, so
`routers.transactions.<name>` and every existing import keep working. Patch a
name where it is looked up: these modules look up each other's names, not
transactions.py's.

    models.py  request and result models (SignDetails, TradeIn, CheckResult, ...)
    picks.py   conveyance registration, pick ownership, swaps, protections, Stepien
    rules.py   scales, min/max salary, two-way, QO, FA holds, raises, exceptions,
               hard caps, extension eligibility
"""
