"""Additive image-selection intelligence layered on top of the protected legacy selector.

This module never replaces the legacy validation/identity/duplicate rules. It only
adds deterministic evidence, safety classification, richer ranking metadata, and
set-diversity signals. No external AI reviewer is required.
"""
from __future__ import annotations
import re
from typing import Any, Dict, List, Tuple

NON_PRODUCT_TERMS = (
    "brand logo","brand-logo","logo image","wordmark","favicon","icon",
    "banner","advertisement","advertising","wallpaper","vector","stock photo",
    "stock-photo","shutterstock","istock","getty","placeholder",
    "social share","prime logo","amazon logo","brand graphic","brand-only",
)

def _tokens(value: Any) -> List[str]:
    return [x for x in re.findall(r"[a-z0-9]+", str(value or "").lower()) if len(x) >= 3]

def _evidence_text(c: Dict[str, Any]) -> str:
    return " ".join(str(c.get(k) or "") for k in (
        "source","source_url","title","alt","caption","description","id"
    )).lower()

def is_obvious_non_product(c: Dict[str, Any], product: Dict[str,Any]) -> Tuple[bool,str]:
    text=_evidence_text(c)
    for term in NON_PRODUCT_TERMS:
        if term in text:
            return True, term
    name_tokens=set(_tokens(product.get("name")))
    brand_tokens=set(_tokens(product.get("brand")))
    evidence_tokens=set(_tokens(text))
    meaningful=name_tokens-{"with","from","this","that","product","official","amazon","new","pack","size","color","the","for","and"}
    if brand_tokens and brand_tokens & evidence_tokens and meaningful and not (meaningful & evidence_tokens):
        return True, "brand_only_without_product_evidence"
    return False,""

def annotate_candidate(c: Dict[str,Any], product: Dict[str,Any]) -> Dict[str,Any]:
    x=dict(c)
    name_tokens=set(_tokens(product.get("name")))
    brand_tokens=set(_tokens(product.get("brand")))
    text=_evidence_text(c)
    evidence=set(_tokens(text))
    meaningful=name_tokens-{"with","from","this","that","product","official","amazon","new","pack","size","color","the","for","and"}
    model_tokens={t for t in meaningful if any(ch.isdigit() for ch in t) or "-" in t}
    exact_hits=len(meaningful & evidence)
    model_hits=len(model_tokens & evidence)
    brand_hit=bool(brand_tokens & evidence)
    provider=str(x.get("provider") or "").lower()
    trusted=provider in {"amazon_creators_api","amazon_asin_cdn","amazon_direct","product_page"}
    resolution=max(int(x.get("width") or 0),int(x.get("height") or 0))
    entropy=float(x.get("entropy") or 0)
    opaque=float(x.get("opaque_ratio") or 0)
    quality=min(100, int(45 + min(25,resolution/240) + min(15,entropy*2) + (10 if opaque>=.98 else 0)))
    safety=0 if trusted else -8
    evidence_score=min(30, exact_hits*5 + model_hits*8 + (4 if brand_hit else 0))
    x.update({
        "advanced_source_trust": "amazon" if trusted else "fallback",
        "advanced_exact_token_hits": exact_hits,
        "advanced_model_hits": model_hits,
        "advanced_brand_evidence": brand_hit,
        "advanced_quality_score": quality,
        "advanced_evidence_score": evidence_score,
        "advanced_rank_signal": max(0,min(100,quality+evidence_score+safety)),
    })
    bad,reason=is_obvious_non_product(x,product)
    x["advanced_non_product"] = bad
    if reason: x["advanced_non_product_reason"]=reason
    return x

def filter_obvious_non_product(candidates: List[Dict[str,Any]], product: Dict[str,Any]) -> List[Dict[str,Any]]:
    out=[]
    for c in candidates:
        x=annotate_candidate(c,product)
        if x.get("advanced_non_product"):
            continue
        out.append(x)
    return out

def advanced_tiebreak_key(c: Dict[str,Any]) -> Tuple[int,int,int]:
    return (
        int(c.get("advanced_rank_signal") or 0),
        int(c.get("advanced_model_hits") or 0),
        int(c.get("advanced_exact_token_hits") or 0),
    )
