
#!/usr/bin/env python3
from pathlib import Path
import argparse, json, re

MARKER = "WOOP_TEMP_MEDIA_LIFECYCLE_V1"

NEW_PUBLISHERS = r'''
# WOOP_TEMP_MEDIA_LIFECYCLE_V1: one temporary upload per WoopSocial platform attempt.
WOOP_SOCIAL_PROJECT_ID = "177362778728693760"

def _woop_delivery(payload, platform):
    rows = payload.get("social_account_posts") or []
    return next((x for x in rows if str(x.get("platform") or "").upper() == platform),
                rows[0] if len(rows) == 1 else {})

def _woop_delete_media(media_id):
    try:
        result = composio_tool("WOOP_SOCIAL_DELETE_MEDIA", {"media_id": str(media_id)}, "woop_social")
        if result.get("deleted") is True:
            return True
    except Exception:
        pass
    # Verify absence after a lost/ambiguous delete response; never guess from list order.
    remaining = composio_tool("WOOP_SOCIAL_LIST_MEDIA", {
        "project_id": WOOP_SOCIAL_PROJECT_ID, "media_ids": [str(media_id)], "limit": 1
    }, "woop_social")
    if not (remaining.get("items") or []):
        return True
    raise RuntimeError("WOOPSOCIAL_MEDIA_DELETE_NOT_CONFIRMED")

def publish_woop_social(platform, video, product):
    if "woop_social" not in CONNECTIONS:
        raise RuntimeError("WOOPSOCIAL_CONNECTION_NOT_ACTIVE")
    if not video.exists() or video.stat().st_size <= 20000 or video.stat().st_size > 100 * 1024 * 1024:
        raise RuntimeError("WOOPSOCIAL_VIDEO_FILE_INVALID_OR_OVER_100MB")

    media_id = None
    post_id = None
    try:
        accounts = composio_tool("WOOP_SOCIAL_LIST_SOCIAL_ACCOUNTS", {"project_id": WOOP_SOCIAL_PROJECT_ID}, "woop_social")
        rows = accounts.get("social_accounts") or []
        account = next((x for x in rows if str(x.get("platform") or "").upper() == platform
                        and str(x.get("status") or "").upper() == "CONNECTED"), None)
        if not account:
            raise RuntimeError("WOOPSOCIAL_" + platform + "_ACCOUNT_NOT_CONNECTED")
        account_id = str(account.get("id") or "")
        if not account_id:
            raise RuntimeError("WOOPSOCIAL_" + platform + "_ACCOUNT_ID_MISSING")

        file_obj = stage_file("woop_social", "WOOP_SOCIAL_UPLOAD_MEDIA", video, "video/mp4")
        uploaded = composio_tool("WOOP_SOCIAL_UPLOAD_MEDIA", {
            "project_id": WOOP_SOCIAL_PROJECT_ID, "file": file_obj
        }, "woop_social")
        media_id = find_value(uploaded, ["media_id"])
        if not media_id:
            raise RuntimeError("WOOPSOCIAL_" + platform + "_MEDIA_ID_MISSING")
        media_id = str(media_id)

        if platform == "TIKTOK":
            target = {
                "social_account_id": account_id, "platform": "TIKTOK",
                "post_type": "VIDEO", "post_mode": "DIRECT_POST",
                "privacy_level": "PUBLIC_TO_EVERYONE", "allow_comment": True,
                "allow_duet": True, "allow_stitch": True,
                "is_your_brand": False, "is_branded_content": False,
                "is_ai_generated_content": False, "auto_add_music": False
            }
        elif platform == "X":
            target = {"social_account_id": account_id, "platform": "X"}
        else:
            raise RuntimeError("WOOPSOCIAL_UNSUPPORTED_PLATFORM")

        result = composio_tool("WOOP_SOCIAL_PUBLISH_POST_NOW", {
            "content": [{"text": make_caption(product),
                         "media": [{"type": "MEDIA_LIBRARY", "media_id": media_id}]}],
            "social_accounts": [target],
            "auto_delete_media_after_publish": False
        }, "woop_social")
        delivery = _woop_delivery(result, platform)
        post_id = str(result.get("id") or result.get("post_id") or delivery.get("post_id") or "")
        status = str(delivery.get("delivery_status") or "").upper()

        # Retain the asset while the platform is still processing it.
        for _ in range(60):
            if status in ("PUBLISHED", "FAILED") or not post_id:
                break
            time.sleep(5)
            detail = composio_tool("WOOP_SOCIAL_GET_POST", {"post_id": post_id}, "woop_social")
            delivery = _woop_delivery(detail, platform)
            status = str(delivery.get("delivery_status") or "").upper()

        if status not in ("PUBLISHED", "FAILED"):
            return {
                "status": "unknown", "external_id": str(delivery.get("external_post_id") or ""),
                "url": delivery.get("external_post_url"), "woopsocial_post_id": post_id or None,
                "media_id": media_id, "media_deleted": False, "cleanup_pending": True,
                "error": "Delivery did not reach PUBLISHED or FAILED within the polling window"
            }

        deleted = False
        cleanup_error = None
        try:
            deleted = _woop_delete_media(media_id)
        except Exception as exc:
            cleanup_error = str(exc)[:500]
        return {
            "status": status.lower(), "external_id": str(delivery.get("external_post_id") or ""),
            "url": delivery.get("external_post_url"), "woopsocial_post_id": post_id or None,
            "media_id": media_id, "media_deleted": deleted, "cleanup_pending": not deleted,
            "error": str(delivery.get("error_message") or cleanup_error or "")[:700] or None
        }

    except Exception as exc:
        message = str(exc)
        match = re.search(r"HTTP_(\d{3})", message)
        code = int(match.group(1)) if match else None
        # A definite 4xx rejection is terminal; timeouts and 5xx remain ambiguous.
        definitive = (code is not None and 400 <= code < 500 and code not in (408, 425, 429)) or "insufficient credits" in message.lower()
        deleted = False
        cleanup_error = None
        if media_id and definitive:
            try:
                deleted = _woop_delete_media(media_id)
            except Exception as cleanup_exc:
                cleanup_error = str(cleanup_exc)[:400]
        return {
            "status": "failed" if definitive else "unknown", "external_id": "", "url": None,
            "woopsocial_post_id": post_id or None, "media_id": media_id,
            "media_deleted": deleted, "cleanup_pending": bool(media_id and not deleted),
            "error": (message + (" | cleanup: " + cleanup_error if cleanup_error else ""))[:900]
        }

def publish_x(video, product):
    return publish_woop_social("X", video, product)

def publish_tiktok(video, product):
    return publish_woop_social("TIKTOK", video, product)

def reconcile_pending_woop_cleanup():
    if "woop_social" not in CONNECTIONS:
        return
    changed = False
    for asin, entry in state.get("products", {}).items():
        for platform in ("x", "tiktok"):
            info = (entry.get("platforms") or {}).get(platform) or {}
            media_id = info.get("media_id")
            post_id = info.get("woopsocial_post_id")
            if not info.get("cleanup_pending") or not media_id or not post_id:
                continue
            try:
                detail = composio_tool("WOOP_SOCIAL_GET_POST", {"post_id": str(post_id)}, "woop_social")
                delivery = _woop_delivery(detail, platform.upper())
                status = str(delivery.get("delivery_status") or "").upper()
                if status in ("PUBLISHED", "FAILED"):
                    _woop_delete_media(str(media_id))
                    info["media_deleted"] = True
                    info["cleanup_pending"] = False
                    info["status"] = status.lower()
                    if delivery.get("error_message"):
                        info["error"] = str(delivery.get("error_message"))[:700]
                    changed = True
                    print("WOOPSOCIAL_TEMP_MEDIA_CLEANED", asin, platform, status)
            except Exception as exc:
                print("WOOPSOCIAL_CLEANUP_RECONCILE_PENDING", asin, platform, type(exc).__name__, str(exc)[:180])
    if changed:
        save_state(state)
'''

def patch_source(source):
    if MARKER in source:
        return source
    old_required = 'for _required in ("pinterest", "googledrive", "youtube", "instagram", "facebook", "twitter", "woop_social"):'
    new_required = 'for _required in ("pinterest", "googledrive", "youtube", "instagram", "facebook", "woop_social"):'
    if source.count(old_required) != 1:
        raise RuntimeError("PATCH_GUARD_REQUIRED_CONNECTIONS_NOT_FOUND")
    source = source.replace(old_required, new_required, 1)

    start = source.find("def publish_x(video, product):")
    end = source.find("PUBLISHERS = {", start)
    if start < 0 or end < 0:
        raise RuntimeError("PATCH_GUARD_PUBLISHER_BLOCK_NOT_FOUND")
    source = source[:start] + NEW_PUBLISHERS + "\n\n" + source[end:]

    old_active = '(name == "x" and "twitter" in CONNECTIONS)'
    if source.count(old_active) != 1:
        raise RuntimeError("PATCH_GUARD_ACTIVE_X_TARGET_NOT_FOUND")
    source = source.replace(old_active, '(name == "x" and "woop_social" in CONNECTIONS)', 1)

    old_retry = '''        prior_status = str(prior.get("status") or "")
        if prior_status in ("published", "unknown", "submitting"):
            continue
'''
    new_retry = '''        prior_status = str(prior.get("status") or "")
        # X/TikTok use one attempt per ASIN. Never resubmit an earlier attempt.
        if platform in ("x", "tiktok") and prior_status not in ("", "new", "not_connected"):
            continue
        if prior_status in ("published", "unknown", "submitting"):
            continue
'''
    if source.count(old_retry) != 1:
        raise RuntimeError("PATCH_GUARD_ONE_ATTEMPT_LOOP_NOT_FOUND")
    source = source.replace(old_retry, new_retry, 1)

    old_retryable = '''        for platform in ACTIVE_TARGETS:
            pstate = platform_states.get(platform) or {}
            if pstate.get("status") == "failed" and int((entry.get("retry_counts") or {}).get(platform, 0)) < MAX_PLATFORM_RETRIES:
'''
    new_retryable = '''        for platform in ACTIVE_TARGETS:
            # WoopSocial X/TikTok attempts are terminal per ASIN; do not requeue them.
            if platform in ("x", "tiktok"):
                continue
            pstate = platform_states.get(platform) or {}
            if pstate.get("status") == "failed" and int((entry.get("retry_counts") or {}).get(platform, 0)) < MAX_PLATFORM_RETRIES:
'''
    if source.count(old_retryable) != 1:
        raise RuntimeError("PATCH_GUARD_BATCH_RETRY_FILTER_NOT_FOUND")
    source = source.replace(old_retryable, new_retryable, 1)

    old_log = '            print("VIDEO_PLATFORM_PUBLISHED", asin, platform, result.get("external_id", ""))\n'
    new_log = '''            if result.get("status") == "failed":
                print("VIDEO_PLATFORM_FAILED", asin, platform, str(result.get("error") or "")[:350])
            elif result.get("status") == "unknown":
                print("VIDEO_PLATFORM_UNKNOWN", asin, platform, str(result.get("error") or "")[:250])
            else:
                print("VIDEO_PLATFORM_PUBLISHED", asin, platform, result.get("external_id", ""))
'''
    if source.count(old_log) != 1:
        raise RuntimeError("PATCH_GUARD_PLATFORM_LOG_NOT_FOUND")
    source = source.replace(old_log, new_log, 1)

    old_run = "run_daily_worker()\n# Manifest is a Kaggle output artifact"
    new_run = "reconcile_pending_woop_cleanup()\nrun_daily_worker()\n# Manifest is a Kaggle output artifact"
    if source.count(old_run) != 1:
        raise RuntimeError("PATCH_GUARD_RUN_ENTRYPOINT_NOT_FOUND")
    source = source.replace(old_run, new_run, 1)

    compile(source, "kaggle_video_v155_patched.py", "exec")
    for required in (MARKER, '"WOOP_SOCIAL_DELETE_MEDIA"', '"WOOP_SOCIAL_GET_POST"', '"woop_social" in CONNECTIONS'):
        if required not in source:
            raise RuntimeError("PATCH_VALIDATION_MISSING:" + required)
    return source

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--notebook-dir", required=True)
    args = ap.parse_args()
    root = Path(args.notebook_dir)
    files = list(root.glob("*.ipynb"))
    if len(files) != 1:
        raise RuntimeError("EXPECTED_EXACTLY_ONE_NOTEBOOK_FOUND:" + str(len(files)))
    nb = json.loads(files[0].read_text(encoding="utf-8"))
    cells = [c for c in nb.get("cells", []) if c.get("cell_type") == "code"]
    if len(cells) != 1:
        raise RuntimeError("EXPECTED_ONE_PRODUCTION_CODE_CELL_FOUND:" + str(len(cells)))
    original = "".join(cells[0].get("source", []))
    patched = patch_source(original)
    cells[0]["source"] = patched.splitlines(keepends=True)
    files[0].write_text(json.dumps(nb, ensure_ascii=False, indent=1), encoding="utf-8")
    print("PATCHED_NOTEBOOK", files[0].name, "source_bytes", len(patched), "already_patched", MARKER in original)

if __name__ == "__main__":
    main()
