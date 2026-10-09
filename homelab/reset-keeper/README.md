# reset-keeper

Spends Claude reset grants for the proxy's account pool, by the rules in the
`reset_keeper.py` docstring. In short:

- Only a used-up **weekly** window triggers a reset; a 5-hour block never does.
- A reset zeroes usage but keeps the weekly end date, so it is worth only the use it
  buys before that date. The keeper compares "reset now" with "wait" in hours of
  nonstop use, using the learned time to burn a full week, and picks the larger.
- In the last hour before a grant expires, it spends whatever is left.
- After every confirmed reset it clears the proxy's own cooldown (`/v0/management/reset-quota`).

The dashboard (papuman/Cli-Proxy-API-Management-Center, quota page) shows its decisions
and has an **Auto reset / Use it manually** switch. Endpoint: port 8318, `GET /status`,
`PUT /mode {"auto": bool}`, management key as Bearer.

Deploy (docker-prd-01, `/opt/cli-proxy-api`): copy `reset_keeper.py` to `reset-keeper/`,
put `CPA_MGMT_KEY=...` in `reset-keeper/.env` (mode 600), and add the `reset-keeper`
service from HomeLab `cli-proxy-api/docker-compose.yml` (python:3.12-alpine, state in
`/srv/cli-proxy-api/reset-keeper`). Tests: `python3 -m unittest test_reset_keeper`.
