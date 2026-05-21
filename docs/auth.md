# Authentication & Persistence — Backend

> Архитектурный документ. Целевая модель авторизации и хранения данных для JS Notebook (api). Соответствует требованиям TARDIS-15.
>
> Документ описывает **целевое** состояние. Текущий код (`/auth/login` с password) — temporary заглушка, подлежит замене на OTP-flow отдельным тикетом.

## Содержание

1. [Цели и контекст](#1-цели-и-контекст)
2. [Стратегия авторизации](#2-стратегия-авторизации)
3. [JWT: формат и параметры](#3-jwt-формат-и-параметры)
4. [Модели данных](#4-модели-данных)
5. [API-контракт](#5-api-контракт)
6. [Local / dev / test режим](#6-local--dev--test-режим)
7. [Notebook persistence](#7-notebook-persistence)
8. [Conflict resolution](#8-conflict-resolution)
9. [Версионирование](#9-версионирование)
10. [Биометрия (future)](#10-биометрия-future)
11. [Rate limiting и защита от abuse](#11-rate-limiting-и-защита-от-abuse)
12. [Переменные окружения](#12-переменные-окружения)
13. [Open questions](#13-open-questions)

---

## 1. Цели и контекст

- Auth должен быть **простым**: без сторонних OAuth-провайдеров, без логина/пароля.
- Идентификация — по **email + одноразовому коду (OTP)**.
- Доступ к API — по **JWT access token**, серверная ревокация — через **refresh token + таблицу sessions**.
- LLM-ключи и любые секреты — только на бэке. Фронт не получает ничего, кроме access/refresh.
- Ноутбуки привязаны к пользователю: один владелец, многоустройственный доступ, ручной и автоматический sync.

---

## 2. Стратегия авторизации

### 2.1. Flow

```
[FE] email
  │
  ▼
POST /auth/otp/request { email }
  │  ├─ prod  → 204 (email отправлен)
  │  └─ dev   → 200 { otp: "123456" }
  ▼
[user вводит OTP]
  │
  ▼
POST /auth/otp/verify { email, otp }
  │
  ▼
{ accessToken, refreshToken, user }
  │
  ▼
[FE → localStorage]
  │
  ▼
... запросы с Authorization: Bearer <accessToken> ...
  │
  ▼ (по 401 или по exp)
POST /auth/refresh { refreshToken }
  │
  ▼
{ accessToken, refreshToken }   ← refresh rotation
  │
  ▼
POST /auth/logout  → revoke session
```

### 2.2. Принципы

- **User создаётся лениво.** При первом успешном `otp/verify` для нового email — создаётся запись в `users`. Отдельной регистрации нет.
- **OTP одноразовый.** После успешного verify помечается `used_at` и больше не принимается.
- **Refresh rotation.** При каждом `/auth/refresh` старый refresh инвалидируется (`sessions.revoked_at` или `sessions.refresh_token_hash` перезаписывается на новый). Reuse старого refresh = атака → инвалидируем все сессии пользователя.
- **Logout — серверный.** Помечает `sessions.revoked_at`. Любая последующая попытка refresh с этим токеном → 401.
- **Access не отзывается.** При logout фронт сразу выкидывает access из памяти, но любой in-flight запрос с этим access успешно отработает до его `exp`. Это осознанный trade-off в пользу простоты и performance (никаких blocklist-проверок на каждый запрос).

---

## 3. JWT: формат и параметры

| Параметр | Значение |
|---|---|
| Algorithm | **HS256** |
| Secret | `JWT_SECRET` (env, минимум 32 байта random) |
| Access TTL | **15 минут** |
| Refresh TTL | **30 дней** |
| Clock skew tolerance | 30 секунд |

### 3.1. Access token payload

```json
{
  "sub": "<user_id>",
  "sessionId": "<session_id>",
  "iat": 1716220800,
  "exp": 1716221700
}
```

- `sub` — id пользователя.
- `sessionId` — id записи в `sessions`. Используется для аудита, не для проверки отзыва на каждом запросе.
- `iat`, `exp` — стандарт.

### 3.2. Refresh token

- Refresh — это **opaque random string** (32+ байта, base64url), а не JWT. JWT-формат для refresh избыточен: он всё равно проверяется через БД.
- В БД хранится **хеш** (`sha256` или `argon2`), не сам токен. Утечка БД не даёт активные refresh-токены.

---

## 4. Модели данных

Все таблицы в схеме `app`.

### 4.1. `users`

| Колонка | Тип | Описание |
|---|---|---|
| `id` | `uuid` PK | Идентификатор пользователя. |
| `email` | `citext` UNIQUE NOT NULL | Email, case-insensitive. |
| `display_name` | `text` NULL | Опционально, для UI. |
| `created_at` | `timestamptz` NOT NULL DEFAULT now() | |
| `last_login_at` | `timestamptz` NULL | Обновляется на каждом успешном verify. |
| `biometric_snapshot` | `jsonb` NULL | Placeholder для будущей биометрии (см. §9). |

### 4.2. `otps`

| Колонка | Тип | Описание |
|---|---|---|
| `id` | `uuid` PK | |
| `user_id` | `uuid` FK → users.id NULL | NULL до первого verify (user ещё не создан). |
| `email` | `citext` NOT NULL | Денормализация, чтобы выдавать OTP до создания user. |
| `code_hash` | `text` NOT NULL | sha256(code + per-row salt) или argon2. Никогда не хранить plain. |
| `expires_at` | `timestamptz` NOT NULL | now() + 5 минут. |
| `used_at` | `timestamptz` NULL | NULL = ещё не использован. |
| `attempts` | `int` NOT NULL DEFAULT 0 | Счётчик неудачных попыток verify. |
| `created_at` | `timestamptz` NOT NULL DEFAULT now() | |

**Индексы:**
- `(email, created_at DESC)` — для последнего активного OTP пользователя.
- TTL-cleanup через cron: `DELETE FROM otps WHERE expires_at < now() - interval '1 day'`.

### 4.3. `sessions`

| Колонка | Тип | Описание |
|---|---|---|
| `id` | `uuid` PK | Совпадает с `sessionId` в JWT. |
| `user_id` | `uuid` FK → users.id NOT NULL | |
| `refresh_token_hash` | `text` NOT NULL UNIQUE | sha256(refresh). |
| `user_agent` | `text` NULL | Для аудита. |
| `ip` | `inet` NULL | Для аудита. |
| `created_at` | `timestamptz` NOT NULL DEFAULT now() | |
| `expires_at` | `timestamptz` NOT NULL | now() + 30 дней. |
| `revoked_at` | `timestamptz` NULL | NULL = активна. |

**Индексы:**
- `(refresh_token_hash)` UNIQUE — поиск при `/auth/refresh`.
- `(user_id, revoked_at)` — активные сессии пользователя.

### 4.4. `notebooks`

| Колонка | Тип | Описание |
|---|---|---|
| `id` | `uuid` PK | |
| `owner_id` | `uuid` FK → users.id NOT NULL | |
| `title` | `text` NOT NULL | |
| `format_version` | `int` NOT NULL DEFAULT 1 | См. §7 о версионировании. |
| `cells` | `jsonb` NOT NULL DEFAULT '[]' | Массив ячеек, см. §7.2. |
| `created_at` | `timestamptz` NOT NULL DEFAULT now() | |
| `updated_at` | `timestamptz` NOT NULL DEFAULT now() | Обновляется на каждом save. Равен `max(cells[].updatedAt, notebook.updated_at)`. |
| `deleted_at` | `timestamptz` NULL | Soft-delete. |

**Индексы:**
- `(owner_id, updated_at DESC)` — список ноутбуков пользователя.
- Partial index `(owner_id) WHERE deleted_at IS NULL`.

---

## 5. API-контракт

Base prefix: `/api/v1`. Все endpoint’ы возвращают JSON. Error envelope:

```json
{ "code": "machine_readable_code", "message": "Human readable" }
```

### 5.1. `POST /api/v1/auth/otp/request`

Инициирует выдачу OTP для email.

**Request:**
```json
{ "email": "user@example.com" }
```

**Response — production (`APP_ENV=prod`):** `204 No Content`. Email с кодом уходит через внешний email-сервис.

**Response — dev/local/test (`APP_ENV in (dev, local, test)`):** `200 OK`:
```json
{ "otp": "123456", "expiresAt": "2026-05-21T10:05:00Z" }
```

**Errors:**
- `400 invalid_email` — невалидный формат.
- `429 too_many_otp_requests` — rate limit превышен (см. §10).

**Side effects:**
- Все предыдущие неиспользованные OTP этого email помечаются `used_at = now()` (инвалидация).
- Создаётся новая запись в `otps` с `expires_at = now() + 5 мин`.
- Проверяется rate limit по email.

### 5.2. `POST /api/v1/auth/otp/verify`

Проверяет OTP и выдаёт пару токенов.

**Request:**
```json
{ "email": "user@example.com", "otp": "123456" }
```

**Response 200:**
```json
{
  "accessToken": "eyJhbGciOi...",
  "refreshToken": "r7K3...base64url...",
  "user": { "id": "uuid", "email": "user@example.com", "displayName": null }
}
```

**Errors:**
- `400 invalid_otp` — код не совпал. `attempts += 1`. На 5-й неудаче OTP помечается `used_at = now()`.
- `400 otp_expired` — `expires_at < now()`.
- `400 otp_already_used` — повторная попытка использовать уже использованный код.

**Side effects:**
- Если user с этим email не существует — создаётся.
- OTP помечается `used_at = now()`.
- `users.last_login_at = now()`.
- Создаётся запись в `sessions` с `refresh_token_hash`, `expires_at = now() + 30 дней`.
- Генерируется access JWT (`sub = user.id`, `sessionId = session.id`, TTL 15 мин).

### 5.3. `POST /api/v1/auth/refresh`

Выполняет ротацию refresh токена и выдаёт новый access.

**Request:**
```json
{ "refreshToken": "r7K3..." }
```

**Response 200:**
```json
{ "accessToken": "eyJhbGciOi...", "refreshToken": "newR9X..." }
```

**Errors:**
- `401 invalid_refresh` — хеш не найден в `sessions`.
- `401 refresh_revoked` — `sessions.revoked_at IS NOT NULL`. Дополнительно: все активные сессии этого пользователя отзываются (подозрение на reuse).
- `401 refresh_expired` — `sessions.expires_at < now()`.

**Side effects:**
- Обновляется `sessions.refresh_token_hash` на хеш нового refresh.
- `sessions.expires_at` не продлевается на 30 дней, хранит изначальный дедлайн. Иначе «вечная» сессия.

### 5.4. `POST /api/v1/auth/logout`

**Headers:** `Authorization: Bearer <access>`.

**Request:**
```json
{ "refreshToken": "r7K3..." }
```

**Response:** `204 No Content`.

**Side effects:**
- `sessions.revoked_at = now()` для указанного refresh. Если refresh не найден — idempotent возвращаем 204.

### 5.5. `GET /api/v1/auth/me`

**Headers:** `Authorization: Bearer <access>`.

**Response 200:**
```json
{ "id": "uuid", "email": "user@example.com", "displayName": null }
```

**Errors:**
- `401 unauthorized` — access невалиден / просрочен.

---

## 6. Local / dev / test режим

Режим определяется переменной `APP_ENV`. Вспомогательный helper (например `settings.is_local_like`) возвращает `True` для `dev`, `local`, `test`.

| Режим | Email-вызов | OTP в ответе | HTTP code |
|---|---|---|---|
| `prod` | Да, через внешний API | Нет | 204 |
| `dev` / `local` / `test` | Нет | Да, в JSON | 200 |

**Defence-in-depth:** Endpoint **никогда** не возвращает OTP в prod — это должно быть покрыто интеграционным тестом, который выставляет `APP_ENV=prod` и проверяет `204` + отсутствие поля `otp` в боди.

**Выбор поставщика email:** открытый вопрос. Отправка email должна быть абстрагирована интерфейсом `EmailService.send_otp(email, code)`, чтобы поставщика можно было заменить. В `dev`/`local`/`test` реализация — no-op stub, пишущий в лог.

---

## 7. Notebook persistence

### 7.1. Где хранится что

- **Клиент (IndexedDB):** каноническая локальная копия, работает оффлайн.
- **Сервер (PostgreSQL):** master для sync между устройствами. Без авторизации синхронизация не работает (auth wall на фронте).

### 7.2. Формат ячейки в `notebooks.cells`

```json
[
  {
    "id": "cell-1",
    "kind": "code",
    "content": "console.log('hi')",
    "updatedAt": "2026-05-21T10:00:00Z"
  },
  {
    "id": "cell-2",
    "kind": "markdown",
    "content": "## Hello",
    "updatedAt": "2026-05-21T10:01:00Z"
  }
]
```

**Поля:**
- `id` — стабильный client-generated id (UUID v4 или short id). Не меняется при перемещении. Необходим для LWW.
- `kind` — `'code'` или `'markdown'` (выровняли с `ui/src/features/notebook/domain/cell.ts`).
- `content` — исходный текст. Для `code` — JavaScript. Для `markdown` — GFM.
- `updatedAt` — ISO timestamp последнего изменения ячейки. Используется для LWW (§8).

**Порядок ячеек** — порядок в массиве. Дополнительных полей для «order» не храним — это упрощает модель. При перемещении ячейки изменяется notebook-уровень `updated_at`, но не `cell.updatedAt`.

### 7.3. Форматное версионирование

Краткая справка. Полное описание стратегии версионирования — §9.

- `notebooks.format_version` (`int`, начиная с `1`).
- Source of truth — серверная константа `CURRENT_FORMAT_VERSION` (см. §9.2).
- При изменении формата вводится migration step на бэке, который при чтении ноутбука с устаревшей `format_version` мигрирует его на лету.
- Старые версии формата должны оставаться читаемыми в любом случае — никаких «ломающих» изменений без migration step.


### 7.4. Notebook API (краткий обзор)

> Детальный контракт — отдельный тикет. Здесь — только shape, важный для auth/persistence.

Все endpoint’ы требуют `Authorization: Bearer <access>`.

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/notebooks` | Список ноутбуков текущего пользователя. |
| `POST` | `/api/v1/notebooks` | Создать новый. |
| `GET` | `/api/v1/notebooks/{id}` | Получить по id. 403 если `owner_id != current_user.id`. |
| `PATCH` | `/api/v1/notebooks/{id}` | Обновление. Принимает полный массив `cells` и `title`. Conflict resolution — см. §8. |
| `DELETE` | `/api/v1/notebooks/{id}` | Soft-delete: `deleted_at = now()`. |

---

## 8. Conflict resolution

Пользователь может редактировать один ноутбук с разных устройств, иногда оффлайн. Сервер при `PATCH` решает конфликты.

### 8.1. Алгоритм

**LWW per-cell по `updatedAt`:**

1. Клиент присылает полный массив `cells` (каждая ячейка с `updatedAt`).
2. Сервер читает текущий ноутбук из базы.
3. Для каждого `cell.id` из объединённого множества (client ∪ server):
   - Если ячейка есть только в client → взять client.
   - Если ячейка есть только в server → взять server.
   - Если в обоих → взять ту, у которой `updatedAt` больше (LWW).
4. Порядок ячеек — берём с client (last writer определяет порядок). Новые ячейки из server (которых нет в client) — добавляются в конец.
5. `notebooks.updated_at = max(merged_cells[].updatedAt)`.

### 8.2. Ограничения этой стратегии

- **Edit war внутри одной ячейки** — изменения с «проигравшего» устройства теряются. Это врождённое ограничение LWW.
- **Clock skew** — время берём с клиента. Если часы расходятся на минуты — LWW может выбрать «не ту» версию. Принимаем этот риск.
- **Full CRDT** (например Yjs) — в backlog. Переход потребует перехода на другую модель хранения.

### 8.3. Удаление ячеек

Ячейка, удалённая на client, не присылается в PATCH. Но если на server эта ячейка была обновлена позже «времени удаления» — сервер сохранит её (LWW). Чтобы это работало корректно, клиент должен присылать **tombstones**: `{ id, deletedAt }`. В формат v1 это не включено — ячейка возвращается при синхронизации с другого устройства. **Open question** для v2.

---

## 9. Версионирование

Раздел собирает в одном месте всю информацию о версионировании в бэкенде: формат хранения заметки, API, JWT, миграции СУБД. Парный раздел на фронте — `ui/docs/auth.md` «Версионирование».

### 9.1. Что версионируется

| Сущность | Схема версионирования | Где подробности |
|---|---|---|
| Формат заметки | `notebooks.format_version` — целое число, monotonic. | §7.3, §9.2–9.5 |
| API | URL-префикс `/api/v1`. Breaking-changes → `/api/v2`. | §5 |
| JWT | Поле `alg` в заголовке и фиксированный set клеймов. Смена алгоритма = breaking. | §3 |
| Схема базы | Liquibase changesets, прикладываются на старте. | §4, `api/liquibase/changelog/` |

### 9.2. Source of truth для `format_version`

- Константа «текущая версия» живёт в бэке: `app/modules/notebooks/format.py::CURRENT_FORMAT_VERSION` (планируемое расположение).
- Эта константа экспортируется в OpenAPI-схему (`docs/openapi.json`) как `components.schemas.Notebook.properties.formatVersion.const` — чтобы фронт при регенерации типов видел точную версию контракта.
- Фронт руками декларирует `MAX_SUPPORTED_FORMAT_VERSION` — это версия, на которую рассчитан код рендера. Она может отставать от бэка между deploy-ами.

### 9.3. Кто инкрементирует

Инкремент — только бэкенд, при выпуске новой формат-миграции. Фронтенд никогда не меняет `formatVersion` в исходящих PATCH-запросах.

Условия инкремента:
- Переименование или удаление существующего поля.
- Изменение типа или семантики существующего поля.
- Добавление нового `cell.kind`, который старый фронт не умеет рендерить.
- Ввод tombstones, batched edits, или других изменений в conflict-resolution протоколе (§8).

Добавление опциональных полей, которые старый клиент может безопасно игнорировать, — инкремент НЕ требуется. См. §9.5 о форвард-совместимости.

### 9.4. Миграция при чтении

При любом чтении ноутбука из базы бэк выполняет «migrate-on-read»:

```python
while notebook.format_version < CURRENT_FORMAT_VERSION:
    migrator = MIGRATIONS[notebook.format_version]   # v1→v2, v2→v3, ...
    notebook = migrator(notebook)
    notebook.format_version += 1
```

- Миграции — явные, по одна на переход, живут в `app/modules/notebooks/format_migrations/`.
- Результат миграции записывается в базу при ближайшем успешном записывающем запросе (lazy persist). Пользовательский GET не должен блокироваться записью.
- Старые версии обязаны оставаться читаемыми бэком всегда. Удаление миграции возможно только после явного backfill (UPDATE всех записей в базе до текущей версии).

### 9.5. Что делать, если `formatVersion > MAX_SUPPORTED` на фронте

Бэк отдаёт ноутбук в «new»-версии формата (даунгрейд не выполняет — это ведёт к потере данных).

Реакция фронта описана в `ui/docs/auth.md` «Версионирование». Коротко: read-only режим + баннер «обновите страницу».

**Форвард-совместимость внутри одной версии:** в пределах одного `formatVersion` клиент игнорирует неизвестные поля в ячейке и ноутбуке и при PATCH возвращает их обратно без изменений. Это позволяет бэку вводить опциональные поля без бампа версии.

### 9.6. История версий формата

| Version | Status | Released | Changes |
|---|---|---|---|
| `1` | current | TBD | Базовый формат: `cells: [{ id, kind: 'code'\|'markdown', content, updatedAt }]`. |

При вводе новой версии добавляется строка в таблицу и соответствующий migrator в `app/modules/notebooks/format_migrations/`.

---

## 10. Биометрия (future)

Биометрия **не реализуется в рамках этой задачи**. Подготавливаем схему:

- `users.biometric_snapshot` (`jsonb`) — планируемая структура:
  ```json
  {
    "deviceId": "<uuid>",
    "publicKey": "<base64>",
    "algorithm": "webauthn",
    "registeredAt": "2026-05-21T10:00:00Z"
  }
  ```
- Будущие endpoint’ы (зарезервированы):
  - `POST /api/v1/auth/biometric/register`
  - `POST /api/v1/auth/biometric/verify`
- WebAuthn — ожидаемая технология.

---

## 11. Rate limiting и защита от abuse

| Endpoint | Limit |
|---|---|
| `POST /auth/otp/request` | 3 запроса / 15 мин на email. Сверх этого — важен и пер-IP limit (например 20 / 15 мин). |
| `POST /auth/otp/verify` | 10 попыток / 15 мин на email. Дополнительно: 5 неудачных попыток на один OTP → инвалидация этого OTP. |
| `POST /auth/refresh` | 60 / мин на sessionId. Reuse старого refresh → отзыв всех сессий пользователя. |

- **CAPTCHA** — не в v1. Добавим если появятся злоупотребления.
- **Sessions retention** — при logout ставится `revoked_at`. Cron удаляет записи старше **90 дней** после revocation/expiration. Объём аудита ограничен.
- **OTP cleanup** — cron удаляет `otps WHERE expires_at < now() - interval '1 day'`.

---

## 12. Переменные окружения

| Variable | Default | Description |
|---|---|---|
| `APP_ENV` | `dev` | `prod`, `dev`, `local`, `test`. Управляет поведением OTP-endpoint’а. |
| `JWT_SECRET` | — (required) | Секрет для HS256. Минимум 32 байта random. |
| `JWT_ACCESS_TTL_SECONDS` | `900` | 15 минут. |
| `JWT_REFRESH_TTL_SECONDS` | `2592000` | 30 дней. |
| `OTP_TTL_SECONDS` | `300` | 5 минут. |
| `OTP_MAX_ATTEMPTS` | `5` | Неудачных попыток до инвалидации. |
| `OTP_RATE_LIMIT_PER_EMAIL` | `3` | Запросов / 15 мин. |
| `EMAIL_PROVIDER` | `noop` | `sendgrid` / `resend` / `postmark` / `smtp` / `noop`. В dev/local/test — `noop`. |
| `EMAIL_PROVIDER_API_KEY` | — | Ключ вендора. |
| `EMAIL_FROM` | `no-reply@notebook.example` | From-адрес. |

Существующие в `app/core/config.py` переменные `token_ttl_seconds` (86400) и `session_ttl_seconds` (604800) подлежат замене на пару access/refresh выше. Переменные `oauth_name_*` — удалить, OAuth-провайдеры не используются.

---

## 13. Open questions

- **Email-вендор**: SendGrid / Resend / Postmark / self-hosted SMTP — выбор делается при реализации (отдельный тикет).
- **Tombstones для удалённых ячеек**: в v1 не включены. При синхронизации с устаревшего устройства удалённая ячейка может вернуться в ноутбук.
- **Audit log**: отдельная таблица `auth_events` (login, logout, refresh_revoked, otp_failed)? Не в v1.
