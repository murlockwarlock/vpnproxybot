# NL Stack Disaster Recovery Bundle

This package is for cloning the current Netherlands production stack to another server:

- Marzban master, including `/var/lib/marzban`, SQLite DB, Xray config, custom `subscription.py` and `share.py`.
- Anewka Telegram bot, including `vpn_bot.db` and `.env`.
- Loonapie webstore, including `webstore_loonapie.db` and `.env.loonapie`.
- Nginx HTTP and SNI stream configs.
- Optional Let's Encrypt certificates.

It is intentionally a `docker compose` stack, not one fat container. It still starts with one command, but keeps Marzban, bot, webstore and nginx as separate processes. All services use host networking because the live NL configs depend on localhost ports and Xray/SNI routing.

## Create Backup On NL

Copy this repo or at least `ops/nl-stack/scripts/make_backup_bundle.sh` to the NL master and run:

```bash
sudo bash make_backup_bundle.sh --include-certs
```

Without `--include-certs`, the archive will not contain `/etc/letsencrypt`. In that case DNS must be pointed to the new server and certs must be reissued before public HTTPS works.

The script prints an archive path like:

```text
/root/nl-stack-backups/nl-stack_20260523_181500.tar.gz
```

Move that archive to the new server together with this repo directory.

## Restore On New Server

Prerequisites:

- Linux server with Docker and Docker Compose.
- Ports `80`, `443`, `8443`, `8444` open.
- No existing services bound to `7000`, `8081`, `8901`, `80`, `443`, `8443`, `8444`, or the Xray inbound ports from Marzban config.

Run from the repo on the new server:

```bash
cd ops/nl-stack
sudo bash scripts/restore_bundle.sh /root/nl-stack_YYYYmmdd_HHMMSS.tar.gz
```

Then check:

```bash
docker compose ps
docker compose logs --tail=100 marzban bot webstore nginx
curl -k -I https://127.0.0.1:8443
curl -k -I https://127.0.0.1:8444
```

After validation, point DNS for `loonapie.xyz` and `vpn.psysoldatov.ru` to the new server. If certs were not included, issue fresh certificates for both domains before switching production traffic.

## Files

- `docker-compose.yml`: starts Marzban, bot, webstore and nginx.
- `Dockerfile.app`: shared image for bot and webstore from the current repo code.
- `scripts/make_backup_bundle.sh`: run on NL to create a transferable archive.
- `scripts/restore_bundle.sh`: run on the new server to unpack and start the stack.
- `env/*.example`: documentation-only examples, not production secrets.
- `nginx/`: fallback nginx configs; restore overwrites them with live NL configs when present in the bundle.

## Important Limits

This clones the master stack state. It does not clone external provider behavior or DNS/IP reputation. If a client config hardcodes old IPs or DNS still points to NL, traffic will not move until DNS and client subscription updates are refreshed.

The bot/webstore databases are SQLite snapshots from the backup moment. If the old NL server remains live after backup, payments/subscriptions created there after the snapshot will not exist on the restored server unless a newer bundle is restored.
