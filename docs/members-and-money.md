# Members, sessions and NB¥ sinks

Browser sessions behind the .nbn.today cookie, and the three things members spend NB¥ on. Moved out of CLAUDE.md on 2026-11-16, word for word; CLAUDE.md keeps a one-line pointer per section.

## Browser sessions (the `.nbn.today` cookie)

The token lives in `localStorage`, which is per-origin — so a member signed in on
`nbn.today` is a stranger on `pdc.nbn.today`. `POST /api/auth/session` (bearer-auth)
mints an **opaque** session id into `sessions.json` and returns it as
`nbn_session=…; Domain=.nbn.today; Secure; HttpOnly; SameSite=Lax; Max-Age=30d`,
which every subdomain then sends automatically. `token-badge.js` calls it on load.

The member's token never enters the cookie, which is what makes a session
*revocable*: `_drop_sessions_for` runs on `rotate-token`, `DELETE /api/tokens/{token}`,
and `DELETE /api/members/{name}`. Roles are read live from members.json on every
resolve rather than frozen onto the session, so a grant or revocation lands on the
next request. Expiry is evaluated on read (reaping as it goes) — no scheduler.

**The cookie is honoured on a narrow allowlist and nothing else** (`_cookie_accepted`):
`GET /api/auth/me` and `/api/fa/*`. Cookie auth is what makes CSRF possible at all,
so `PUT /api/roster/{team}`, `POST /api/transactions`, `POST /api/self/renounce` and
every other write keep requiring the header — their blast radius is zero. Widening
it is a one-line change; narrowing it after the fact would not be. `POST /api/auth/session`
is itself off the list, so a session cannot mint its successor and 30 days is a real
ceiling. The allowlist lives in `get_token_info`, which reads `request.url.path`, so
`require_role` / `require_any_role` / `require_admin` inherit it with no per-endpoint
opt-in. A *bad* `Authorization` header still 403s — only the missing-header branch
falls through to the cookie, so a stale token fails loudly instead of quietly
succeeding as whoever the cookie belongs to.

The server also sets a second, valueless `nbn_session_live` marker cookie that is
*not* HttpOnly. It carries no secret; it exists only so page JS can tell it already
has a session, since the real cookie is unreadable and every page load would
otherwise mint another row. `POST /api/auth/session/logout` deletes the row and
clears both; it is deliberately unauthenticated, since it only ever destroys the
caller's own cookie. `tests/test_auth_session.py` pins all of the above.

## NB¥ sinks (`routers/themes.py`, and the two in `auth.py`)

Three things members spend NB¥ on. All three take `_balances_lock`, refuse
with a **402 naming the price and the balance**, and append a ledger row —
the ledger is the only receipt any of them produces.

| Sink | Price | Where |
|---|---|---|
| Cosmetics (name colour, status text) | 500 | `_COSMETIC_COST` — `auth.py` |
| Avatar upload | 5,000 | `_AVATAR_COST` — `auth.py` |
| Theme unlock | 5,000 flat — **your own team's is free** | `THEME_PRICE` / `own_team_theme` — `themes.py` |

`themes.py` sells the site's non-default themes (Lavender Rose plus one per
team) into `members[name]["cosmetics"]["themes"]`, which rides back out on
`/api/members/me`. Three things it does that the older two do not, and each is
pinned by `tests/test_themes.py`:

- **The whole operation is under one lock**, because the "already owned" check
  and the charge have to be atomic — a double-clicked menu item must not pay
  twice for one theme. A repeat purchase returns `already_owned: true` and
  charges nothing.
- **A failed grant refunds**, with the reversal as its own ledger row. Charging
  for a theme the member does not end up owning is the one outcome they have no
  way back from.
- **`LIVE_TEAM_THEMES` is a gate, not a list of teams.** A team belongs there
  only once `css/theme.css` in nbn-today actually has its block; listing one
  early sells 5,000 NB¥ of nothing. `build/check_theme_catalog.sh` over
  there checks the two repos agree.
- **A member's own team's theme is free and is not a grant.** `own_team_theme`
  derives it from the member's open tenure every time it is asked, so it lapses
  when the tenure does and never enters `cosmetics.themes`; buying it is a 400.
  It reaches the picker as `free_themes` on `/api/members/me`, because
  `GET /api/themes` is public and cached per browser, not per member.

Entitlement is server-side, selection is not — the browser keeps the chosen
theme in `localStorage` so `nav.js` can paint before any fetch resolves. Full
rationale in nbn-today's `docs/themes.md`.
