import os,json,urllib.request,time

BASE="https://web-production-dae68.up.railway.app"
SECRET=os.environ["AMAZON_BATCH_SECRET"]

def post(path,payload,headers,retries=8):
    last_error=None
    for attempt in range(1,retries+1):
        try:
            req=urllib.request.Request(BASE+path,data=json.dumps(payload).encode(),headers={"Content-Type":"application/json",**headers},method="POST")
            with urllib.request.urlopen(req,timeout=60) as response:
                body=response.read().decode()
                print(path,response.status,body)
                return body
        except Exception as exc:
            last_error=exc
            print(f"{path} attempt {attempt}/{retries} failed: {type(exc).__name__}: {exc}",flush=True)
            if attempt < retries: time.sleep(min(30,5*attempt))
    raise last_error

# Pin A remains the universal wake interface, but a transient Pin A/gateway failure
# must never prevent the actual daily scheduler activation.
try:
    post("/pin-a",{"source":"railway-cron","request_id":"railway-cron"},{"X-Pin-A-Secret":SECRET,"X-Pin-A-Source":"railway-cron"})
except Exception as exc:
    print(f"Pin A wake failed after retries; continuing to mandatory batch activation: {type(exc).__name__}: {exc}",flush=True)

# Mandatory execution activation uses the same Railway-owned daily scheduler/ledger.
# Repeated wakes remain idempotent and cannot create competing batch ownership.
post("/amazon/run-batch",{"batch":1,"scheduler_run_id":"railway-cron","scheduled_local_time":"06:00"},{"X-Scheduler-Secret":SECRET},retries=10)
