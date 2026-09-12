"""Amazon candidate discovery with durable duplicate protection and replacements."""
from __future__ import annotations
import logging
from typing import Any,Dict,List,Optional
from amazon_client import AmazonCreatorsClient,amazon_credentials_present,extract_asin_from_item,extract_detail_page_url,extract_title
from amazon_boards import BOARD_SEARCH_PROFILES
from published_registry import registry
logger=logging.getLogger("pinterest-agent.amazon_discovery"); MAX_REPLACEMENTS_PER_SLOT=5
RATES={"luxury beauty":10.0,"luxury stores beauty":10.0,"digital music":5.0,"physical music":5.0,"handmade":5.0,"digital videos":5.0,"physical books":4.5,"kitchen":4.5,"automotive":4.5,"apparel":4.0,"watches":4.0,"jewelry":4.0,"luggage":4.0,"shoes":4.0,"handbags":4.0,"home":3.0,"furniture":3.0,"sports":3.0,"outdoors":3.0,"tools":3.0,"beauty":3.0,"headphones":3.0,"toys":3.0,"pc":2.5,"television":2.0,"digital video games":2.0,"health":1.0,"personal care":1.0,"grocery":1.0}
def is_dormant(): return not amazon_credentials_present()
def _blob(i):
    parts=[extract_title(i)]; info=i.get("itemInfo") or {}
    for k in ("features","classifications"):
        v=info.get(k) if isinstance(info,dict) else None
        if isinstance(v,dict): parts += [str(x) for x in (v.get("displayValues") or [])]
    b=i.get("browseNodeInfo") or {}
    for n in (b.get("browseNodes") or []) if isinstance(b,dict) else []:
        if isinstance(n,dict): parts += [str(n.get("displayName", "")),str(n.get("contextFreeName", ""))]
    return " ".join(parts).lower()
def _rank(i):
    b=i.get("browseNodeInfo") or {}; vals=[]
    if isinstance(b,dict):
        x=b.get("websiteSalesRank")
        if isinstance(x,dict): x=x.get("salesRank") or x.get("rank")
        if x: vals.append(x)
        for n in b.get("browseNodes") or []:
            if isinstance(n,dict) and n.get("salesRank"): vals.append(n["salesRank"])
    nums=[]
    for x in vals:
        try:
            n=int(x)
            if n>0: nums.append(n)
        except: pass
    return min(nums) if nums else None
def _rate(i):
    blob=_blob(i); hits=[(r,k) for k,r in RATES.items() if k in blob]
    return max(hits) if hits else (4.0,"all other categories planning proxy")
def _cand(i):
    asin=extract_asin_from_item(i); url=extract_detail_page_url(i)
    if not asin or not url or registry.is_published(asin=asin,url=url): return None
    rate,key=_rate(i); title=extract_title(i); rank=_rank(i); newest=int(any(x in title.lower() for x in ("newest","latest","2026")))
    return {"asin":asin,"affiliate_url":url,"title":title,"sales_rank":rank,"commission_proxy_pct":rate,"commission_proxy_note":f"planning proxy: {key}; actual rate is determined by Amazon","newest_hint":newest,"raw":i}
def _rank_board(items,profile):
    out=[]
    for i in items:
        c=_cand(i)
        if not c: continue
        c["score"]=c["newest_hint"]*20+c["commission_proxy_pct"]+(max(0,12-min(12,c["sales_rank"]/10000)) if c["sales_rank"] else 0); out.append(c)
    return sorted(out,key=lambda x:(-x["score"],x["sales_rank"] is None,x["sales_rank"] or 10**18))
def _rank_global(items):
    out=[]
    for i in items:
        c=_cand(i)
        if not c:continue
        c["score"]=c["commission_proxy_pct"]*100+c["newest_hint"]*5+(max(0,10-min(10,c["sales_rank"]/10000)) if c["sales_rank"] else 0); out.append(c)
    return sorted(out,key=lambda x:(-x["score"],x["sales_rank"] is None,x["sales_rank"] or 10**18))
async def discover_for_board(board_name:str,*,exclude_asins:Optional[set]=None,client:Optional[AmazonCreatorsClient]=None):
    if is_dormant():return None
    client=client or AmazonCreatorsClient(); profile=BOARD_SEARCH_PROFILES.get(board_name) or BOARD_SEARCH_PROFILES["Everything Else"]; excluded=set(exclude_asins or set())|registry.all_published_asins(); items=[]
    for kw in profile["keywords"]:
        try: items += await client.search_items(kw,item_count=10,sort_by="NewestArrivals")
        except Exception as e: logger.warning("Amazon search failed %s: %s",kw,e)
    for c in _rank_board(items,profile):
        if c["asin"] not in excluded:return c
    return None
async def discover_global(*,exclude_asins:Optional[set]=None,client:Optional[AmazonCreatorsClient]=None):
    if is_dormant():return None
    client=client or AmazonCreatorsClient(); excluded=set(exclude_asins or set())|registry.all_published_asins(); items=[]
    for kw in ("luxury beauty","kitchen best sellers","physical books best sellers","home furniture","apparel fashion","tools outdoors","handbags luggage","handmade gifts","new releases best sellers"):
        try: items += await client.search_items(kw,item_count=10,sort_by="Featured")
        except Exception as e: logger.warning("Amazon global search failed %s: %s",kw,e)
    for c in _rank_global(items):
        if c["asin"] not in excluded:return c
    return None
def assert_url_unmodified(original,candidate): return (original or "")== (candidate or "")
