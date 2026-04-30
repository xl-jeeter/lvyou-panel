"""Lawnet Device Panel - 绿微设备群控管理系统"""

import asyncio
import hashlib
import hmac
import base64
import json
import logging
import os
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

import aiohttp
from fastapi import FastAPI, HTTPException, Query, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.database import init_db, get_db, db_connection

# ── Logging ────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("lvyou-panel")

# ── Timezone ───────────────────────────────────────────────────
CST = timezone(timedelta(hours=8))

# ── Global aiohttp session (reuse connections) ─────────────────
_http_session: aiohttp.ClientSession | None = None

async def get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            connector=aiohttp.TCPConnector(limit=20, ttl_dns_cache=300),
        )
    return _http_session

# ── Token calc ──────────────────────────────────────────────────
def calc_token(password: str) -> str:
    return hashlib.md5(f"admin|{password}".encode()).hexdigest()


# ── DingTalk signing (shared logic) ─────────────────────────────
def _sign_dingtalk_webhook(webhook_url: str, secret: str) -> str:
    """Add timestamp + HMAC signature to DingTalk webhook URL."""
    if not secret:
        return webhook_url
    timestamp = str(round(time.time() * 1000))
    sign_string = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(secret.encode("utf-8"), sign_string.encode("utf-8"), digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote(base64.b64encode(hmac_code))
    return f"{webhook_url}&timestamp={timestamp}&sign={sign}"


# ── Device communication ────────────────────────────────────────
async def device_call(ip: str, token: str, cmd: str, params: dict = None, timeout: int = 10):
    """Send command to a device via HTTP and return parsed JSON."""
    url = f"http://{ip}/ctrl"
    req_params = {"token": token, "cmd": cmd}
    if params:
        req_params.update(params)
    try:
        session = await get_http_session()
        async with session.get(url, params=req_params, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            return await resp.json()
    except asyncio.TimeoutError:
        return {"code": -1, "note": "Timeout"}
    except aiohttp.ClientError as e:
        return {"code": -1, "note": str(e)}
    except Exception as e:
        return {"code": -1, "note": str(e)}


# ── SMS polling & caching ────────────────────────────────────────
async def poll_sms(device_id: str, ip: str, token: str):
    """Fetch new SMS from device and cache locally."""
    try:
        async with db_connection() as db:
            for slot_num in ("1", "2"):
                res = await device_call(ip, token, "querysms", {"p1": "0", "p2": "50", "p4": slot_num})
                if res.get("code") != 0:
                    continue

                results = res.get("results", [])
                if not isinstance(results, list) or len(results) == 0:
                    continue

                cur = await db.execute(
                    "SELECT MAX(sms_ts) FROM sms WHERE device_id=? AND sim_slot=?",
                    (device_id, f"sim{slot_num}")
                )
                row = await cur.fetchone()
                last_ts = (row[0] or 0)

                for msg in results:
                    sms_ts = msg.get("smsTs", 0)
                    if sms_ts <= last_ts:
                        continue
                    direction = "sent" if msg.get("dir") == 1 else "received"
                    phone = msg.get("phNum", "")
                    content = msg.get("smsBd", "")
                    sms_time = datetime.fromtimestamp(sms_ts, tz=CST).strftime("%Y-%m-%d %H:%M:%S")
                    await db.execute(
                        """INSERT INTO sms (device_id, sim_slot, phone, content, direction, raw_json, sms_time, sms_ts)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (device_id, f"sim{slot_num}", phone, content, direction,
                         json.dumps(msg, ensure_ascii=False), sms_time, sms_ts)
                    )
                    # Get device name for notification
                    cur = await db.execute("SELECT name FROM devices WHERE id=?", (device_id,))
                    dev_row = await cur.fetchone()
                    dev_name = dev_row["name"] if dev_row else device_id
                    asyncio.create_task(notify_new_sms(dev_name, f"sim{slot_num}", phone, content, direction))
                await db.commit()
    except Exception as e:
        logger.error("poll_sms error for %s: %s", device_id, e, exc_info=True)


# ── Lifespan ────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    asyncio.create_task(sms_polling_loop())
    logger.info("LvYou Panel started")
    yield
    # Cleanup: close global HTTP session
    global _http_session
    if _http_session and not _http_session.closed:
        await _http_session.close()
        _http_session = None
    logger.info("LvYou Panel stopped")


app = FastAPI(title="Lawnet Device Panel", version="1.0", lifespan=lifespan)


# ── API Key Auth Middleware ─────────────────────────────────────
async def _get_api_key():
    """Read stored API key from config."""
    try:
        async with db_connection() as db:
            cur = await db.execute("SELECT value FROM config WHERE key='api_key'")
            row = await cur.fetchone()
        return row["value"] if row else ""
    except Exception:
        return ""


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Protect /api/* endpoints with X-API-Key header."""
    path = request.url.path
    # Skip auth for: static files, HTML pages, webhook, auth endpoint
    if (
        not path.startswith("/api/")
        or path == "/api/auth"
        or path == "/webhook"
        or path.startswith("/static/")
    ):
        return await call_next(request)

    stored_key = await _get_api_key()
    # If no API key is configured, skip auth (initial setup)
    if not stored_key:
        return await call_next(request)

    provided = request.headers.get("X-API-Key", "")
    if not provided:
        provided = request.query_params.get("api_key", "")
    if provided != stored_key:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)


@app.post("/api/auth")
async def api_auth(data: dict):
    """Validate API key. Returns {ok: true} if valid."""
    stored_key = await _get_api_key()
    if not stored_key:
        return {"ok": True, "needsSetup": True}
    provided = data.get("api_key", "")
    if provided != stored_key:
        raise HTTPException(401, "Invalid API key")
    return {"ok": True}

# Serve static files
_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")


# ── SMS Polling Loop ───────────────────────────────────────────
async def sms_polling_loop():
    """Periodically poll all devices for new SMS."""
    await asyncio.sleep(30)  # Wait for device init
    while True:
        try:
            async with db_connection() as db:
                cur = await db.execute("SELECT id, ip, token FROM devices")
                devices = [dict(r) for r in (await cur.fetchall())]
            for dev in devices:
                try:
                    await poll_sms(dev["id"], dev["ip"], dev["token"])
                except Exception:
                    pass
        except Exception:
            pass
        await asyncio.sleep(60)  # Poll every 60 seconds


# ── DingTalk Notification ───────────────────────────────────────
async def send_dingtalk(msg: str):
    """Send notification to configured DingTalk webhook with signature."""
    webhook_url = None
    secret = None
    try:
        async with db_connection() as db:
            cur = await db.execute("SELECT value FROM config WHERE key='dingtalk_webhook'")
            row = await cur.fetchone()
            cur2 = await db.execute("SELECT value FROM config WHERE key='dingtalk_secret'")
            row2 = await cur2.fetchone()
        if not row or not row["value"]:
            return False
        webhook_url = row["value"]
        secret = row2["value"] if row2 else ""
    except Exception:
        return False
    try:
        signed_url = _sign_dingtalk_webhook(webhook_url, secret)
        session = await get_http_session()
        payload = {"msgtype": "text", "text": {"content": msg}}
        async with session.post(signed_url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            return resp.status == 200
    except Exception:
        return False


async def notify_new_sms(dev_name: str, slot: str, phone: str, content: str, direction: str):
    """Push SMS notification via DingTalk."""
    emoji = "📩" if direction == "received" else "📤"
    dir_label = "收到" if direction == "received" else "发出"
    msg = f"{emoji} {dev_name} {dir_label}短信\n卡槽：{slot}\n号码：{phone}\n内容：{content}"
    await send_dingtalk(msg)


# ── HTML Pages ──────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    return _page("dashboard", "控制面板")


@app.get("/sms", response_class=HTMLResponse)
async def sms_page():
    return _page("sms", "短信记录")


@app.get("/devices", response_class=HTMLResponse)
async def devices_page():
    return _page("devices", "设备管理")


@app.get("/logs", response_class=HTMLResponse)
async def logs_page():
    return _page("logs", "通话记录")





# ── API: Devices ────────────────────────────────────────────────
@app.get("/api/devices")
async def api_devices():
    async with db_connection() as db:
        cur = await db.execute("SELECT * FROM devices ORDER BY created_at DESC")
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


@app.post("/api/devices")
async def api_add_device(data: dict):
    name = data.get("name", "").strip()
    ip = data.get("ip", "").strip()
    password = data.get("password", "").strip()
    notes = data.get("notes", "").strip()

    if not ip:
        raise HTTPException(400, "IP 不能为空")
    if not password and not data.get("token"):
        raise HTTPException(400, "密码或 token 不能为空")

    token = data.get("token") or calc_token(password)

    # Test connection
    res = await device_call(ip, token, "ping", timeout=5)
    if res.get("code") != 0:
        raise HTTPException(400, f"设备 {ip} 连接失败: {res.get('note','')}")

    dev_id = res.get("devId", ip.replace(".", "-"))

    if not name:
        name = f"Device-{ip.split('.')[-1]}"

    async with db_connection() as db:
        await db.execute(
            "INSERT OR REPLACE INTO devices (id, name, ip, token, notes) VALUES (?,?,?,?,?)",
            (dev_id, name, ip, token, notes)
        )
        await db.commit()
    return {"ok": True, "dev_id": dev_id}


@app.delete("/api/devices/{device_id}")
async def api_delete_device(device_id: str):
    async with db_connection() as db:
        await db.execute("DELETE FROM devices WHERE id=?", (device_id,))
        await db.execute("DELETE FROM sms WHERE device_id=?", (device_id,))
        await db.execute("DELETE FROM call_logs WHERE device_id=?", (device_id,))
        await db.commit()
    return {"ok": True}


@app.put("/api/devices/{device_id}")
async def api_update_device(device_id: str, data: dict):
    allowed = ("name", "ip", "token", "notes")
    sets = []
    vals = []
    for k in allowed:
        if k in data:
            sets.append(f"{k}=?")
            vals.append(data[k])
    if sets:
        vals.append(device_id)
        async with db_connection() as db:
            await db.execute(f"UPDATE devices SET {', '.join(sets)} WHERE id=?", vals)
            await db.commit()
    return {"ok": True}


# ── API: Webhook ────────────────────────────────────────────────
@app.post("/webhook")
async def api_webhook(request: Request):
    """Receive push messages from devices (SMS, call, etc.)"""
    try:
        ct = request.headers.get("content-type", "")
        if "application/json" in ct:
            data = await request.json()
        else:
            body = await request.body()
            from urllib.parse import unquote
            decoded = unquote(body.decode("utf-8"))
            data = dict(parse_qsl(decoded))
    except Exception:
        return {"status": "bad_request"}

    msg_type = int(data.get("type", 0))
    dev_id = data.get("devId", "")

    if msg_type == 501:  # New SMS received
        slot = f"sim{data.get('slot', 1)}"
        phone = data.get("phNum", "")
        content = data.get("smsBd", "")
        sms_ts = int(data.get("smsTs", 0)) // 1000  # ms -> seconds
        if not sms_ts:
            sms_ts = int(data.get("msgTs", 0))
        sms_time = datetime.fromtimestamp(sms_ts, tz=CST).strftime("%Y-%m-%d %H:%M:%S")

        async with db_connection() as db:
            await db.execute(
                """INSERT OR IGNORE INTO sms (device_id, sim_slot, phone, content, direction, raw_json, sms_time, sms_ts)
                   VALUES (?, ?, ?, ?, 'received', ?, ?, ?)""",
                (dev_id, slot, phone, content, json.dumps(data, ensure_ascii=False), sms_time, sms_ts)
            )
            await db.commit()
            # Get device name for notification
            cur = await db.execute("SELECT name FROM devices WHERE id=?", (dev_id,))
            dev_row = await cur.fetchone()
            dev_name = dev_row["name"] if dev_row else dev_id
        asyncio.create_task(notify_new_sms(dev_name, slot, phone, content, "received"))

    elif msg_type == 502:  # Sent SMS success
        slot = f"sim{data.get('slot', 1)}"
        phone = data.get("phNum", "")
        content = data.get("smsBd", "")
        sms_ts = int(data.get("devSmsTs", data.get("msgTs", 0)))
        sms_time = datetime.fromtimestamp(sms_ts, tz=CST).strftime("%Y-%m-%d %H:%M:%S")

        async with db_connection() as db:
            await db.execute(
                """INSERT OR IGNORE INTO sms (device_id, sim_slot, phone, content, direction, raw_json, sms_time, sms_ts)
                   VALUES (?, ?, ?, ?, 'sent', ?, ?, ?)""",
                (dev_id, slot, phone, content, json.dumps(data, ensure_ascii=False), sms_time, sms_ts)
            )
            await db.commit()
            cur = await db.execute("SELECT name FROM devices WHERE id=?", (dev_id,))
            dev_row = await cur.fetchone()
            dev_name = dev_row["name"] if dev_row else dev_id
        asyncio.create_task(notify_new_sms(dev_name, slot, phone, content, "sent"))

    return {"status": "ok"}


# ── API: Device Status ──────────────────────────────────────────
@app.get("/api/devices/{device_id}/status")
async def api_device_status(device_id: str):
    async with db_connection() as db:
        cur = await db.execute("SELECT * FROM devices WHERE id=?", (device_id,))
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "设备不存在")

    dev = dict(row)
    res = await device_call(dev["ip"], dev["token"], "stat", timeout=10)
    return {"device": dev, "status": res}


@app.get("/api/devices/status/all")
async def api_all_status():
    async with db_connection() as db:
        cur = await db.execute("SELECT * FROM devices ORDER BY name")
        rows = await cur.fetchall()
        devices = [dict(r) for r in rows]

    async def fetch_status(dev):
        try:
            res = await device_call(dev["ip"], dev["token"], "stat", timeout=3)
        except Exception:
            res = {"code": -1, "note": "offline"}
        return {"device": dev, "status": res}

    results = await asyncio.gather(*[fetch_status(d) for d in devices])
    return results


# ── API: Commands ───────────────────────────────────────────────
@app.post("/api/devices/{device_id}/cmd/{cmd}")
async def api_device_cmd(device_id: str, cmd: str, data: dict = None):
    async with db_connection() as db:
        cur = await db.execute("SELECT * FROM devices WHERE id=?", (device_id,))
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "设备不存在")

    dev = dict(row)
    params = data or {}

    # If sending SMS, also cache it
    if cmd == "sendsms":
        slot_num = str(params.get("p1", "1"))
        phone = params.get("p2", "")
        content = params.get("p3", "")
        res = await device_call(dev["ip"], dev["token"], "sendsms", {"p1": slot_num, "p2": phone, "p3": content})
        if res.get("code") == 0:
            async with db_connection() as db:
                await db.execute(
                    "INSERT INTO sms (device_id, sim_slot, phone, content, direction, sms_time, sms_ts) VALUES (?,?,?,?,?,datetime('now','localtime'),strftime('%s','now'))",
                    (device_id, f"sim{slot_num}", phone, content, "sent")
                )
                await db.commit()
                cur = await db.execute("SELECT name FROM devices WHERE id=?", (device_id,))
                dev_row = await cur.fetchone()
                dev_name = dev_row["name"] if dev_row else device_id
            asyncio.create_task(notify_new_sms(dev_name, f"sim{slot_num}", phone, content, "sent"))
        return res

    # Generic command
    res = await device_call(dev["ip"], dev["token"], cmd, params)
    return res


# ── API: SMS ────────────────────────────────────────────────────
@app.get("/api/sms")
async def api_sms(
    device_id: str = Query(None),
    phone: str = Query(None),
    keyword: str = Query(None),
    direction: str = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=200),
):
    where = ["1=1"]
    qparams = []

    if device_id:
        where.append("device_id=?")
        qparams.append(device_id)
    if phone:
        where.append("phone LIKE ?")
        qparams.append(f"%{phone}%")
    if keyword:
        where.append("content LIKE ?")
        qparams.append(f"%{keyword}%")
    if direction:
        where.append("direction=?")
        qparams.append(direction)

    where_sql = " AND ".join(where)
    offset = (page - 1) * per_page

    async with db_connection() as db:
        cur = await db.execute(f"SELECT COUNT(*) FROM sms WHERE {where_sql}", qparams)
        total = (await cur.fetchone())[0]

        cur = await db.execute(
            f"""SELECT s.*, d.name as device_name, d.ip as device_ip
               FROM sms s LEFT JOIN devices d ON s.device_id=d.id
               WHERE {where_sql}
               ORDER BY s.sms_time DESC
               LIMIT ? OFFSET ?""",
            qparams + [per_page, offset]
        )
        rows = await cur.fetchall()

    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "items": [dict(r) for r in rows],
    }


@app.post("/api/sms/refresh/{device_id}")
async def api_refresh_sms(device_id: str):
    """Manually refresh SMS cache from device."""
    async with db_connection() as db:
        cur = await db.execute("SELECT * FROM devices WHERE id=?", (device_id,))
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "设备不存在")

    dev = dict(row)
    await poll_sms(device_id, dev["ip"], dev["token"])
    return {"ok": True}


# ── API: Settings ──────────────────────────────────────────────
@app.get("/api/settings")
async def api_get_settings():
    async with db_connection() as db:
        cur = await db.execute("SELECT key, value FROM config")
        rows = await cur.fetchall()
    return {r["key"]: r["value"] for r in rows}


@app.post("/api/settings")
async def api_save_settings(data: dict):
    async with db_connection() as db:
        for key, value in data.items():
            await db.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, str(value)))
        await db.commit()
    return {"ok": True}


@app.post("/api/test-dingtalk")
async def api_test_dingtalk():
    """Test DingTalk webhook via backend proxy (avoids CORS)."""
    webhook_url = None
    secret = None
    async with db_connection() as db:
        cur = await db.execute("SELECT value FROM config WHERE key='dingtalk_webhook'")
        row = await cur.fetchone()
        cur2 = await db.execute("SELECT value FROM config WHERE key='dingtalk_secret'")
        row2 = await cur2.fetchone()
    if not row or not row["value"]:
        raise HTTPException(400, "未配置钉钉 Webhook")
    webhook_url = row["value"]
    secret = row2["value"] if row2 else ""
    try:
        signed_url = _sign_dingtalk_webhook(webhook_url, secret)
        session = await get_http_session()
        payload = {"msgtype": "text", "text": {"content": "✅ LvYou Panel 钉钉推送测试成功！"}}
        async with session.post(signed_url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            result = await resp.text()
            return {"ok": resp.status == 200, "status": resp.status, "body": result}
    except Exception as e:
        return {"ok": False, "status": 0, "body": str(e)}


# ── API: Summary Stats ──────────────────────────────────────────
@app.get("/api/stats")
async def api_stats():
    async with db_connection() as db:
        cur = await db.execute("""
            SELECT
                (SELECT COUNT(*) FROM devices),
                (SELECT COUNT(*) FROM sms),
                (SELECT COUNT(*) FROM sms WHERE direction='received'),
                (SELECT COUNT(*) FROM sms WHERE direction='sent'),
                (SELECT COUNT(*) FROM call_logs)
        """)
        row = await cur.fetchone()
    return {
        "devices": row[0],
        "sms_total": row[1],
        "sms_received": row[2],
        "sms_sent": row[3],
        "call_total": row[4],
    }


# ── API: Call Logs ──────────────────────────────────────────────
@app.get("/api/logs")
async def api_logs(
    device_id: str = Query(None),
    phone: str = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=200),
):
    where = ["1=1"]
    qparams = []
    if device_id:
        where.append("device_id=?")
        qparams.append(device_id)
    if phone:
        where.append("phone LIKE ?")
        qparams.append(f"%{phone}%")

    where_sql = " AND ".join(where)
    offset = (page - 1) * per_page

    async with db_connection() as db:
        cur = await db.execute(f"SELECT COUNT(*) FROM call_logs WHERE {where_sql}", qparams)
        total = (await cur.fetchone())[0]
        cur = await db.execute(
            f"""SELECT c.*, d.name as device_name
               FROM call_logs c LEFT JOIN devices d ON c.device_id=d.id
               WHERE {where_sql} ORDER BY c.call_time DESC LIMIT ? OFFSET ?""",
            qparams + [per_page, offset]
        )
        rows = await cur.fetchall()
    return {"total": total, "page": page, "per_page": per_page, "items": [dict(r) for r in rows]}


# ══════════════════════════════════════════════════════════════════
#  FRONTEND (template referencing external static files)
# ══════════════════════════════════════════════════════════════════

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__ - 设备群控</title>
<link rel="stylesheet" href="/static/style.css">
</head>
<body>

<nav>
<span class="logo">📡 设备群控</span>
<a href="/" class="__NAV_DASH__">控制面板</a>
<a href="/sms" class="__NAV_SMS__">短信记录</a>
<a href="/logs" class="__NAV_LOG__">通话记录</a>
<a href="/devices" class="__NAV_DEV__">设备管理</a>
</nav>

<main>
<div id="page-dashboard" class="__PAGE_DASH__">
  <div class="refresh-bar"><span>最后刷新：</span><span id="lastRefresh">-</span>
    <button class="btn btn-sm" onclick="refreshAll()" id="refreshBtn">🔄 刷新</button>
  </div>
  <div class="stats-grid" id="statsCards"></div>
  <div class="card" style="margin-bottom:16px;overflow:hidden">
    <div style="display:flex;justify-content:space-between;align-items:center;cursor:pointer" onclick="document.getElementById('settingsBody').classList.toggle('mr');var s=document.getElementById('settingsBody').classList.contains('mr')?'▶ 展开设置':'▼ 收起设置';document.getElementById('settingsHint').textContent=s">
      <h2 style="margin:0">⚙️ 设置 <span id="dingtalkStatus" style="font-size:11px;font-weight:400"></span></h2>
      <span style="font-size:11px;color:var(--sub)" id="settingsHint">▶ 展开设置</span>
    </div>
    <div id="settingsBody" class="mr" style="margin-top:10px">
      <div style="margin-bottom:10px;padding-bottom:10px;border-bottom:1px solid var(--border)">
        <div style="font-size:13px;font-weight:600;margin-bottom:5px">🔔 钉钉推送</div>
        <div style="display:flex;gap:6px;flex-wrap:wrap">
          <input id="dingtalkWebhook" placeholder="Webhook 地址" style="flex:2;min-width:160px;padding:8px;border:1px solid var(--border);border-radius:8px;font-size:12px">
          <input id="dingtalkSecret" placeholder="加签密钥(可选)" style="flex:1;min-width:120px;padding:8px;border:1px solid var(--border);border-radius:8px;font-size:12px">
          <button class="btn btn-p btn-sm" onclick="saveDingtalk()">保存</button>
          <button class="btn btn-sm" onclick="testDingtalk()">测试</button>
        </div>
      </div>
      <div style="margin-bottom:10px;padding-bottom:10px;border-bottom:1px solid var(--border)">
        <div style="font-size:13px;font-weight:600;margin-bottom:5px">🔑 API 密钥</div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
          <input id="apiKeyInput" type="password" placeholder="留空则关闭认证" style="flex:2;min-width:160px;padding:8px;border:1px solid var(--border);border-radius:8px;font-size:12px">
          <button class="btn btn-p btn-sm" onclick="saveApiKey()">保存</button>
          <span id="apiKeyStatus" style="font-size:11px;color:var(--sub)"></span>
        </div>
      </div>
      <div>
        <div style="font-size:13px;font-weight:600;margin-bottom:5px">➕ 添加设备</div>
        <form onsubmit="quickAddDevice(event)">
          <div style="display:flex;gap:6px;flex-wrap:wrap">
            <input id="qaName" placeholder="名称" style="flex:1;min-width:80px;padding:8px;border:1px solid var(--border);border-radius:8px;font-size:12px">
            <input id="qaIP" placeholder="IP *" required style="flex:1;min-width:100px;padding:8px;border:1px solid var(--border);border-radius:8px;font-size:12px">
            <input id="qaPwd" type="password" placeholder="密码 *" required style="flex:1;min-width:80px;padding:8px;border:1px solid var(--border);border-radius:8px;font-size:12px">
            <button class="btn btn-p btn-sm" type="submit">添加</button>
          </div>
        </form>
      </div>
    </div>
  </div>
  <div class="device-grid" id="deviceGrid"></div>
</div>

<div id="page-sms" class="__PAGE_SMS__">
  <h2 style="margin-bottom:16px">📩 短信记录</h2>
  <div class="search-bar">
    <select id="smsDevFilter" onchange="loadSMS()"><option value="">全部设备</option></select>
    <input id="smsPhoneFilter" placeholder="手机号" oninput="loadSMS()">
    <input id="smsKwFilter" placeholder="内容关键词" oninput="loadSMS()">
    <select id="smsDirFilter" onchange="loadSMS()"><option value="">全部方向</option><option value="received">接收</option><option value="sent">发送</option></select>
    <button class="btn btn-p" onclick="loadSMS()">搜索</button>
  </div>
  <div style="overflow-x:auto"><table class="tbl"><thead><tr>
    <th>时间</th><th>设备</th><th>卡槽</th><th>方向</th><th>号码</th><th>内容</th>
  </tr></thead><tbody id="smsTableBody"></tbody></table></div>
  <div class="pg" id="smsPagination"></div>
</div>

<div id="page-logs" class="__PAGE_LOG__">
  <h2 style="margin-bottom:16px">📞 通话记录</h2>
  <div class="search-bar">
    <select id="logDevFilter" onchange="loadLogs()"><option value="">全部设备</option></select>
    <input id="logPhoneFilter" placeholder="手机号" oninput="loadLogs()">
    <button class="btn btn-p" onclick="loadLogs()">搜索</button>
  </div>
  <div style="overflow-x:auto"><table class="tbl"><thead><tr>
    <th>时间</th><th>设备</th><th>方向</th><th>号码</th><th>操作</th><th>时长</th>
  </tr></thead><tbody id="logTableBody"></tbody></table></div>
  <div class="pg" id="logPagination"></div>
</div>

<div id="page-devices" class="__PAGE_DEV__">
  <h2 style="margin-bottom:16px">⚙️ 设备管理</h2>
  <div class="card">
    <h2>添加设备</h2>
    <form id="addForm" onsubmit="addDevice(event)">
      <div class="form-row">
        <div class="form-group"><label>名称</label><input id="addName" placeholder="可选"></div>
        <div class="form-group"><label>IP 地址 *</label><input id="addIP" placeholder="192.168.x.x" required></div>
        <div class="form-group"><label>管理员密码 *</label><input id="addPwd" type="password" placeholder="管理员密码" required></div>
      </div>
      <button class="btn btn-p" type="submit">+ 添加设备</button>
    </form>
  </div>
  <div style="overflow-x:auto"><table class="tbl"><thead><tr>
    <th>ID</th><th>名称</th><th>IP</th><th>备注</th><th>添加时间</th><th>操作</th>
  </tr></thead><tbody id="devTableBody"></tbody></table></div>
</div>
</main>

<div class="toast" id="toast"></div>

<script src="/static/app.js"></script>
</body>
</html>"""


def _page(page_id: str, title: str) -> str:
    active = lambda p: "active" if p == page_id else ""
    show = lambda p: "" if p == page_id else "mr"
    return (HTML_TEMPLATE
        .replace("__TITLE__", title)
        .replace("__NAV_DASH__", active("dashboard"))
        .replace("__NAV_SMS__", active("sms"))
        .replace("__NAV_LOG__", active("logs"))
        .replace("__NAV_DEV__", active("devices"))
        .replace("__PAGE_DASH__", show("dashboard"))
        .replace("__PAGE_SMS__", show("sms"))
        .replace("__PAGE_LOG__", show("logs"))
        .replace("__PAGE_DEV__", show("devices"))
    )


# ── Entry ──────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=34567)

