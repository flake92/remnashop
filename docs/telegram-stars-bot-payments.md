# Bot payment visibility (Telegram Stars policy)

Telegram requires digital goods sold inside bots to be paid with Telegram
Stars only. Bots that keep offering card or SBP payments inside Telegram can
be blocked without warning.

## How it works

Every payment gateway has two independent switches (`Панель → 🌐 Платежные системы`):

| Switch | Meaning |
|---|---|
| `🟢 Включено / 🔴 Выключено` | Global on/off for the gateway (bot **and** web cabinet). |
| `🤖 В боте: показан / скрыт` | Bot-only visibility. A hidden gateway keeps working in the web cabinet (Clean Pay). |

The bot lists only gateways with `is_active = true` and `web_only = false`.
When no gateway is left for the bot, the user is told to pay in the web
cabinet (the `🌐 Личный кабинет` menu button).

The web cabinet keeps using every active non-Stars gateway, so hiding YooKassa
from the bot does not affect Clean Pay checkout.

## Recommended setup

1. `TELEGRAM_STARS` — active, shown in the bot. Star prices are maintained per
   plan duration in the plan editor (`plan_prices` with currency `XTR`).
2. `YOOKASSA` — active, hidden in the bot (`web_only = true`). Cards and SBP
   stay available in the web cabinet.

`stars_require_paid_purchase` (extra settings) blocks Stars for users with no
prior non-Stars purchase. It is off by default; with YooKassa hidden in the
bot, keep it off, otherwise new users cannot pay in the bot at all.

## Implementation notes

- Migration `0064_gateway_web_only` adds `payment_gateways.web_only` (default
  `false`, additive, safe to retry).
- Bot dialogs call `PaymentGatewayDao.get_active_for_bot()`; web endpoints keep
  calling `get_active()`.
- `TELEGRAM_STARS` was already rejected by the web purchase validation
  (`_assert_web_gateway`), so Stars remains bot-only.
