# Referral reward manual recovery

## Rollout and rollback safety

Deploy migration 0052 without mixed application versions:

1. Drain traffic and stop all old web processes and reward workers; verify none
   remain connected or running.
2. Apply migration 0052.
3. Deploy and start only the new web and worker version.
4. After the new deployment is healthy, set
   `REFERRAL_REWARD_BACKFILL_ENABLED=true` and restart/redeploy only that new
   version before invoking preview or apply.

Do not run old and new Remnashop processes against the migrated database at the
same time. Once durable reward traffic or historical backfill begins, migration
0052 is forward-only. Its downgrade refuses to remove the schema when any
`referral_reward_resolutions` row, any `referral_reward_backfill_audits` row, or
any `referral_rewards.source_transaction_id` value exists. A downgrade is only
permitted before those durable records exist; legacy-only reward rows with a
null `source_transaction_id` do not trip the guard. Never delete provenance or
audit evidence to bypass this protection. Recover forward or restore the entire
database from a consistent pre-0052 backup instead.

Rewards in `MANUAL_REQUIRED` must never be replayed blindly. An `EXTRA_DAYS`
timeout or expired worker lease can mean that Remnawave applied the absolute
target even though Remnashop did not persist completion.

1. List cases with `GET /api/v1/admin/referral-rewards/manual` using the admin
   API key. The worker emits a one-shot critical log for newly visible cases.
2. Match `source_transaction_id`, recipient `user_id`, stored
   `target_subscription_id`, `baseline_expire_at`, and `target_expire_at` to the
   payment record, database state, Remnawave audit, and current panel state.
3. When the effect is proven applied, call
   `POST /api/v1/admin/referral-rewards/{id}/resolve` with
   `{"resolution":"CONFIRM_ISSUED","expected_version":3,`
   `"operator_reference":"alice/TICKET-123",`
   `"reason":"Verified target expiry in Remnawave audit"}`.
4. Use `CANCEL` only after proving the effect was not applied (or after an
   explicit audited rollback):
   `{"resolution":"CANCEL","expected_version":3,`
   `"operator_reference":"alice/TICKET-123",`
   `"reason":"Verified remote baseline after rollback"}`.

If a replacement subscription or later state drift makes those strict checks
inconclusive, an operator may add `"allow_drift":true` only after recording the
external audit evidence in `operator_reference` and `reason`. The decision stores
the observed current subscription id, remote UUID, and expiry as an immutable
evidence snapshot. The default remains fail-closed.

`expected_version` is mandatory and comes from `manual_incident_version` in the
admin listing. Each distinct ambiguity/refund increments this version and has its
own `manual_cause`; the same `(reward_id, incident_version)` decision is
idempotent. A stale version returns a conflict. A later refund after an earlier
ambiguity was confirmed opens a new incident, while a resolved refund incident
does not reopen on every sweep.

The resolver never adds points or days. It records an append-only audited
decision in `referral_reward_resolutions`; an identical request is idempotent,
while conflicting evidence is rejected. For `EXTRA_DAYS`, confirmation requires
the current panel expiry to be at least the durable target and synchronizes the
observed expiry locally, so a legitimate later renewal remains valid. Cancellation
requires the panel expiry to match the stored baseline exactly and otherwise
fails closed.

An `ON_FIRST_PAYMENT` row can be confirmed only if it already owns the durable
first-payment claim marker. A marker-less manual row must be canceled/reconciled
at its source and left to normal atomic winner selection; manually claiming the
marker risks a duplicate first-payment grant.

`ON_FIRST_PAYMENT` means the first-ever successfully fulfilled paid non-trial,
non-test transaction. A later refund does not reopen eligibility. Pending rewards
for a refunded source are safely superseded; a refund during processing or after
issuance enters manual review for explicit clawback handling.

## Historical reward backfill

Historical rewards are recovered through an explicit, audited
**inventory → preview → apply** workflow. Set `BASE_URL` to the Remnashop origin
and send the admin API key in `X-API-Key` for every request.

The flag defaults to false. Inventory remains read-only while disabled, but
preview and apply return `503 Service Unavailable`; never bypass that rollout
gate or enable it while any pre-0052 process is running.

1. Inventory eligible historical source transactions. This step is read-only and
   does not create reward intents:

   ```bash
   curl -sS \
     -H "X-API-Key: $API_KEY" \
     "$BASE_URL/api/v1/admin/referral-rewards/backfill/inventory?limit=100&offset=0"
   ```

2. Select and independently verify the exact `source_transaction_id` values,
   then persist an audited preview with the operator identity, external reference,
   and reason:

   ```bash
   curl -sS -X POST \
     -H "X-API-Key: $API_KEY" \
     -H "Content-Type: application/json" \
     "$BASE_URL/api/v1/admin/referral-rewards/backfill/preview" \
     -d '{
       "source_transaction_ids": [77, 91],
       "operator_identity": "alice",
       "operator_reference": "TICKET-135",
       "reason": "Verified missing durable intents against payment records"
     }'
   ```

   Save the returned `preview_id` and the complete `config_snapshot`. Review every
   transaction, generated intent, and error. Continue only when `can_apply` is
   `true` and the preview exactly matches the approved incident scope.
   `*_REFERRAL_POSTDATES_PAYMENT` means attribution did not exist when payment
   completed. `PARTIAL_EXISTING_INTENTS_MANUAL_REVIEW` means one reward level
   already has a different durable policy snapshot. Both are intentionally
   fail-closed and must not be forced through backfill.

3. Apply the same source IDs and operator evidence, copying the complete config
   object from the preview response into `expected_config_snapshot`:

   ```bash
   curl -sS -X POST \
     -H "X-API-Key: $API_KEY" \
     -H "Content-Type: application/json" \
     "$BASE_URL/api/v1/admin/referral-rewards/backfill/31/apply" \
     -d '{
       "source_transaction_ids": [77, 91],
       "operator_identity": "alice",
       "operator_reference": "TICKET-135",
       "reason": "Verified missing durable intents against payment records",
       "expected_config_snapshot": {
         "enabled": true,
         "max_level": 2,
         "accrual_strategy": "ON_FIRST_PAYMENT",
         "reward_type": "POINTS",
         "reward_strategy": "AMOUNT",
         "reward_config": {"1": 10, "2": 5}
       }
     }'
   ```

Apply locks and recomputes eligibility and attribution, then rejects any drift
from the persisted preview. An identical successfully applied request is an
idempotent audit replay; it does not create the reward intents again. If config,
source IDs, operator evidence, or eligibility changed, create a new preview.

**Never replay a legacy ambiguous reward.** If inventory or preview reports
`LEGACY_AMBIGUOUS_REWARD_REQUIRES_RESOLUTION`, the old process may already have
applied the points or days without recording completion. Do not include that
transaction in an apply request and do not create a reward manually. Reconcile
the external effect and database evidence through the manual-resolution process
above; historical backfill must remain fail-closed for this case.
