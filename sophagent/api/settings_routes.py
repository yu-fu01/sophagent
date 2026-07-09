"""运行时系统设置。当前仅一项：可调的单文件大小上限 max_upload_bytes。

GET 对所有登录用户开放（前端据此提示/预校验上传）；修改权仅 admin。"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth import require_admin, require_user
from ..config import (effective_max_upload_bytes, effective_compress_threshold,
                      effective_dingtalk_allowed_chat_ids,
                      effective_dingtalk_config, effective_dingtalk_allowed_user_ids,
                      effective_dingtalk_dm_policy,                       effective_dingtalk_free_response_chats,
                      effective_dingtalk_mention_patterns, effective_dingtalk_require_mention,
                      effective_dingtalk_home_channel, effective_dingtalk_reply_emotion,
                      effective_dingtalk_webhook_url,
                      effective_feishu_config, effective_qq_config, effective_telegram_token,
                      effective_weixin_config, effective_weixin_allowed_user_ids,
                      effective_weixin_dm_policy, effective_write_approval,
                      get_config, mask_dingtalk_client_id, mask_weixin_account_id,
                      COMPRESS_THRESHOLD_MIN, COMPRESS_THRESHOLD_MAX,
                      DEFAULT_COMPRESS_THRESHOLD)

router = APIRouter()

MIN_UPLOAD_BYTES = 1024                      # 1 KB
MAX_UPLOAD_CEILING = 1024 * 1024 * 1024      # 1 GB 硬顶（base64 整文件入内存，防 OOM）


class MaxUploadBytesBody(BaseModel):
    max_upload_bytes: int


class CompressThresholdBody(BaseModel):
    compress_threshold: float


class WriteApprovalBody(BaseModel):
    write_approval: bool


@router.get("")
async def get_settings(request: Request, _user=Depends(require_user)):
    db = request.app.state.db
    wx_cfg = await effective_weixin_config(db)
    wx_account_raw = (
        wx_cfg.account_id if wx_cfg
        else ((await db.get_setting("weixin_account_id")) or get_config().weixin_account_id or "")
    )
    cfg = get_config()
    from ..im.platforms.weixin.health import public_health_view as weixin_public_health_view
    from ..im.platforms.dingtalk.health import public_health_view as dingtalk_public_health_view
    dt_cfg = await effective_dingtalk_config(db)
    dt_client_raw = (
        dt_cfg.client_id if dt_cfg
        else ((await db.get_setting("dingtalk_client_id")) or cfg.dingtalk_client_id or "")
    )
    return {
        "max_upload_bytes": await effective_max_upload_bytes(db),
        "default_max_upload_bytes": get_config().max_upload_bytes,
        "min_bytes": MIN_UPLOAD_BYTES,
        "max_bytes": MAX_UPLOAD_CEILING,
        "compress_threshold": await effective_compress_threshold(db),
        "default_compress_threshold": DEFAULT_COMPRESS_THRESHOLD,
        "compress_threshold_min": COMPRESS_THRESHOLD_MIN,
        "compress_threshold_max": COMPRESS_THRESHOLD_MAX,
        "telegram_configured": bool(await effective_telegram_token(db)),
        "qq_configured": all(await effective_qq_config(db)),
        "feishu_configured": bool((fs_cfg := await effective_feishu_config(db)).app_id and fs_cfg.app_secret),
        "weixin_configured": bool(wx_cfg),
        "weixin_account_id": mask_weixin_account_id(wx_account_raw),
        "weixin_dm_policy": await effective_weixin_dm_policy(db),
        "weixin_allowed_user_ids": ",".join(await effective_weixin_allowed_user_ids(db)),
        "weixin_health": (
            weixin_public_health_view(cfg.data_dir, wx_account_raw)
            if wx_account_raw else {"status": "ok", "needs_relogin": False, "message": ""}
        ),
        "dingtalk_configured": bool(dt_cfg),
        "dingtalk_client_id": mask_dingtalk_client_id(dt_client_raw),
        "dingtalk_health": (
            dingtalk_public_health_view(cfg.data_dir, dt_client_raw)
            if dt_client_raw else {"status": "ok", "needs_relogin": False, "message": ""}
        ),
        "dingtalk_dm_policy": await effective_dingtalk_dm_policy(db),
        "dingtalk_allowed_user_ids": ",".join(await effective_dingtalk_allowed_user_ids(db)),
        "dingtalk_require_mention": await effective_dingtalk_require_mention(db),
        "dingtalk_allowed_chat_ids": ",".join(await effective_dingtalk_allowed_chat_ids(db)),
        "dingtalk_free_response_chats": ",".join(await effective_dingtalk_free_response_chats(db)),
        "dingtalk_mention_patterns": (
            (await db.get_setting("dingtalk_mention_patterns")) or cfg.dingtalk_mention_patterns or ""
        ),
        "dingtalk_card_template_id": (
            (await db.get_setting("dingtalk_card_template_id")) or cfg.dingtalk_card_template_id or ""
        ),
        "dingtalk_webhook_configured": bool(await effective_dingtalk_webhook_url(db)),
        "dingtalk_home_channel": await effective_dingtalk_home_channel(db),
        "dingtalk_reply_emotion": await effective_dingtalk_reply_emotion(db),
        "write_approval": await effective_write_approval(db),
    }


@router.put("/write_approval")
async def set_write_approval(body: WriteApprovalBody, request: Request,
                             admin=Depends(require_admin)):
    """Toggle the global memory write-approval gate (admin policy)."""
    await request.app.state.db.set_setting(
        "write_approval", "true" if body.write_approval else "false", admin["id"]
    )
    return {"ok": True}


@router.put("/max_upload_bytes")
async def set_max_upload_bytes(body: MaxUploadBytesBody, request: Request,
                               admin=Depends(require_admin)):
    value = body.max_upload_bytes
    if value < MIN_UPLOAD_BYTES or value > MAX_UPLOAD_CEILING:
        raise HTTPException(400, f"max_upload_bytes must be between {MIN_UPLOAD_BYTES} "
                                 f"and {MAX_UPLOAD_CEILING} bytes")
    await request.app.state.db.set_setting("max_upload_bytes", str(value), admin["id"])
    return {"ok": True}


@router.put("/compress_threshold")
async def set_compress_threshold(body: CompressThresholdBody, request: Request,
                                 admin=Depends(require_admin)):
    value = body.compress_threshold
    if value < COMPRESS_THRESHOLD_MIN or value > COMPRESS_THRESHOLD_MAX:
        raise HTTPException(400, f"compress_threshold must be between "
                                 f"{COMPRESS_THRESHOLD_MIN} and {COMPRESS_THRESHOLD_MAX}")
    await request.app.state.db.set_setting("compress_threshold", str(value), admin["id"])
    return {"ok": True}


class TelegramTokenBody(BaseModel):
    token: str


@router.put("/telegram")
async def set_telegram_token(body: TelegramTokenBody, request: Request,
                             admin=Depends(require_admin)):
    token = (body.token or "").strip()
    await request.app.state.db.set_setting("telegram_bot_token", token, admin["id"])
    # 现场生效：停旧 polling、按新 token 起新（空则停）
    await request.app.state.im_controller.restart(request.app.state.db)
    return {"ok": True, "configured": bool(token)}


class QQBotBody(BaseModel):
    app_id: str = ""
    client_secret: str = ""


@router.put("/qqbot")
async def set_qqbot_config(body: QQBotBody, request: Request,
                           admin=Depends(require_admin)):
    app_id = (body.app_id or "").strip()
    client_secret = (body.client_secret or "").strip()
    await request.app.state.db.set_setting("qq_app_id", app_id, admin["id"])
    await request.app.state.db.set_setting("qq_client_secret", client_secret, admin["id"])
    await request.app.state.im_controller.restart(request.app.state.db)
    return {"ok": True, "configured": bool(app_id and client_secret)}


@router.post("/qqbot/test")
async def test_qqbot_config(body: QQBotBody, _admin=Depends(require_admin)):
    app_id = (body.app_id or "").strip()
    client_secret = (body.client_secret or "").strip()
    if not app_id or not client_secret:
        raise HTTPException(400, "app_id and client_secret are required")
    from ..im.platforms.qqbot import QQBotClient
    client = QQBotClient(app_id, client_secret)
    try:
        gateway_url = await client.gateway_url()
    except Exception as e:
        raise HTTPException(400, f"QQ Bot connection test failed: {e}") from e
    finally:
        await client.aclose()
    return {"ok": True, "gateway": bool(gateway_url)}


@router.post("/qqbot/restart")
async def restart_qqbot(request: Request, _admin=Depends(require_admin)):
    request.app.state.im_controller.current_qq_config = None
    await request.app.state.im_controller.restart(request.app.state.db)
    return {"ok": True}


class FeishuBody(BaseModel):
    app_id: str = ""
    app_secret: str = ""
    domain: str = "feishu"
    connection_mode: str = "websocket"
    verification_token: str = ""
    encrypt_key: str = ""


@router.put("/feishu")
async def set_feishu_config(body: FeishuBody, request: Request,
                            admin=Depends(require_admin)):
    app_id = (body.app_id or "").strip()
    app_secret = (body.app_secret or "").strip()
    domain = (body.domain or "feishu").strip() or "feishu"
    connection_mode = (body.connection_mode or "websocket").strip() or "websocket"
    verification_token = (body.verification_token or "").strip()
    encrypt_key = (body.encrypt_key or "").strip()
    db = request.app.state.db
    await db.set_setting("feishu_app_id", app_id, admin["id"])
    await db.set_setting("feishu_app_secret", app_secret, admin["id"])
    await db.set_setting("feishu_domain", domain, admin["id"])
    await db.set_setting("feishu_connection_mode", connection_mode, admin["id"])
    await db.set_setting("feishu_verification_token", verification_token, admin["id"])
    await db.set_setting("feishu_encrypt_key", encrypt_key, admin["id"])
    await request.app.state.im_controller.restart(db)
    return {"ok": True, "configured": bool(app_id and app_secret)}


@router.post("/feishu/test")
async def test_feishu_config(body: FeishuBody, _admin=Depends(require_admin)):
    app_id = (body.app_id or "").strip()
    app_secret = (body.app_secret or "").strip()
    domain = (body.domain or "feishu").strip() or "feishu"
    if not app_id or not app_secret:
        raise HTTPException(400, "app_id and app_secret are required")
    from ..im.platforms.feishu import FEISHU_SETUP_CHECKLIST, FeishuClient
    client = FeishuClient(app_id, app_secret, domain=domain)
    try:
        info = await client.probe()
    except Exception as e:
        raise HTTPException(400, f"Feishu connection test failed: {e}") from e
    finally:
        await client.aclose()
    bot = info.get("bot") or {}
    return {
        "ok": True,
        "bot_name": bot.get("bot_name"),
        "open_id": bot.get("open_id"),
        "setup_checklist": list(FEISHU_SETUP_CHECKLIST),
    }


@router.post("/feishu/restart")
async def restart_feishu(request: Request, _admin=Depends(require_admin)):
    request.app.state.im_controller.current_feishu_config = None
    await request.app.state.im_controller.restart(request.app.state.db)
    return {"ok": True}


class WeixinBody(BaseModel):
    account_id: str = ""
    token: str = ""
    base_url: str = "https://ilinkai.weixin.qq.com"
    allowed_user_ids: str = ""
    dm_policy: str = "pairing"


@router.put("/weixin")
async def set_weixin_config(body: WeixinBody, request: Request,
                            admin=Depends(require_admin)):
    account_id = (body.account_id or "").strip()
    token = (body.token or "").strip()
    base_url = (body.base_url or "https://ilinkai.weixin.qq.com").strip().rstrip("/") or "https://ilinkai.weixin.qq.com"
    allowed_user_ids = (body.allowed_user_ids or "").strip()
    dm_policy = (body.dm_policy or "pairing").strip().lower() or "pairing"
    if dm_policy not in {"pairing", "allowlist", "disabled"}:
        raise HTTPException(400, "dm_policy must be pairing, allowlist, or disabled")
    db = request.app.state.db
    await db.set_setting("weixin_account_id", account_id, admin["id"])
    if token:
        await db.set_setting("weixin_token", token, admin["id"])
    elif not account_id:
        await db.set_setting("weixin_token", "", admin["id"])
    await db.set_setting("weixin_base_url", base_url, admin["id"])
    await db.set_setting("weixin_allowed_user_ids", allowed_user_ids, admin["id"])
    await db.set_setting("weixin_dm_policy", dm_policy, admin["id"])
    if account_id and token:
        from ..im.platforms.weixin.auth import persist_qr_credentials
        from ..im.platforms.weixin.health import clear_health
        persist_qr_credentials(get_config().data_dir, {
            "account_id": account_id,
            "token": token,
            "base_url": base_url,
        })
        clear_health(get_config().data_dir, account_id)
    await request.app.state.im_controller.restart(db)
    configured = bool(await effective_weixin_config(db))
    return {"ok": True, "configured": configured}


@router.post("/weixin/restart")
async def restart_weixin(request: Request, _admin=Depends(require_admin)):
    request.app.state.im_controller.current_weixin_config = None
    await request.app.state.im_controller.restart(request.app.state.db)
    return {"ok": True}


@router.post("/weixin/qr")
async def start_weixin_qr(_admin=Depends(require_admin)):
    from ..im.platforms.weixin.auth import start_qr_session
    try:
        return await start_qr_session()
    except Exception as e:
        raise HTTPException(400, f"Weixin QR login failed: {e}") from e


@router.get("/weixin/qr/{session_id}")
async def poll_weixin_qr(session_id: str, request: Request, admin=Depends(require_admin)):
    from ..im.platforms.weixin.auth import persist_qr_credentials, poll_qr_session
    try:
        result = await poll_qr_session(session_id)
    except Exception as e:
        raise HTTPException(400, f"Weixin QR poll failed: {e}") from e
    if result.get("status") == "confirmed" and result.get("token"):
        from ..im.platforms.weixin.health import clear_health
        db = request.app.state.db
        account_id = str(result.get("account_id") or "")
        token = str(result.get("token") or "")
        base_url = str(result.get("base_url") or "https://ilinkai.weixin.qq.com")
        await db.set_setting("weixin_account_id", account_id, admin["id"])
        await db.set_setting("weixin_token", token, admin["id"])
        await db.set_setting("weixin_base_url", base_url, admin["id"])
        persist_qr_credentials(get_config().data_dir, {
            "account_id": account_id,
            "token": token,
            "base_url": base_url,
            "user_id": str(result.get("user_id") or ""),
        })
        clear_health(get_config().data_dir, account_id)
        await request.app.state.im_controller.restart(db)
        return {
            "status": "confirmed",
            "message": result.get("message", "微信连接成功"),
            "account_id": account_id,
            "configured": True,
        }
    safe = dict(result)
    safe.pop("token", None)
    return safe


class DingTalkBody(BaseModel):
    client_id: str = ""
    client_secret: str = ""
    card_template_id: str = ""
    robot_code: str = ""
    allowed_user_ids: str = ""
    dm_policy: str = "pairing"
    require_mention: bool = True
    allowed_chat_ids: str = ""
    free_response_chats: str = ""
    mention_patterns: str = ""
    webhook_url: str = ""
    home_channel: str = ""
    reply_emotion: bool = True


@router.put("/dingtalk")
async def set_dingtalk_config(body: DingTalkBody, request: Request,
                              admin=Depends(require_admin)):
    client_id = (body.client_id or "").strip()
    client_secret = (body.client_secret or "").strip()
    card_template_id = (body.card_template_id or "").strip()
    robot_code = (body.robot_code or "").strip()
    allowed_user_ids = (body.allowed_user_ids or "").strip()
    dm_policy = (body.dm_policy or "pairing").strip().lower() or "pairing"
    if dm_policy not in {"pairing", "allowlist", "disabled"}:
        raise HTTPException(400, "dm_policy must be pairing, allowlist, or disabled")
    if client_id and client_secret and not card_template_id:
        existing = await request.app.state.db.get_setting("dingtalk_card_template_id")
        card_template_id = (existing or get_config().dingtalk_card_template_id or "").strip()
    db = request.app.state.db
    await db.set_setting("dingtalk_client_id", client_id, admin["id"])
    if client_secret:
        await db.set_setting("dingtalk_client_secret", client_secret, admin["id"])
    elif not client_id:
        await db.set_setting("dingtalk_client_secret", "", admin["id"])
    await db.set_setting("dingtalk_card_template_id", card_template_id, admin["id"])
    await db.set_setting("dingtalk_robot_code", robot_code, admin["id"])
    await db.set_setting("dingtalk_allowed_user_ids", allowed_user_ids, admin["id"])
    await db.set_setting("dingtalk_dm_policy", dm_policy, admin["id"])
    await db.set_setting("dingtalk_allowed_chat_ids", (body.allowed_chat_ids or "").strip(), admin["id"])
    await db.set_setting("dingtalk_free_response_chats", (body.free_response_chats or "").strip(), admin["id"])
    await db.set_setting("dingtalk_mention_patterns", (body.mention_patterns or "").strip(), admin["id"])
    await db.set_setting("dingtalk_webhook_url", (body.webhook_url or "").strip(), admin["id"])
    await db.set_setting("dingtalk_home_channel", (body.home_channel or "").strip(), admin["id"])
    await db.set_setting(
        "dingtalk_reply_emotion",
        "true" if body.reply_emotion else "false",
        admin["id"],
    )
    await db.set_setting(
        "dingtalk_require_mention",
        "true" if body.require_mention else "false",
        admin["id"],
    )
    await request.app.state.im_controller.restart(db)
    configured = bool(await effective_dingtalk_config(db))
    if configured and client_id:
        from ..im.platforms.dingtalk.health import clear_health
        clear_health(get_config().data_dir, client_id)
    return {"ok": True, "configured": configured}


@router.post("/dingtalk/test")
async def test_dingtalk_config(request: Request, _admin=Depends(require_admin)):
    db = request.app.state.db
    config = await effective_dingtalk_config(db)
    if not config:
        raise HTTPException(400, "DingTalk not configured")
    try:
        import dingtalk_stream

        credential = dingtalk_stream.Credential(config.client_id, config.client_secret)
        client = dingtalk_stream.DingTalkStreamClient(credential)
        token = client.get_access_token()
        if not token:
            raise RuntimeError("empty access_token")
        return {"ok": True, "message": "钉钉凭证有效，access_token 获取成功"}
    except Exception as exc:
        raise HTTPException(400, f"DingTalk test failed: {exc}") from exc


@router.post("/dingtalk/restart")
async def restart_dingtalk(request: Request, _admin=Depends(require_admin)):
    request.app.state.im_controller.current_dingtalk_config = None
    await request.app.state.im_controller.restart(request.app.state.db)
    return {"ok": True}


@router.post("/dingtalk/qr")
async def start_dingtalk_qr(_admin=Depends(require_admin)):
    from ..im.platforms.dingtalk.auth import start_qr_session
    try:
        return await start_qr_session()
    except Exception as e:
        raise HTTPException(400, f"DingTalk QR login failed: {e}") from e


@router.get("/dingtalk/qr/{session_id}")
async def poll_dingtalk_qr(session_id: str, request: Request, admin=Depends(require_admin)):
    from ..im.platforms.dingtalk.auth import poll_qr_session
    try:
        result = await poll_qr_session(session_id)
    except Exception as e:
        raise HTTPException(400, f"DingTalk QR poll failed: {e}") from e
    if result.get("status") == "confirmed":
        db = request.app.state.db
        client_id = str(result.get("client_id") or "")
        client_secret = str(result.get("client_secret") or "")
        await db.set_setting("dingtalk_client_id", client_id, admin["id"])
        await db.set_setting("dingtalk_client_secret", client_secret, admin["id"])
        from ..im.platforms.dingtalk.health import clear_health
        clear_health(get_config().data_dir, client_id)
        await request.app.state.im_controller.restart(db)
        return {
            "status": "confirmed",
            "message": result.get("message", "钉钉授权成功"),
            "client_id": client_id,
            "configured": bool(await effective_dingtalk_config(db)),
        }
    safe = dict(result)
    safe.pop("client_secret", None)
    return safe

