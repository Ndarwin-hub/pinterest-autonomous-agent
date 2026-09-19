#!/bin/sh
set -eu

if [ "${AMAZON_CRON_MODE:-false}" = "true" ]; then
  python -c "import os,json,urllib.request; u='https://web-production-dae68.up.railway.app/amazon/run-batch'; req=urllib.request.Request(u,data=json.dumps({'batch':1}).encode(),headers={'Content-Type':'application/json','X-Scheduler-Secret':os.environ['AMAZON_BATCH_SECRET']},method='POST'); print(urllib.request.urlopen(req,timeout=60).read().decode())"
  exit 0
fi

exec python -c "import agent,image_priority,pin_supervisor; image_priority.install(agent); pin_supervisor.install_runtime(agent); import quality_patch,uvicorn; uvicorn.run('main:app',host='0.0.0.0',port=int(__import__('os').environ['PORT']))"
