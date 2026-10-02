from __future__ import annotations
import asyncio, json, logging, os, shutil, uuid
from pathlib import Path
from daily_ledger import ledger as daily_ledger, BATCH_SIZE
from video_renderer import render_video, affiliate_url, make_music
from video_publisher import publish_video
from kaggle_video_handshake import get_pair_run, run_is_stale, pair_product_statuses
from composio_video_verify import verify_run

log=logging.getLogger(__name__)
PAIR_STARTS=(1,3,5,7,9);PLATFORMS=("facebook","instagram","youtube","x","tiktok");ACTIVE={};SINGLE_ACTIVE=set()
_ORIGINAL_CLAIM=daily_ledger.claim_video_job;_ORIGINAL_MARK=daily_ledger.mark_video_job

def _claim_video_job_with_run(day,batch,slot,owner):
    normalized=owner
    if str(owner).startswith("kaggle-"): normalized=f"kaggle-{day}-pair-{((int(batch)-1)//2)*2+1}"
    return _ORIGINAL_CLAIM(day,batch,slot,normalized)

def _mark_video_job_with_run(day,batch,slot,status,owner=None,error=None,publication_json=None):
    normalized=owner
    if owner and str(owner).startswith("kaggle-"): normalized=f"kaggle-{day}-pair-{((int(batch)-1)//2)*2+1}"
    return _ORIGINAL_MARK(day,batch,slot,status,normalized,error,publication_json)

daily_ledger.claim_video_job=_claim_video_job_with_run;daily_ledger.mark_video_job=_mark_video_job_with_run

def _pair_for(start):return (start,start+1)
def _next_pointer():return int(daily_ledger.get_video_pair_state().get("next_pair_start") or 1)
def _kaggle_decision(day,start):
    run=get_pair_run(day,start)
    if not run:return "missing",None
    status=str(run.get("status") or "").upper()
    if status=="COMPLETED":return "completed",run
    if status=="RUNNING" and not run_is_stale(run):return "running",run
    if status in {"FAILED","INCOMPLETE"} or run_is_stale(run):return "fallback",run
    return "unknown",run

async def on_pinterest_batch_start(day,batch):
    start=_next_pointer()
    if start not in PAIR_STARTS or int(batch)!=start+1:return {"status":"ignored","reason":"not_current_video_pair","next_pair_start":start}
    key=f"{day}:{start}"
    if ACTIVE.get(key):return {"status":"already_active","pair":_pair_for(start)}
    decision,run=_kaggle_decision(day,start)
    if decision=="completed":
        internal=pair_product_statuses(day,start,run.get("run_id"));external=verify_run(run.get("run_id"))
        if internal.get("complete") and external.get("available") and external.get("complete"):
            daily_ledger.advance_video_pair(start,day,reason="kaggle_completed_composio_verified");return {"status":"kaggle_completed_verified","pair":_pair_for(start),"verification":{"internal":internal,"composio":external}}
        if not external.get("available"):return {"status":"kaggle_completed_unverified","pair":_pair_for(start),"verification":{"internal":internal,"composio":external}}
        task=asyncio.create_task(_run_pair(day,start,run.get("run_id")));ACTIVE[key]=task;return {"status":"railway_fallback_started","pair":_pair_for(start),"kaggle_run_id":run.get("run_id"),"verification":{"internal":internal,"composio":external}}
    if decision=="running":return {"status":"kaggle_running","pair":_pair_for(start),"run_id":run.get("run_id")}
    if decision=="unknown":return {"status":"kaggle_status_unknown","pair":_pair_for(start)}
    task=asyncio.create_task(_run_pair(day,start,run.get("run_id") if run else None));ACTIVE[key]=task;return {"status":"railway_fallback_started","pair":_pair_for(start),"kaggle_run_id":run.get("run_id") if run else None}

async def on_kaggle_job_failure(day,batch,slot,error=None):
    """Take over exactly one failed Kaggle video job; never re-run successful Kaggle jobs."""
    key=f"{day}:{int(batch)}:{int(slot)}"
    if key in SINGLE_ACTIVE:return {"status":"already_active","batch":int(batch),"slot":int(slot)}
    rows=daily_ledger.video_batch(day,int(batch))
    row=next((r for r in rows if int(r.get("slot") or 0)==int(slot)),None)
    if not row:return {"status":"ignored","reason":"video_job_not_found","batch":int(batch),"slot":int(slot)}
    if _terminal(row) and str(row.get("status")).lower()=="completed":return {"status":"already_completed","batch":int(batch),"slot":int(slot)}
    owner=f"railway-video-fallback-{int(batch)}-{int(slot)}-{uuid.uuid4().hex[:8]}"
    claim=daily_ledger.claim_video_job(day,int(batch),int(slot),owner)
    if not claim or not claim.get("claimed"):return {"status":"claim_lost","batch":int(batch),"slot":int(slot)}
    SINGLE_ACTIVE.add(key)
    try:
        await asyncio.to_thread(_process_product,day,((int(batch)-1)//2)*2+1,int(batch),row,owner,None,None)
        return {"status":"railway_fallback_completed","batch":int(batch),"slot":int(slot)}
    except Exception as exc:
        err=str(exc)[:1000]
        # A product-level failure must remain recoverable. Do not leave a
        # permanently failed terminal state after a transient renderer/CDN error.
        daily_ledger.mark_video_job(day,int(batch),int(slot),"queued",owner=None,error=err)
        return {"status":"railway_fallback_retryable","batch":int(batch),"slot":int(slot),"error":err}
    finally:
        SINGLE_ACTIVE.discard(key)

async def _run_pair(day,start,kaggle_run_id=None):
    key=f"{day}:{start}";owner=f"railway-video-{start}-{uuid.uuid4().hex[:8]}"
    try:
        daily_ledger.start_video_pair(day,start,owner);external=verify_run(kaggle_run_id) if kaggle_run_id else {"available":False,"products":{}};results=[await _run_batch(day,start,batch,owner,kaggle_run_id,external) for batch in _pair_for(start)]
        if all(results):daily_ledger.advance_video_pair(start,day,reason="railway_pair_processed")
        else:daily_ledger.finish_video_pair(day,start,owner,"incomplete","One or more pair batches had no durable video handoff jobs")
    except Exception as exc:log.exception("Railway video pair %s failed: %s",start,exc);daily_ledger.finish_video_pair(day,start,owner,"failed",str(exc)[:1000])
    finally:ACTIVE.pop(key,None)

def _terminal(row):return str(row.get("status") or "").lower() == "completed"
async def _run_batch(day,pair_start,batch,owner,kaggle_run_id=None,external=None):
    rows=daily_ledger.video_batch(day,batch)
    if not rows:log.warning("Video pair %s: batch %s has no durable Pinterest video handoff; pair will not advance",pair_start,batch);return False
    processed=0; failures=0
    for row in rows[:BATCH_SIZE]:
        # A prior Kaggle claim can leave the durable row in "running" while
        # the actual output was never handed off. Railway owns the recovery
        # after the explicit pair reset, so reclaim stale running rows here.
        if str(row.get("status") or "").lower()=="running":
            try:
                daily_ledger.mark_video_job(day,batch,row["slot"],"queued",owner=owner,error="railway_recovery_reclaim")
                row["status"]="queued"
            except Exception:
                pass
        if str(row.get("status") or "").lower()=="failed":
            # Failed is a retryable product outcome, not a terminal state.
            # Requeue it so the next durable recovery pass can try the next image/render/publish tier.
            try:
                daily_ledger.mark_video_job(day,batch,row["slot"],"queued",owner=owner,error="video_retryable_failure")
                row["status"]="queued"
            except Exception:
                pass
        if _terminal(row):
            processed+=1
            continue
        claim=daily_ledger.claim_video_job(day,batch,row["slot"],owner)
        if not claim or not claim.get("claimed"):
            failures+=1
            continue
        try:
            await asyncio.to_thread(_process_product,day,pair_start,batch,row,owner,kaggle_run_id,external)
            processed+=1
        except Exception as exc:
            failures+=1
            log.exception("Video product failed day=%s batch=%s slot=%s asin=%s: %s",day,batch,row.get("slot"),row.get("asin"),exc)
            daily_ledger.mark_video_job(day,batch,row["slot"],"failed",owner=owner,error=str(exc)[:1000])
    return failures==0 and processed==len(rows[:BATCH_SIZE])

def _resolve_product_images(row, materialize_dir=None):
    """Recover and validate exact-product images; never pass unverified candidates to the renderer."""
    from image_quality import validate_many, filter_obvious_non_product, _alternative_identity_ok

    raw=[]
    try:
        handoff=json.loads(row.get("image_urls_json") or "[]")
        raw.extend({"url":str(u),"provider":"product_page","source":"Pinterest exact-product handoff","original":True}
                   for u in handoff if isinstance(u,str) and u.startswith(("http://","https://")))
    except Exception:
        pass

    product={
        "asin":str(row.get("asin") or "").upper(),
        "name":str(row.get("title") or ""),
        "url":str(row.get("product_url") or ""),
    }
    # When the upstream title is truncated to a brand/category, recover the
    # Amazon URL slug as additional exact-product search evidence.
    if len(product["name"].split()) < 3:
        try:
            from urllib.parse import urlparse, unquote
            path=unquote(urlparse(product["url"]).path).strip("/")
            slug=path.split("/dp/")[0].split("/gp/product/")[0].split("/")[-1]
            slug=re.sub(r"[-_]+"," ",slug)
            if len(slug.split()) >= 3:
                product["name"]=slug
        except Exception:
            pass

    async def recover():
        out=[]
        # Use the same canonical Composio image-search source as Pinterest,
        # but accept a result only after the exact-product identity gate below.
        try:
            from image_priority import _composio_image_search
            q=f'"{product["name"]}" product photo'
            if product["asin"]:
                q+=f' "{product["asin"]}" Amazon'
            out.extend(await _composio_image_search(q,num=30))
        except Exception as exc:
            log.warning("Video image recovery canonical Composio image search failed asin=%s: %s",product["asin"],str(exc)[:240])
        try:
            from image_quality import search_amazon_product_images
            out.extend(await search_amazon_product_images(product))
        except Exception as exc:
            log.warning("Video image recovery Amazon gallery failed asin=%s: %s",product["asin"],str(exc)[:240])
        try:
            from image_quality import search_amazon_api_images
            out.extend(await search_amazon_api_images(product))
        except Exception as exc:
            log.warning("Video image recovery Amazon Creators API failed asin=%s: %s",product["asin"],str(exc)[:240])
        try:
            from image_quality import search_verified_web_product_page_images
            out.extend(await search_verified_web_product_page_images(product,num=20))
        except Exception as exc:
            log.warning("Video image recovery verified product pages failed asin=%s: %s",product["asin"],str(exc)[:240])
        # Independent Bing image search is only an evidence source. The candidate
        # is accepted only when its result/source page proves the exact product.
        try:
            from image_quality import search_independent_bing_images
            q=f'"{product["name"]}" product photo'
            if product["asin"]: q+=f' "{product["asin"]}"'
            out.extend(await search_independent_bing_images(q,num=20))
        except Exception as exc:
            log.warning("Video image recovery independent image search failed asin=%s: %s",product["asin"],str(exc)[:240])
        try:
            from image_quality import search_amazon_asin_cdn_images
            out.extend(await search_amazon_asin_cdn_images(product))
        except Exception as exc:
            log.warning("Video image recovery ASIN CDN failed asin=%s: %s",product["asin"],str(exc)[:240])
        return out

    recovered=asyncio.run(recover())
    candidates=[]
    seen=set()
    for item in raw+recovered:
        u=str(item.get("url") or "")
        if u and u not in seen:
            seen.add(u)
            candidates.append(item)

    try:
        validated=asyncio.run(validate_many(candidates))
    except Exception as exc:
        log.warning("Video image validation failed asin=%s: %s",product["asin"],str(exc)[:240])
        validated=[]

    validated=filter_obvious_non_product(validated,product)
    trusted=[]
    # Exact provenance does not bypass byte validation: CDN error/placeholder bytes
    # must never become trusted video inputs.
    for item in validated:
        provider=str(item.get("provider") or "")
        if provider in {"product_page","amazon_direct","amazon_asin_cdn","amazon_creators_api"}:
            trusted.append(item)
        else:
            try:
                if asyncio.run(_alternative_identity_ok(item,product)):
                    item["identity_gate"]="passed"
                    trusted.append(item)
            except Exception:
                pass

    # Prefer Amazon/exact handoff, then verified evidence sources, then other
    # identity-gated candidates. Do not require four original files here:
    # video_renderer.py creates additional views only from a verified source.
    rank={"product_page":0,"amazon_creators_api":1,"amazon_direct":2,"amazon_asin_cdn":3,
          "verified_web_product_page":4,"independent_bing_image":5}
    trusted.sort(key=lambda x:(rank.get(str(x.get("provider") or ""),9),
                               -max(int(x.get("width") or 0),int(x.get("height") or 0))))
    # Try at least one candidate from every surviving provider before filling the remainder.
    selected=[]; seen_providers=set()
    for item in trusted:
        provider=str(item.get("provider") or "")
        if provider not in seen_providers:
            selected.append(item); seen_providers.add(provider)
    for item in trusted:
        if item not in selected: selected.append(item)
        if len(selected)>=20: break
    if not selected:
        # Last exact-product fallback: use Bing's own thumbnail only when the
        # search result metadata passes the same ASIN/title identity gate.
        # This is intentionally lower-resolution and is normalized locally; it
        # is never a generic image substitution.
        try:
            from urllib.request import Request, urlopen
            from io import BytesIO
            from PIL import Image, ImageStat
            for item in candidates:
                if str(item.get("provider") or "") != "independent_bing_image":
                    continue
                if not str(item.get("thumbnail_url") or "").startswith(("http://","https://")):
                    continue
                try:
                    if not asyncio.run(_alternative_identity_ok(item,product)):
                        continue
                    req=Request(str(item["thumbnail_url"]),headers={"User-Agent":"Mozilla/5.0 PinterestAgent/video"})
                    raw_thumb=urlopen(req,timeout=15).read()
                    if len(raw_thumb)<10000 or len(raw_thumb)>8*1024*1024:
                        continue
                    im=Image.open(BytesIO(raw_thumb)).convert("RGB")
                    if min(im.size)<160:
                        continue
                    stat=ImageStat.Stat(im.resize((64,64)))
                    if min(stat.stddev)<7:
                        continue
                    target=1000/max(1,min(im.size))
                    if target>1:
                        im=im.resize((max(1000,int(im.width*target)),max(1000,int(im.height*target))),Image.Resampling.LANCZOS)
                    if materialize_dir:
                        md=Path(materialize_dir);md.mkdir(parents=True,exist_ok=True)
                        p=md/f"bing_thumb_{len(selected)+1}.jpg"
                        im.save(p,format="JPEG",quality=95,optimize=True)
                        selected.append({"url":str(p),"provider":"independent_bing_thumbnail","source":str(item.get("source") or "Bing exact-product result"),"identity_gate":"passed_thumbnail_fallback","width":im.width,"height":im.height})
                        output_urls.append(str(p))
                        if len(selected)>=5:
                            break
                except Exception:
                    continue
        except Exception as exc:
            log.warning("Video Bing thumbnail fallback failed asin=%s: %s",product["asin"],str(exc)[:180])
    if not selected:
        # Last-resort exact-ASIN recovery: fetch only URLs that were constructed
        # from this exact ASIN (or came from the verified product-page handoff).
        # We still require a real decodable image with usable dimensions/entropy;
        # this bypasses only the search-result identity gate, never byte integrity.
        try:
            import httpx, io
            from PIL import Image, ImageStat
            exact=[]
            for item in candidates:
                provider=str(item.get("provider") or "")
                u=str(item.get("url") or "")
                if provider not in {"amazon_asin_cdn","product_page","amazon_direct","amazon_creators_api"}:
                    continue
                if product["asin"] and provider.startswith("amazon") and product["asin"] not in u.upper():
                    continue
                try:
                    rr=httpx.get(u,timeout=20,follow_redirects=True,headers={"User-Agent":"Mozilla/5.0 PinterestAgent/video","Accept":"image/*"})
                    if rr.status_code>=400 or len(rr.content)<1000:
                        continue
                    im=Image.open(io.BytesIO(rr.content)).convert("RGB")
                    if min(im.size)<160:
                        continue
                    stat=ImageStat.Stat(im.resize((64,64)))
                    if min(stat.stddev)<5:
                        continue
                    if materialize_dir:
                        md=Path(materialize_dir);md.mkdir(parents=True,exist_ok=True)
                        p=md/f"exact_asin_{len(exact)+1}.jpg"
                        im.save(p,format="JPEG",quality=95,optimize=True)
                        exact.append({"url":str(p),"provider":provider,"source":item.get("source") or "Exact ASIN source","identity_gate":"exact_asin_bytes_passed","width":im.width,"height":im.height})
                    else:
                        exact.append(dict(item,width=im.width,height=im.height,identity_gate="exact_asin_bytes_passed"))
                    if len(exact)>=5:
                        break
                except Exception:
                    continue
            if exact:
                selected=exact
                output_urls=[x["url"] for x in exact]
                log.warning("Video exact-ASIN byte fallback accepted asin=%s images=%s",product["asin"],len(exact))
        except Exception as exc:
            log.warning("Video exact-ASIN byte fallback failed asin=%s: %s",product["asin"],str(exc)[:240])
    if not selected:
        raise RuntimeError(f"VIDEO_IMAGE_RECOVERY_FAILED: no verified exact-product image survived validation for {product['asin']}")
    output_urls=[x["url"] for x in selected]
    # Materialize the exact bytes that passed validation. This prevents a second
    # Amazon/CDN request from returning a different 160px placeholder to ffmpeg.
    if materialize_dir:
        from image_quality import materialize_validated_url
        md=Path(materialize_dir);md.mkdir(parents=True,exist_ok=True)
        local=[]
        materialized_items=[]
        from PIL import Image
        for idx,item in enumerate(selected,1):
            p=md/f"verified_{idx}.img"
            try:
                # Exact-ASIN fallback may already have produced a normalized local file.
                # Preserve it instead of trying to fetch the local path as a URL.
                if str(item.get("url") or "").startswith("/") and Path(str(item["url"])).exists():
                    src=Path(str(item["url"]))
                    im=Image.open(src).convert("RGB")
                    jpg=md/f"verified_{len(local)+1}.jpg"
                    im.save(jpg,format="JPEG",quality=95,optimize=True)
                    local.append(str(jpg)); materialized_items.append(item)
                    if len(local)>=5: break
                    continue
                ok_materialized=asyncio.run(materialize_validated_url(item["url"],str(p)))
                if not ok_materialized:
                    import httpx
                    rr=httpx.get(item["url"],timeout=20,follow_redirects=True,
                                 headers={"User-Agent":"Mozilla/5.0 PinterestAgent/quality","Accept":"image/*"})
                    if rr.status_code<400 and len(rr.content)>100:
                        p.write_bytes(rr.content); ok_materialized=True
                if ok_materialized:
                    im=Image.open(p).convert("RGB")
                    if im.width < 160 or im.height < 160:
                        raise ValueError(f"decoded image too small: {im.width}x{im.height}")
                    jpg=md/f"verified_{len(local)+1}.jpg"
                    im.save(jpg,format="JPEG",quality=95,optimize=True)
                    p.unlink(missing_ok=True)
                    local.append(str(jpg)); materialized_items.append(item)
                    if len(local)>=5: break
            except Exception as exc:
                p.unlink(missing_ok=True)
                log.warning("Video verified image normalization failed asin=%s idx=%s: %s",product["asin"],idx,str(exc)[:180])
        if local:
            output_urls=local
            selected=materialized_items
        if len(local)<1:
            raise RuntimeError(f"VIDEO_IMAGE_MATERIALIZATION_FAILED: {len(local)} verified product images materialized from {len(trusted)} identity-gated candidates")
    try:
        daily_ledger.attach_video_images(day=row.get("day"),asin=row["asin"],
            images=[{"url":x["url"],"provider":"video_verified_exact_product"} for x in selected])
    except Exception:
        pass
    log.info("Video verified image recovery asin=%s candidates=%s selected=%s providers=%s materialized=%s",
             product["asin"],len(trusted),len(selected),sorted(set(str(x.get("provider") or "") for x in selected)),
             len([x for x in output_urls if not x.startswith(("http://","https://"))]))
    return output_urls, "verified_exact_product_recovery"

def _process_product(day,pair_start,batch,row,owner,kaggle_run_id=None,external=None):
    root=Path(os.getenv("VIDEO_WORK_DIR","/data/video-runs"))/day/f"batch_{batch}"/f"slot_{row['slot']}"
    root.mkdir(parents=True,exist_ok=True)
    output=root/f"{row['asin']}.mp4"
    try:
        urls,image_source=_resolve_product_images(row,materialize_dir=root/"verified_inputs")
        if len(urls)<1:
            raise RuntimeError("VIDEO_IMAGE_HANDOFF_MISSING: no verified product images")
        prior=json.loads(row.get("publication_json") or "{}") if row.get("publication_json") else {}
        prior_statuses=prior.get("platforms") or {}
        kaggle_status=(pair_product_statuses(day,pair_start,kaggle_run_id).get("products") or {}).get(str(row["asin"]).upper(),{}) if kaggle_run_id else {}
        composio_status=((external or {}).get("products") or {}).get(str(row["asin"]).upper(),{}) if external else {}
        combined_statuses=dict(kaggle_status.get("platforms") or {})
        combined_statuses.update(composio_status.get("platforms") or {})
        combined_statuses.update(prior_statuses)
        failed=[p for p in PLATFORMS if p in combined_statuses and str(combined_statuses[p].get("status","")).upper() not in ("PUBLISHED","SUCCESS","SUBMITTED")]
        targets=failed or [p for p in PLATFORMS if p not in combined_statuses]
        if not targets and combined_statuses:
            daily_ledger.mark_video_job(day,batch,row["slot"],"completed",owner=owner,
                publication_json=json.dumps({"platforms":combined_statuses,"source":"kaggle+composio"},separators=(",",":"))[:12000])
            return True
        track_state=daily_ledger.next_video_music([f"track{i:02d}" for i in range(1,17)])
        music=make_music(track_state["track_id"],root/"music")
        log.info("Video render asin=%s batch=%s slot=%s source=%s images=%s",row["asin"],batch,row["slot"],image_source,len(urls))
        render_video(urls,output,row.get("title") or "",music)
        aff=affiliate_url(row["asin"])
        marker=f"[video-run:{kaggle_run_id or 'railway'}][asin:{str(row['asin']).upper()}][batch:{batch}]"
        caption=f"{(row.get('title') or row['asin'])[:150]}\\nShop now: {aff}\\n{marker}\\n\\nAs an Amazon Associate I earn from qualifying purchases."
        result=publish_video(str(output),row.get("title") or row["asin"],caption,targets)
        merged=dict(combined_statuses)
        merged.update(result.get("platforms") or {})
        ok=any(str(v.get("status","")).upper() in ("PUBLISHED","SUCCESS","SUBMITTED") for v in merged.values())
        combined=dict(result)
        combined["platforms"]=merged
        combined["run_id"]=kaggle_run_id or "railway"
        combined["source"]="railway_fallback"
        daily_ledger.mark_video_job(day,batch,row["slot"],"completed" if ok else "failed",owner=owner,
            publication_json=json.dumps(combined,separators=(",",":"))[:12000])
        return ok
    finally:
        shutil.rmtree(root,ignore_errors=True)

def pair_status(day=None):
    # The pair state is durable, but ACTIVE is intentionally in-memory. Railway
    # restarts therefore used to leave GitHub monitoring a pair whose asyncio task
    # had vanished. Re-arm a persisted running/incomplete pair from the status
    # endpoint itself; the monitor polls this endpoint, so recovery survives restarts.
    day=day or daily_ledger.today_str()
    state=daily_ledger.get_video_pair_state()
    start=int(state.get("next_pair_start") or 1)
    key=f"{day}:{start}"
    status=str(state.get("status") or "").lower()
    if start in PAIR_STARTS and status in {"running","incomplete"}:
        task=ACTIVE.get(key)
        if task is not None and task.done():
            ACTIVE.pop(key,None)
            task=None
        if task is None:
            try:
                task=asyncio.create_task(_run_pair(day,start,None),name=f"video-auto-resume-{start}")
                ACTIVE[key]=task
                log.warning("Video pair auto-resumed/restarted from durable state day=%s pair=%s status=%s",day,start,status)
            except RuntimeError:
                pass
    return {"day":day,"next_pair_start":start,"pair":_pair_for(start),"status":state.get("status")}
