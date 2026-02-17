# Arbitrage Dashboard (FastAPI)

Новый проект в отдельной папке для быстрого и красивого веб-дашборда арбитража.

## Что уже оптимизировано
- Асинхронная загрузка MEXC + Bybit параллельно.
- Для BingX используется bulk-режим (1 запрос на endpoint без symbol), а затем fallback на точечные запросы только если нужно.
- Кеш обновляется в фоне, UI читает только `/api/data`.
- Клиентская фильтрация и сортировка выполняются мгновенно в браузере.
- Настройки (`min_vol`, `min_spread`, `enabled`, `refresh_sec`) сохраняются в `arb_dashboard_config.json`.

## Запуск
```bash
cd arbitrage_dashboard
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Открыть: http://127.0.0.1:8000
