# Coding Agents SDLC (oushcode)

Автоматизированная агентная SDLC-система для GitHub: **Code Agent** генерирует PR по Issue, **Reviewer Agent**
проверяет PR с учетом CI и оставляет review. Всё исполняется на сервере (FastAPI + worker), GitHub Actions используются
только как CI/CD.

## UI

- Панель: `https://oushcoder.duckdns.org/ui`
- В UI видны: очередь jobs, статусы, логи, артефакты запусков.

## GitHub Apps

- Code Agent: `https://github.com/apps/oushcode-code-agent`
- Reviewer Agent: `https://github.com/apps/oushcode-review-agent`

## Как это работает (коротко)

1. Установите **обе** GitHub App в нужный репозиторий (Install / Configure).
2. Создайте Issue в репозитории:
   - Code Agent получит webhook (`issues.opened`/`issues.labeled`), создаст ветку и Pull Request.
3. В репозитории должен быть любой CI/CD (хотя бы "пустой" workflow):
   - Reviewer Agent запускается **после завершения CI** (через `workflow_run.completed` и/или `check_suite.completed`),
     собирает diff + статусы/логи CI и публикует review в PR.
4. Если Reviewer нашел проблемы (`DECISION: fix`), система запускает fix-итерацию Code Agent (ограниченное число итераций).

## Архитектура

- **server**: FastAPI вебхук-сервер (`/webhook`, `/health`, `/ui`)
  - принимает события GitHub, валидирует подпись (HMAC),
  - кладет задачи в очередь.
- **worker**: исполнитель задач (jobs) в фоне
  - последовательно обрабатывает очередь,
  - взаимодействует с GitHub API от имени соответствующего GitHub App.
- **SQLite**: очередь jobs + дедупликация delivery id + состояния итераций.
- **Artifacts (файлы)**: подробные логи и большие выходы сохраняются в `ARTIFACTS_DIR`.

### Tools (allowlist)

Агенты не запускают произвольные команды: используется набор разрешенных инструментов, например:

- GitHub API: PR/Issue/комментарии/ревью, статусы и check-runs
- Локальный repo (read-only): просмотр дерева, diff, файлов, поиск (`rg`)
- CI контекст: объединенный статус + check runs по `head_sha`
