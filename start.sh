#!/bin/sh
set -eu

# Railway owns the long-running web/scheduler process.
# Legacy AMAZON_CRON_MODE one-shot startup is intentionally ignored so it
# cannot recurse into /amazon/run-batch or make the web service exit.
exec python -c "import agent,image_priority,pin_supervisor; image_priority.install(agent); pin_supervisor.install_runtime(agent); import quality_patch,uvicorn; uvicorn.run('main:app',host='0.0.0.0',port=int(__import__('os').environ['PORT']))"
