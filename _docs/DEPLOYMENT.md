# Деплой Dentiva: Шаг за шагом

## Что получим в итоге

- Фронтенд: https://dentiva-dashboard.vercel.app
- Бэкенд: https://dentiva-api.railway.app
- База данных: PostgreSQL на Railway (автоматически)

---

## Часть 1: Деплой бэкенда на Railway

1. Зарегистрируйся на https://railway.app (можно через GitHub)
2. Нажми **"New Project"** → **"Deploy from GitHub repo"**
3. Выбери репозиторий `mssvva-del/dentiva-backend`
4. Railway найдёт Dockerfile автоматически
5. Добавь PostgreSQL: кликни **"New"** → **"Database"** → **"Add PostgreSQL"**
6. Перейди в твой API сервис → вкладка **"Variables"**
7. Добавь переменные (значения из `dentiva-backend/.env`):

   | Переменная | Откуда взять |
   |---|---|
   | `DATABASE_URL` | Railway → PostgreSQL сервис → кнопка "Copy Connection URL", замени `postgresql://` на `postgresql+asyncpg://`. **⚠️ Этот URL — суперпользователь.** Суперпользователь ОСВОБОЖДЁН от Row-Level Security, то есть изоляция клиник (PHI одной клиники от другой) не работает вообще, молча. До первой живой клиники подключение обязано идти от роли `dentiva_app` (создаётся миграцией `b2c3d4e5f6a7_app_role`): `postgresql+asyncpg://dentiva_app:<пароль>@<host>:<port>/<db>`. Проверка: `GET /health/detailed` → `rls_enforced` должно быть `true`. |
   | `DATABASE_URL_SYNC` | Тот же URL, но с `postgresql+psycopg2://` вместо `postgresql://` |
   | `CLERK_SECRET_KEY` | Clerk Dashboard → API Keys |
   | `CLERK_PUBLISHABLE_KEY` | Clerk Dashboard → API Keys |
   | `CLERK_JWKS_URL` | Clerk Dashboard → API Keys (JWKS URL) |
   | `RETELL_WEBHOOK_SECRET` | Retell Dashboard → твой агент |
   | `GROQ_API_KEY` | console.groq.com |
   | `ANTHROPIC_API_KEY` | console.anthropic.com |
   | `ENCRYPTION_KEY` | Сгенерируй: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
   | `ENVIRONMENT` | `production` |
   | `LOG_LEVEL` | `INFO` |
   | `PMS_ADAPTER` | `mock` (для демо) |

8. Нажми **"Deploy"** — Railway соберёт Docker образ (~3-5 минут)
9. Скопируй публичный URL: вкладка **Settings** → **Domains** → **Generate Domain**
   - Пример: `https://dentiva-api-production.railway.app`

---

## Часть 2: Обновить webhook URL в Retell

После получения Railway URL — обнови и синхронизируй агента:

```bash
cd ~/Projects/dentiva-starter/dentiva-voice
# Открой .env и измени BACKEND_URL на Railway URL
# Потом синхронизируй агента:
python3 scripts/sync_agent.py
```

---

## Часть 3: Загрузить demo-данные на Railway

```bash
cd ~/Projects/dentiva-starter/dentiva-backend
# Замени YOUR_RAILWAY_DB_URL на Connection URL из Railway PostgreSQL
DATABASE_URL_SYNC=YOUR_RAILWAY_DB_URL python3 scripts/seed_demo.py
```

---

## Часть 4: Деплой фронтенда на Vercel

1. Зарегистрируйся на https://vercel.com (через GitHub)
2. Нажми **"Add New Project"**
3. Выбери репозиторий `mssvva-del/dentiva-dashboard`
4. Framework: **Next.js** (определится автоматически)
5. Root Directory: оставь пустым (или укажи `dentiva-dashboard` если спросит)
6. Добавь **Environment Variables**:

   | Переменная | Значение |
   |---|---|
   | `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` | Из Clerk Dashboard → API Keys |
   | `CLERK_SECRET_KEY` | Из Clerk Dashboard → API Keys |
   | `NEXT_PUBLIC_API_URL` | Твой Railway URL (например `https://dentiva-api-production.railway.app`) |

7. Нажми **"Deploy"**
8. Получишь URL: `https://dentiva-dashboard.vercel.app`

---

## Часть 5: Настроить Clerk для нового домена

1. Зайди на https://dashboard.clerk.com
2. Выбери своё приложение
3. **Settings** → **Domains**
4. Добавь: `dentiva-dashboard.vercel.app`
5. В **Redirect URLs** добавь: `https://dentiva-dashboard.vercel.app/login`

---

## Финальная проверка

- [ ] https://dentiva-dashboard.vercel.app открывается
- [ ] Можно войти через Clerk
- [ ] Dashboard показывает данные (метрики, звонки, бронирования)
- [ ] `/settings` показывает название практики

---

## Обновление кода (CI/CD)

После любого `git push` в GitHub:
- Vercel автоматически деплоит новую версию фронтенда
- Railway автоматически деплоит новую версию бэкенда

Полный CI/CD без дополнительных настроек.

---

## Важные заметки

**Стоимость Railway:**
- Бесплатный план: $5 кредита в месяц. При активном использовании может закончиться.
- Для стабильной работы: Railway Starter — $5/мес.

**Ключи для будущих агентов:**
- После создания испанского и русского Retell-агентов — добавь в Railway Variables:
  - `RETELL_AGENT_ID_ES`
  - `RETELL_AGENT_ID_RU`

**Свой домен (dentiva.app и т.п.):**
- Настраивается через Vercel: Settings → Domains
- Не забудь добавить этот домен и в Clerk Dashboard

---

## Что уже готово в коде

- `Dockerfile` — Railway соберёт автоматически
- `railway.toml` — запускает миграции (`alembic upgrade head`) перед стартом сервера
- `vercel.json` — говорит Vercel использовать pnpm и Next.js
- CORS настроен: разрешены `localhost:3000` и все `*.vercel.app` домены
- `/health` эндпоинт для Railway healthcheck


## ⚠️ ALLOW_SUPERUSER_DB — временный аварийный выход

Бэкенд отказывается стартовать в production, если его роль в БД — SUPERUSER или
BYPASSRLS: такая роль обходит все политики RLS, и изоляция клиник превращается в
украшение. `ALLOW_SUPERUSER_DB=true` позволяет всё равно подняться.

Это допустимо ТОЛЬКО пока нет ни одной живой клиники с настоящими данными
пациентов. Перед первой платящей клиникой:

1. Переключить `DATABASE_URL` на роль `dentiva_app`.
2. Убрать `ALLOW_SUPERUSER_DB` из переменных Railway.
3. Проверить `GET /health/detailed` → `"rls_enforced": true`.

Пока `rls_enforced` не `true`, любые две клиники в одной базе видят данные друг
друга при первой же ошибке в коде — RLS их не остановит.
