import os,json,urllib.request

BASE="https://web-production-dae68.up.railway.app"
SECRET=os.environ["AMAZON_BATCH_SECRET"]

def post(path,payload,headers):
    req=urllib.request.Request(
        BASE+path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type":"application/json",**headers},
        method="POST",
    )
    with urllib.request.urlopen(req,timeout=60) as response:
        print(path,response.read().decode())

post(
    "/pin-a",
    {"source":"railway-cron","request_id":"railway-cron"},
    {"X-Pin-A-Secret":SECRET,"X-Pin-A-Source":"railway-cron"},
)
post(
    "/amazon/run-batch",
    {"batch":1},
    {"X-Scheduler-Secret":SECRET},
)
