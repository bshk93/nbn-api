# Contributing to nbn-api

Thanks for helping. This is the API behind [nbn.today](https://nbn.today).

## How a change gets in

1. Branch off `main`.
2. Make your change, with tests.
3. Before asking for review, rebase on the latest `main`.
4. Push your branch and open a pull request into `main`.

`main` is what runs live, so changes only reach it through a reviewed
pull request. You don't need to deploy anything. Once it's approved and
merged, the owner ships it.

If your change also needs a change to the site
([nbn-today](https://github.com/bshk93/nbn-today)), open a pull request
there too and link the two, so they can ship together.

## Tests

Every module in `tests/` runs on its own:

```bash
venv/bin/python -m tests.test_something
```

Add a test for what you change. If it's a new module, add it to `MODULES` in
`tests/run_all.py`. Run the tests for the code you touched. The whole suite
takes a long time and you don't need to run it.

**A test must never touch the real league data.** Your pull request is tested
on the server, where `routers/constants.py` points `DATA_DIR` at the live data.
Setting an environment variable does not change that. So a test that reaches a
real loader or writer changes real rosters, balances or Discord. This has
already happened more than once. In a test, always:

- point every `*_FILE` your code writes at a temp directory, or stub the module's
  `load_*` / `save_*` functions (see `tests/test_game_starters.py`);
- stub anything that pays NB¥ (`routers/wallet.py`) or posts to Discord;
- watch for side effects in *other* modules your code calls. If your new code
  writes something when module A runs, module A's existing tests now write it
  too. Stub it there as well.

## Secrets

Never commit tokens, webhook URLs, API keys or `.env`. This repo is public. A
secret that gets pushed has to be revoked, even if it's deleted straight away.
Read credentials from the environment (`os.environ`), the way the existing
routers do.

## Finding your way around

- `CLAUDE.md` describes how the API is put together. If you use Claude Code,
  it reads this file automatically.
- `routers/` holds one file per feature. A new router is registered in
  `main.py`.
- The league's rules are in the site's rulebook
  ([nbn.today/rulebook](https://nbn.today/rulebook)). Validation code cites
  the section it enforces.
