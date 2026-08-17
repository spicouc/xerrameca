# Standalone deployment

Xerrameca is deployed independently from Pluribus. The two services must not share a virtualenv, working tree, SQLite file, environment file, or systemd unit.

## Target layout

```text
Pluribus
  service: pluribus.service
  port:    8790
  data:    /opt/pluribus/data/

Xerrameca
  service: xerrameca.service
  port:    8791
  code:    /opt/xerrameca/
  venv:    /opt/xerrameca/venv/
  env:     /etc/xerrameca/xerrameca.env
  data:    /var/lib/xerrameca/xerrameca.db
```

## Required runtime settings

```dotenv
XERRAMECA_HOST=0.0.0.0
XERRAMECA_PORT=8791
XERRAMECA_DB_PATH=/var/lib/xerrameca/xerrameca.db
XERRAMECA_IDENTITY_PROVIDER=pluribus
PLURIBUS_BASE_URL=http://127.0.0.1:8790
PLURIBUS_TIMEOUT_SECONDS=10
XERRAMECA_SUMMARY_DISPATCH_SECONDS=30
XERRAMECA_SUMMARY_MAX_ATTEMPTS=10
```

`PLURIBUS_SERVICE_API_KEY` is optional. Configure it only when deliberate final-summary persistence to Brain is desired. It must belong to a dedicated integration agent with the minimum required write scope. Never commit it.

## Installation

1. Create the service account and persistent directories.
2. Install this repository into its own virtualenv.
3. Store runtime configuration in `/etc/xerrameca/xerrameca.env` with root-readable/service-readable permissions only.
4. Install `systemd/xerrameca.service`.
5. Start Xerrameca without restarting Pluribus.

Example bootstrap commands for an administrator:

```bash
useradd --system --home /var/lib/xerrameca --shell /usr/sbin/nologin xerrameca || true
install -d -o xerrameca -g xerrameca -m 0750 /var/lib/xerrameca
install -d -o root -g xerrameca -m 0750 /etc/xerrameca

python3 -m venv /opt/xerrameca/venv
/opt/xerrameca/venv/bin/pip install --upgrade pip
/opt/xerrameca/venv/bin/pip install /opt/xerrameca

install -o root -g root -m 0644 systemd/xerrameca.service /etc/systemd/system/xerrameca.service
systemctl daemon-reload
systemctl enable --now xerrameca.service
```

## Verification order

Do not use Xerrameca availability as a Pluribus health signal.

1. `GET :8790/health` — Pluribus must already be green.
2. `GET :8791/health` — standalone Xerrameca local health.
3. `POST :8791/mcp/` with `tools/list` — exactly seven Xerrameca tools.
4. `POST :8791/v1/xerrameca/command` with a real agent `X-API-Key` — `help` and `agents`.
5. Run `scripts/smoke_x3.py` with two real agent credentials.
6. Restart only `xerrameca.service`; Pluribus must stay green throughout.

## Backups

Back up `/var/lib/xerrameca/xerrameca.db` independently. A Xerrameca rollback must never restore or mutate `pluribus.db`.

Before upgrades:

```bash
systemctl stop xerrameca.service
sqlite3 /var/lib/xerrameca/xerrameca.db 'PRAGMA wal_checkpoint(TRUNCATE); PRAGMA quick_check;'
cp --preserve=all /var/lib/xerrameca/xerrameca.db /var/lib/xerrameca/xerrameca.db.pre-upgrade
systemctl start xerrameca.service
```

Use a timestamped/managed backup policy in production rather than overwriting a single backup.

## Rollback invariant

A failed Xerrameca deployment is rolled back using only:

- Xerrameca code/version;
- Xerrameca virtualenv/container image;
- Xerrameca database backup if its own schema was changed.

It must never require a Pluribus database rollback.
