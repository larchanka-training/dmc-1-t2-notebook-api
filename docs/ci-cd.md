# CI/CD and Deployment Guide for API

Документ описывает инфраструктуру backend-сервиса `api`: переменные окружения, Docker, локальный запуск, CI в GitHub Actions и базовый сценарий deploy.

## Стек

- Python 3.12
- FastAPI
- Uvicorn
- Pytest
- Docker / Docker Compose
- GitHub Actions

## Переменные окружения

Файл-пример находится в `api/.env.example`. Для локального запуска создайте рабочий `.env`:

```bash
cd api
cp .env.example .env
```

Основные переменные:

| Переменная | Назначение | Пример |
| --- | --- | --- |
| `APP_NAME` | Название приложения FastAPI | `JS Notebook API` |
| `APP_ENV` | Окружение запуска | `dev`, `test`, `prod` |
| `API_PREFIX` | Префикс API-роутов | `/api/v1` |
| `APP_HOST` | Host для локального запуска | `0.0.0.0` |
| `APP_PORT` | Port для локального запуска | `8000` |
| `DATABASE_URL` | Строка подключения к PostgreSQL | `postgresql://admin:admin123@postgres:5432/wiki` |
| `OAUTH_NAME_APPLICATION_ID` | ID OAuth-приложения | хранить в GitHub Secrets |
| `OAUTH_NAME_SECRET_KEY` | Secret OAuth-приложения | хранить в GitHub Secrets |
| `TOKEN_TTL_SECONDS` | Время жизни access token | `86400` |
| `SESSION_TTL_SECONDS` | Время жизни сессии | `604800` |

Файл `.env` не должен попадать в git. Для production-значений используйте GitHub Actions Secrets или секреты целевой платформы.

## Docker

Backend собирается из `api/Dockerfile`.

Сборка образа из корня репозитория:

```bash
docker build -t js-notebook-api:local ./api
```

Запуск контейнера:

```bash
docker run --rm \
  --env-file api/.env \
  -p 8000:8000 \
  js-notebook-api:local
```

Проверка:

```bash
curl http://127.0.0.1:8000/api/v1/health
```

Ожидаемый ответ:

```json
{"status":"ok"}
```

## Docker Compose

Локальная инфраструктура поднимается из корня монорепозитория:

```bash
docker compose up --build
```

Сервисы:

| Сервис | URL | Назначение |
| --- | --- | --- |
| `api` | `http://127.0.0.1:8000` | FastAPI backend |
| `frontend` | `http://127.0.0.1:3000` | UI |
| `postgres` | `127.0.0.1:5432` | PostgreSQL |
| `pgadmin` | `http://127.0.0.1:5050` | Админка БД |
| `proxy` | `http://127.0.0.1` | Nginx reverse proxy |

Полезные команды:

```bash
docker compose ps
docker compose logs -f api
docker compose down
docker compose down -v
```

## Локальная разработка без Docker

```bash
cd api
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Тесты:

```bash
cd api
pytest
```

Lint:

```bash
cd api
python -m pip install ruff
ruff check .
```

## GitHub Actions CI

Workflow для backend находится в `.github/workflows/api-ci.yml`.

Pipeline запускается при:

- push в `main`
- pull request в `main`
- изменениях в `api/**` или самом workflow-файле

Этапы CI:

1. `lint`: установка зависимостей и запуск `ruff check`.
2. `test`: установка dev-зависимостей и запуск `pytest`.
3. `build`: сборка Docker-образа из `api/Dockerfile`.

Минимальные production-секреты для GitHub Actions:

| Secret | Назначение |
| --- | --- |
| `DATABASE_URL` | Production database URL |
| `OAUTH_NAME_APPLICATION_ID` | OAuth application ID |
| `OAUTH_NAME_SECRET_KEY` | OAuth secret |
| `TOKEN_TTL_SECONDS` | Access token TTL |
| `SESSION_TTL_SECONDS` | Session TTL |

Если будет добавлена публикация Docker-образа в registry, дополнительно понадобятся:

| Secret | Назначение |
| --- | --- |
| `REGISTRY_USERNAME` | Логин Docker registry |
| `REGISTRY_TOKEN` | Token/password Docker registry |

## Deployment

Базовый deploy-процесс:

1. Проверить, что PR прошел `lint`, `test` и `build` в GitHub Actions.
2. Создать production `.env` на сервере или настроить secrets в платформе deploy.
3. Собрать Docker-образ:

```bash
docker build -t js-notebook-api:<version> ./api
```

4. Запустить контейнер:

```bash
docker run -d \
  --name js-notebook-api \
  --restart unless-stopped \
  --env-file /opt/js-notebook/api.env \
  -p 8000:8000 \
  js-notebook-api:<version>
```

5. Проверить healthcheck:

```bash
curl http://127.0.0.1:8000/api/v1/health
```

6. Проверить логи:

```bash
docker logs -f js-notebook-api
```

Обновление версии:

```bash
docker stop js-notebook-api
docker rm js-notebook-api
docker run -d \
  --name js-notebook-api \
  --restart unless-stopped \
  --env-file /opt/js-notebook/api.env \
  -p 8000:8000 \
  js-notebook-api:<new-version>
```

Rollback:

```bash
docker stop js-notebook-api
docker rm js-notebook-api
docker run -d \
  --name js-notebook-api \
  --restart unless-stopped \
  --env-file /opt/js-notebook/api.env \
  -p 8000:8000 \
  js-notebook-api:<previous-version>
```

## Definition of Done

- `api/.env.example` содержит все необходимые переменные без production-секретов.
- `api/Dockerfile` собирает production-ready FastAPI image.
- `docker compose up --build api` запускает backend локально.
- GitHub Actions выполняет lint, test и Docker build.
- `api/docs/ci-cd.md` описывает локальный запуск, CI и deployment.
