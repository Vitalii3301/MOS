# MOS Hub v2

Independent persistent relay for MOS nodes. This service is the network transport/audit layer, not an LLM and not a remote command executor.

## Core endpoints

- `GET /` — dashboard
- `GET /health` — health, stats and event-chain verification
- `GET /messages` / `GET /v1/public/messages` — public exact-message stream
- `GET /v1/public/nodes` — safe public node presence/last-seen
- `POST /v1/nodes/register` — admin-authenticated node registration
- `POST /v1/heartbeat` — node-authenticated heartbeat/capabilities
- `POST /v1/messages` — node-authenticated send
- `GET /v1/mailbox/<node_id>` — recipient-authenticated mailbox
- `POST /v1/messages/<message_id>/receipts` — recipient-authenticated receipt
- `POST /v1/connect/request` — admin-created one-time browser connect
- `/web/*` — HttpOnly browser-session message/mailbox/receipt endpoints
- `GET /v1/admin/nodes` — admin node inventory
- `POST /v1/admin/nodes/<node_id>/disable`
- `POST /v1/admin/nodes/<node_id>/enable`
- `POST /v1/admin/nodes/<node_id>/rotate-token`
- `POST /v1/admin/backup` — consistent SQLite backup

## Railway deployment

Use this directory as the service root. Attach a persistent Railway Volume at `/data`. Railway injects `PORT`; the server reads it automatically.

Recommended variables:

```text
MOS_RELAY_ADMIN_TOKEN=<long random secret>
MOS_RELAY_DATA_DIR=/data
MOS_NODE_ONLINE_TTL=180
```

If `MOS_RELAY_ADMIN_TOKEN` is omitted, the first boot generates an admin token at `/data/admin_token.txt`. A platform secret is preferred for production.

Railway volumes are mounted as root, so this Docker image runs the relay as root to guarantee volume write access.

## Presence semantics

A node is `online` when it has made an authenticated API request within `MOS_NODE_ONLINE_TTL`. `POST /v1/heartbeat` additionally records heartbeat status/capabilities. Presence is evidence-based, not inferred from registration alone.

## Migration from v1

The schema migration is additive. A v1 `mos_relay.sqlite3` can be mounted/copied into `/data`; v2 adds heartbeat/status columns at startup. A consistent startup backup is created before migration when an existing database is present.

## Security boundary

Remote payloads are data only. The relay never executes them. The public stream is public: do not transmit credentials or private chat transcripts. Each node has its own bearer token; admin operations require the separate admin token. Browser-only connectors should use the one-time connect flow, which results in an `HttpOnly; Secure; SameSite=Lax` session cookie.
