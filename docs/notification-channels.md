# Уведомления в Telegram, e-mail и Web Push

Всё, что бот отправляет пользователю, доходит по всем его каналам:

- **служебные** — события пользователя (`UserEvent`: подписка скоро закончится,
  закончилась, трафик, льготный период, реферальные начисления и т. д.),
  «платежи снова доступны» и личные сообщения администратора;
- **новости** — ручные рассылки администратора, по той же аудитории (все,
  с подпиской, просроченные и т. д.).

Интерактивные ответы бота в диалогах в другие каналы не копируются.

## Как устроено

`NotificationService.copy_to_channels` рендерит текст тем же переводчиком, что и
Telegram, и ставит строки в очередь `notification_deliveries` в собственной
транзакции. Задача taskiq `deliver_notification_channels_task` каждую минуту:

- отменяет просроченные строки (служебные push — сутки, остальное — 3 дня);
- доставляет push всем подпискам пользователя (RFC 8291/8292, без внешних
  библиотек; только известные push-сервисы, ответ 404/410 удаляет подписку);
- доставляет письма через **отдельный SMTP рассылок** (`BULK_EMAIL_*`) с
  лимитом `BULK_EMAIL_RATE_PER_MINUTE` (по умолчанию 30).

Перед отправкой согласие проверяется заново: отписка всегда побеждает уже
поставленное в очередь письмо. Напоминание «подписка скоро закончится» по e-mail
отправляет отдельный конвейер `subscription_email_reminders` — дублей нет.

## Согласия

| Поле `users` | По умолчанию | Смысл |
| --- | --- | --- |
| `email_service_notifications_enabled` | `true` у всех | служебные письма, в том числе напоминания |
| `email_news_enabled` + `email_news_enabled_at` | `false` | новости — только с явного согласия (регистрация или профиль), время согласия хранится |

## Письма

Multipart (текст + HTML), `List-Unsubscribe` (HTTPS) и
`List-Unsubscribe-Post: List-Unsubscribe=One-Click` (RFC 8058), `List-Id`,
`Precedence: bulk` для новостей, `Auto-Submitted: auto-generated` для служебных,
`Message-ID` домена отправителя, видимая ссылка «Отписаться» и ссылка на
настройки. Ссылки строятся от origin `WEB_CABINET_URL`; токен отписки — HMAC от
`APP_CRYPT_KEY`, без срока действия. Для домена отправителя настройте SPF, DKIM и
DMARC.

## API (под ключом auth-сервиса Clean Pay)

- `GET /api/v1/public/auth/notifications/push/key`
- `GET`, `POST /api/v1/public/auth/notifications/push/subscription`,
  `POST …/push/subscription/delete`
- `PATCH /api/v1/public/auth/notification-preferences` — `service_email_enabled`,
  `news_email_enabled`
- `POST /api/v1/public/auth/notifications/unsubscribe` — по токену, без входа
- `POST /api/v1/public/auth/register` — `email_news_consent`

## Лента в кабинете

Каждая копия сообщения хранится в `notification_feed_items` (год) и показывается на странице кабинета `/notifications`. Push и письма ведут на неё.

## Удаление рассылки

Отменяет копии в очереди, убирает сообщение из ленты и снимает показанные push. Отправленное письмо отозвать нельзя: отчёт показывает их число.

## Наблюдаемость

- Отчёт рассылки в боте показывает по каналам: отправлено, в очереди, ошибки,
  отменено и сколько пользователей аудитории копию не получат.
- Проверка SMTP раз в 5 минут — отдельно для основного SMTP и SMTP рассылок,
  с указанием, какой из них не отвечает.

## Ключи VAPID

```bash
docker exec remnashop python -m src.infrastructure.services.web_push
```

Меню Clean Pay создаёт их само при обновлении Remnashop. Не меняйте пару без
необходимости: новые ключи отключают все подписанные устройства.
