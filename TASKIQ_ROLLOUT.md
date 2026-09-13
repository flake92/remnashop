# Taskiq queue-scoping rollout

The current release writes new tasks to a deployment-scoped Redis stream and
continues reading the legacy `taskiq` stream. The compatibility reader prevents
ordinary unread or pending work from being stranded, but a legacy message does
not contain a deployment identity. It therefore cannot be routed safely between
multiple old stacks automatically.

Use a short maintenance window for the first upgrade:

1. Stop every old scheduler and other task producer, including the web/bot
   process. Leave exactly one old worker running.
2. Inspect `XINFO GROUPS taskiq` in Valkey until the `taskiq` group's `pending`
   and `lag` values are both zero. `XLEN` is not a drain signal because old
   acknowledged entries may still exist in the stream.
3. Stop the old worker cleanly and confirm no old stack remains connected.
4. Set one stable, non-placeholder `TASKIQ_DEPLOYMENT_ID` for this stack, then
   start the migration, worker, scheduler and web/bot processes from the same
   immutable image digest.
5. Confirm the worker health output reports a fresh deployment heartbeat and
   `legacy_backlog=0` before reopening traffic.

Do not run old and new stacks concurrently while the legacy group has pending
or unread work. Task execution is at-least-once: handlers must remain
idempotent, and an interrupted task may be replayed. The broker renews and
acknowledges entries only while Redis still records the current consumer as the
owner, so a stale worker cannot delete a message reclaimed by another worker.

Keep the legacy stream until a separately reviewed cleanup after every deployed
environment reports `pending=0` and `lag=0`. The application does not delete the
whole legacy stream automatically.
