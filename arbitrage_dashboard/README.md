# Arbitrage Dashboard (FastAPI)

Новый проект в отдельной папке для быстрого и красивого веб-дашборда арбитража.

## Что уже оптимизировано
- Асинхронная загрузка MEXC + Bybit параллельно.
- Для BingX используется bulk-режим (1 запрос на endpoint без symbol), а затем fallback на точечные запросы только если нужно.
- Кеш обновляется в фоне, UI читает только `/api/data`.
- Клиентская фильтрация и сортировка выполняются мгновенно в браузере.
- Настройки (`min_vol`, `min_spread`, `enabled`, `refresh_sec`) сохраняются в `arb_dashboard_config.json`.

## Запуск

### Быстрый запуск в Windows (без консоли)
- Просто дважды кликните `run_dashboard.bat` в папке проекта.
- Батник сам:
  - создаст `.venv` (если его нет),
  - установит зависимости (если не установлены),
  - запустит дашборд на `http://127.0.0.1:8000`.

### Linux / macOS
```bash
cd arbitrage_dashboard
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

### Windows (cmd)
```bat
cd arbitrage_dashboard
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python app.py
```

### Windows (PowerShell)
```powershell
cd arbitrage_dashboard
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

Открыть: http://127.0.0.1:8000

## Важно про GitHub и бинарные файлы
- В репозитории намеренно игнорируются бинарные ассеты из `assets/logos` и `assets/sounds` (кроме `README.txt`).
- Это сделано, чтобы избежать ошибки `Binary files are not supported` при обновлении репозитория.
- Если хотите добавить свои логотипы/звуки локально — просто копируйте их в эти папки, в Git они не попадут.

## Доступ к локальному FastAPI в интернете через ngrok

### 1) Установка ngrok в проект (локально)

```bash
cd arbitrage_dashboard
./scripts/install_ngrok.sh
```

Скрипт установит бинарь локально в папку проекта: `.tools/ngrok/ngrok` (в репозиторий не коммитится).

### 2) Запуск FastAPI + ngrok вручную

Сначала запуск API:

```bash
uvicorn app:app --reload --port 8000
```

Потом в другом терминале запуск ngrok:

```bash
.tools/ngrok/ngrok http 8000
```

Или, если `ngrok` уже установлен глобально:

```bash
ngrok http 8000
```

### 3) Быстрый запуск одним скриптом

```bash
./scripts/run_with_ngrok.sh
```

### 4) Пример публичной ссылки от ngrok

После запуска ngrok вы увидите что-то вроде:

```text
Forwarding  https://abc12345.ngrok-free.app -> http://localhost:8000
```

Эту `https://...ngrok-free.app` ссылку можно открыть из интернета.

## Ошибка Codex про обновление PR

Если видите сообщение:

`Codex в настоящее время не поддерживает обновление PR, обновляемых за пределами Codex. Создайте новый PR.`

Это ожидаемое ограничение платформы. Решение:

1. Зафиксируйте изменения новым коммитом.
2. Создайте **новый PR** (не обновляйте старый PR, который изменялся вне Codex).
3. Закройте старый PR, если он больше не нужен.
