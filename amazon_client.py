"""Amazon Creators API client. Dormant unless all Amazon credentials exist."""
from __future__ import annotations
import logging, os, time
from typing import Any, Dict, List, Optional
import httpx
logger = logging.getLogger("pinterest-agent.amazon_client")
CREATORS_API_HOST = os.getenv("AMAZON_CREATORS_API_HOST", "https://creatorsapi.amazon")

def amazon_credentials_present() -> bool:
    return all(os.getenv(k, "").strip() for k in ("AMAZON_CLIENT_ID", "AMAZON_CLIENT_SECRET", "AMAZON_PARTNER_TAG"))

def amazon_config() -> Dict[str, str]:
    return {"client_id":os.getenv("AMAZON_CLIENT_ID","").strip(),"client_secret":os.getenv("AMAZON_CLIENT_SECRET","").strip(),"partner_tag":os.getenv("AMAZON_PARTNER_TAG","").strip(),"marketplace":os.getenv("AMAZON_MARKETPLACE","www.amazon.com").strip(),"credential_version":os.getenv("AMAZON_CREDENTIAL_VERSION","3.1").strip()}

def _token_url(version: str) -> str:
    override=os.getenv("AMAZON_TOKEN_URL","").strip()
    if override: return override
    return {"3.1":"https://api.amazon.com/auth/o2/token","3.2":"https://api.amazon.co.uk/auth/o2/token","3.3":"https://api.amazon.co.jp/auth/o2/token"}.get(version,"https://api.amazon.com/auth/o2/token")

class AmazonCreatorsClient:
    def __init__(self):
        if not amazon_credentials_present(): raise RuntimeError("Amazon Creators API credentials are not configured")
        self.cfg=amazon_config(); self._token:Optional[str]=None; self._expires=0.0
    async def _ensure_token(self, client:httpx.AsyncClient)->str:
        if self._token and time.time() < self._expires-60: return self._token
        body={"grant_type":"client_credentials","client_id":self.cfg["client_id"],"client_secret":self.cfg["client_secret"],"scope":os.getenv("AMAZON_OAUTH_SCOPE","creatorsapi::default")}
        r=await client.post(_token_url(self.cfg["credential_version"]),json=body,headers={"Content-Type":"application/json"},timeout=30)
        if r.status_code>=400:
            logger.error("Amazon token request failed status=%s",r.status_code); raise RuntimeError(f"Amazon OAuth token failed: HTTP {r.status_code}")
        data=r.json(); token=data.get("access_token")
        if not token: raise RuntimeError("Amazon OAuth response missing access_token")
        self._token=token; self._expires=time.time()+int(data.get("expires_in") or 3600); return token
    async def _post(self,path:str,body:Dict[str,Any])->Dict[str,Any]:
        async with httpx.AsyncClient() as client:
            token=await self._ensure_token(client)
            r=await client.post(f"{CREATORS_API_HOST.rstrip('/')}{path}",headers={"Authorization":f"Bearer {token}","Content-Type":"application/json","x-marketplace":self.cfg["marketplace"]},json=body,timeout=45)
            if r.status_code>=400:
                logger.error("Creators API %s failed status=%s",path,r.status_code); raise RuntimeError(f"Creators API {path} HTTP {r.status_code}")
            return r.json()
    async def search_items(self,keywords:str,*,item_count:int=10,sort_by:Optional[str]=None,browse_node_id:Optional[str]=None)->List[Dict[str,Any]]:
        body={"partnerTag":self.cfg["partner_tag"],"keywords":keywords,"itemCount":max(1,min(item_count,10)),"marketplace":self.cfg["marketplace"],"resources":["images.primary.large","itemInfo.title","itemInfo.features","itemInfo.classifications","offersV2.listings.price","offersV2.listings.availability","browseNodeInfo.browseNodes.salesRank","browseNodeInfo.websiteSalesRank"]}
        if sort_by: body["sortBy"]=sort_by
        if browse_node_id: body["browseNodeId"]=browse_node_id
        data=await self._post("/catalog/v1/searchItems",body)
        return list(data.get("items") or (data.get("searchResult") or {}).get("items") or data.get("Items") or (data.get("SearchResult") or {}).get("Items") or [])
    async def get_items(self,asins:List[str])->List[Dict[str,Any]]:
        body={"partnerTag":self.cfg["partner_tag"],"itemIds":asins[:10],"itemIdType":"ASIN","marketplace":self.cfg["marketplace"],"resources":["images.primary.large","itemInfo.title","itemInfo.classifications","offersV2.listings.price","offersV2.listings.availability"]}
        data=await self._post("/catalog/v1/getItems",body)
        return list(data.get("items") or (data.get("itemsResult") or {}).get("items") or data.get("Items") or (data.get("ItemsResult") or {}).get("Items") or [])

def extract_detail_page_url(item:Dict[str,Any])->Optional[str]:
    for k in ("detailPageURL","detailPageUrl","DetailPageURL","url","URL"):
        v=item.get(k)
        if isinstance(v,str) and v.startswith("http"): return v
    return None

def extract_asin_from_item(item:Dict[str,Any])->Optional[str]:
    for k in ("asin","ASIN","itemId","ItemId"):
        v=item.get(k)
        if isinstance(v,str) and len(v)==10: return v.upper()
    return None

def extract_title(item:Dict[str,Any])->str:
    for path in (("title",),("itemInfo","title","displayValue"),("ItemInfo","Title","DisplayValue")):
        cur:Any=item
        for p in path:
            if not isinstance(cur,dict) or p not in cur: break
            cur=cur[p]
        else:
            if isinstance(cur,str): return cur
    return ""
