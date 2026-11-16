# nbn-api

The backend for [nbn.today](https://nbn.today), the NBN fantasy basketball GM
league: a FastAPI app serving the league's rosters, contracts, transactions,
picks, box scores, stats build, committees, NB¥ economy and Discord
integrations.

It runs as one systemd service (`nbn-api.service`, port 8001, behind nginx at
`nbn.today/api/`) on a small shared box, and keeps all state as JSON and CSV
files in `/var/lib/nothing-but-stats` (there is no database). The site is the
sibling repo, `nbn-today`.

## Start here

- **`CLAUDE.md`**: how the code is laid out, the rules for the production box,
  dev vs live checkouts, auth, and an index of every subsystem.
- **`CONTRIBUTING.md`**: how to make a change, and the rule that a test must
  never touch real league data.
- **`docs/`**: one file per area. `transactions.md` (every transaction type),
  `data-model.md` (every data file), and the subsystem docs CLAUDE.md indexes.

## Run the tests

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/python -m tests.run_all
```

Nothing starts a server and nothing reads the real data directory. Tests that
need it are listed in `tests/run_all.py` and skipped, by name, where it
doesn't exist. CI (`.github/workflows/ci.yml`) runs the same suite on every push.

## Deploy

Edit in a dev checkout, merge to `main`, then on the box:

```bash
cd ~/projects/nbn-api && ./deploy.sh
```

`deploy.sh` fast-forwards, refuses a dirty tree, restarts the service and
prints the rollback command. When a change spans both repos, the API ships
first.
