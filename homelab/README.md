# HomeLab branch

The proxy that runs on docker-prd-01:8317 (`/opt/cli-proxy-api`).

- `homelab` = an upstream release tag + one commit: the quota-snapshot patch (from jcarcaboso/CLIProxyAPI@43b0f3d,
  reviewed 2026-10-05). It hands each account's quota numbers to scheduler plugins, which the quota-balancer
  plugin needs. Without it the plugin silently round-robins. Drop the commit once upstream PR #6377 merges
  (its field is a pointer without json tags, so the plugin will need a tweak).
- Plugin: papuman/cliproxy-quota-balancer @ 1d85102 (fork of jcarcaboso's, reviewed 2026-10-05).
- UI: papuman/Cli-Proxy-API-Management-Center, branch `homelab` (Ledger view), release asset `management.html`.

## Update to a new upstream release

```bash
git fetch upstream --tags
git rebase --onto vX.Y.Z v8.0.15 homelab     # replay our commits on the new tag
docker build -f homelab/Dockerfile --build-arg VERSION=vX.Y.Z-quota -t local/cli-proxy-api:vX.Y.Z-quota .
git push --force-with-lease origin homelab && git tag vX.Y.Z-quota && git push origin vX.Y.Z-quota
```

Then set the image in `/opt/cli-proxy-api/docker-compose.yml` and `sudo docker compose up -d`.
Rollback: the previous `local/cli-proxy-api:*-quota` image, or stock `eceasy/cli-proxy-api:<tag>` (no quota data).
