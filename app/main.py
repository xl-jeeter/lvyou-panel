"""Lawnet Device Panel - 绿微设备群控管理系统"""

import asyncio
import hashlib
import json
import time
from datetime import datetime
from contextlib import asynccontextmanager

import aiohttp
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from urllib.parse import parse_qsl

from app.database import init_db, get_db

# ── Token calc ──────────────────────────────────────────────────
def calc_token(password: str) -> str:
    return hashlib.md5(f"admin|{password}".encode()).hexdigest()


# ── Device communication ────────────────────────────────────────
async def device_call(ip: str, token: str, cmd: str, params: dict = None, timeout: int = 10):
    """Send command to a device via HTTP and return parsed JSON."""
    url = f"http://{ip}/ctrl"
    req_params = {"token": token, "cmd": cmd}
    if params:
        req_params.update(params)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=req_params, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                return await resp.json()
    except asyncio.TimeoutError:
        return {"code": -1, "note": "Timeout"}
    except aiohttp.ClientError as e:
        return {"code": -1, "note": str(e)}
    except Exception as e:
        return {"code": -1, "note": str(e)}


def translate_slot(slot: str) -> str:
    return "sim1" if slot in ("1", "sim1") else "sim2"


# ── SMS polling & caching ────────────────────────────────────────
async def poll_sms(device_id: str, ip: str, token: str):
    """Fetch new SMS from device and cache locally."""
    db = await get_db()
    try:
        for slot_num in ("1", "2"):
            slot_name = f"sim{slot_num}"
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
                sms_time = datetime.utcfromtimestamp(sms_ts + 8*3600).strftime("%Y-%m-%d %H:%M:%S")
                await db.execute(
                    """INSERT INTO sms (device_id, sim_slot, phone, content, direction, raw_json, sms_time, sms_ts)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (device_id, f"sim{slot_num}", phone, content, direction,
                     json.dumps(msg, ensure_ascii=False), sms_time, sms_ts)
                )
            await db.commit()
    except Exception as e:
        import traceback
        print(f"poll_sms error for {device_id}: {e}")
        traceback.print_exc()
    finally:
        await db.close()


# ── Lifespan ────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="Lawnet Device Panel", version="1.0", lifespan=lifespan)

# ── Background SMS Polling ─────────────────────────────────────
async def sms_polling_loop():
    """Periodically poll all devices for new SMS."""
    await asyncio.sleep(30)  # Wait for device init
    while True:
        try:
            db = await get_db()
            cur = await db.execute("SELECT id, ip, token FROM devices")
            devices = [dict(r) for r in (await cur.fetchall())]
            await db.close()
            for dev in devices:
                try:
                    await poll_sms(dev["id"], dev["ip"], dev["token"])
                except Exception:
                    pass
        except Exception:
            pass
        await asyncio.sleep(60)  # Poll every 60 seconds


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    asyncio.create_task(sms_polling_loop())
    yield

# Serve static files
import os
_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")


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


def _page(page_id: str, title: str) -> str:
    active = lambda p: "active" if p == page_id else ""
    show = lambda p: "" if p == page_id else "mr"
    css_str = CSS.replace("{C}", TCOLOR)
    return (HTML
        .replace("__TITLE__", title)
        .replace("__NAV_DASH__", active("dashboard"))
        .replace("__NAV_SMS__", active("sms"))
        .replace("__NAV_LOG__", active("logs"))
        .replace("__NAV_DEV__", active("devices"))
        .replace("__PAGE_DASH__", show("dashboard"))
        .replace("__PAGE_SMS__", show("sms"))
        .replace("__PAGE_LOG__", show("logs"))
        .replace("__PAGE_DEV__", show("devices"))
        .replace("__CSS__", css_str)
        .replace("__JS__", JS)
    )


# ── API: Devices ────────────────────────────────────────────────
@app.get("/api/devices")
async def api_devices():
    db = await get_db()
    cur = await db.execute("SELECT * FROM devices ORDER BY created_at DESC")
    rows = await cur.fetchall()
    await db.close()
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

    db = await get_db()
    await db.execute(
        "INSERT OR REPLACE INTO devices (id, name, ip, token, notes) VALUES (?,?,?,?,?)",
        (dev_id, name, ip, token, notes)
    )
    await db.commit()
    await db.close()
    return {"ok": True, "dev_id": dev_id}


@app.delete("/api/devices/{device_id}")
async def api_delete_device(device_id: str):
    db = await get_db()
    await db.execute("DELETE FROM devices WHERE id=?", (device_id,))
    await db.execute("DELETE FROM sms WHERE device_id=?", (device_id,))
    await db.execute("DELETE FROM call_logs WHERE device_id=?", (device_id,))
    await db.commit()
    await db.close()
    return {"ok": True}


@app.put("/api/devices/{device_id}")
async def api_update_device(device_id: str, data: dict):
    db = await get_db()
    sets = []
    vals = []
    for k in ("name", "ip", "token", "notes"):
        if k in data:
            sets.append(f"{k}=?")
            vals.append(data[k])
    if sets:
        vals.append(device_id)
        await db.execute(f"UPDATE devices SET {', '.join(sets)} WHERE id=?", vals)
        await db.commit()
    await db.close()
    return {"ok": True}


# ── API: Device Status ──────────────────────────────────────────
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
        sms_time = datetime.utcfromtimestamp(sms_ts + 8*3600).strftime("%Y-%m-%d %H:%M:%S")

        db = await get_db()
        await db.execute(
            """INSERT OR IGNORE INTO sms (device_id, sim_slot, phone, content, direction, raw_json, sms_time, sms_ts)
               VALUES (?, ?, ?, ?, 'received', ?, ?, ?)""",
            (dev_id, slot, phone, content, json.dumps(data, ensure_ascii=False), sms_time, sms_ts)
        )
        await db.commit()
        await db.close()

    elif msg_type == 502:  # Sent SMS success
        slot = f"sim{data.get('slot', 1)}"
        phone = data.get("phNum", "")
        content = data.get("smsBd", "")
        sms_ts = int(data.get("devSmsTs", data.get("msgTs", 0)))
        sms_time = datetime.utcfromtimestamp(sms_ts + 8*3600).strftime("%Y-%m-%d %H:%M:%S")

        db = await get_db()
        await db.execute(
            """INSERT OR IGNORE INTO sms (device_id, sim_slot, phone, content, direction, raw_json, sms_time, sms_ts)
               VALUES (?, ?, ?, ?, 'sent', ?, ?, ?)""",
            (dev_id, slot, phone, content, json.dumps(data, ensure_ascii=False), sms_time, sms_ts)
        )
        await db.commit()
        await db.close()

    return {"status": "ok"}


@app.get("/api/devices/{device_id}/status")
async def api_device_status(device_id: str):
    db = await get_db()
    cur = await db.execute("SELECT * FROM devices WHERE id=?", (device_id,))
    row = await cur.fetchone()
    await db.close()
    if not row:
        raise HTTPException(404, "设备不存在")

    dev = dict(row)
    res = await device_call(dev["ip"], dev["token"], "stat", timeout=10)
    return {"device": dev, "status": res}


@app.get("/api/devices/status/all")
async def api_all_status():
    db = await get_db()
    cur = await db.execute("SELECT * FROM devices ORDER BY name")
    rows = await cur.fetchall()
    await db.close()

    results = []
    for row in rows:
        dev = dict(row)
        res = await device_call(dev["ip"], dev["token"], "stat", timeout=8)
        results.append({"device": dev, "status": res})
    return results


# ── API: Commands ───────────────────────────────────────────────
@app.post("/api/devices/{device_id}/cmd/{cmd}")
async def api_device_cmd(device_id: str, cmd: str, data: dict = None):
    db = await get_db()
    cur = await db.execute("SELECT * FROM devices WHERE id=?", (device_id,))
    row = await cur.fetchone()
    await db.close()
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
            db2 = await get_db()
            await db2.execute(
                "INSERT INTO sms (device_id, sim_slot, phone, content, direction, sms_time, sms_ts) VALUES (?,?,?,?,?,datetime('now','localtime'),strftime('%s','now'))",
                (device_id, f"sim{slot_num}", phone, content, "sent")
            )
            await db2.commit()
            await db2.close()
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
    db = await get_db()
    where = ["1=1"]
    params = []

    if device_id:
        where.append("device_id=?")
        params.append(device_id)
    if phone:
        where.append("phone LIKE ?")
        params.append(f"%{phone}%")
    if keyword:
        where.append("content LIKE ?")
        params.append(f"%{keyword}%")
    if direction:
        where.append("direction=?")
        params.append(direction)

    where_sql = " AND ".join(where)
    offset = (page - 1) * per_page

    cur = await db.execute(
        f"SELECT COUNT(*) FROM sms WHERE {where_sql}", params
    )
    total = (await cur.fetchone())[0]

    cur = await db.execute(
        f"""SELECT s.*, d.name as device_name, d.ip as device_ip
           FROM sms s LEFT JOIN devices d ON s.device_id=d.id
           WHERE {where_sql}
           ORDER BY s.sms_time DESC
           LIMIT ? OFFSET ?""",
        params + [per_page, offset]
    )
    rows = await cur.fetchall()
    await db.close()

    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "items": [dict(r) for r in rows],
    }


@app.post("/api/sms/refresh/{device_id}")
async def api_refresh_sms(device_id: str):
    """Manually refresh SMS cache from device."""
    db = await get_db()
    cur = await db.execute("SELECT * FROM devices WHERE id=?", (device_id,))
    row = await cur.fetchone()
    await db.close()
    if not row:
        raise HTTPException(404, "设备不存在")

    dev = dict(row)
    await poll_sms(device_id, dev["ip"], dev["token"])
    return {"ok": True}


# ── API: Summary Stats ──────────────────────────────────────────
@app.get("/api/stats")
async def api_stats():
    db = await get_db()
    cur = await db.execute("SELECT COUNT(*) FROM devices")
    device_count = (await cur.fetchone())[0]
    cur = await db.execute("SELECT COUNT(*) FROM sms")
    sms_total = (await cur.fetchone())[0]
    cur = await db.execute("SELECT COUNT(*) FROM sms WHERE direction='received'")
    sms_recv = (await cur.fetchone())[0]
    cur = await db.execute("SELECT COUNT(*) FROM sms WHERE direction='sent'")
    sms_sent = (await cur.fetchone())[0]
    cur = await db.execute("SELECT COUNT(*) FROM call_logs")
    call_total = (await cur.fetchone())[0]
    await db.close()
    return {
        "devices": device_count,
        "sms_total": sms_total,
        "sms_received": sms_recv,
        "sms_sent": sms_sent,
        "call_total": call_total,
    }


# ── API: Call Logs ──────────────────────────────────────────────
@app.get("/api/logs")
async def api_logs(
    device_id: str = Query(None),
    phone: str = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=200),
):
    db = await get_db()
    where = ["1=1"]
    params = []
    if device_id:
        where.append("device_id=?")
        params.append(device_id)
    if phone:
        where.append("phone LIKE ?")
        params.append(f"%{phone}%")

    where_sql = " AND ".join(where)
    offset = (page - 1) * per_page

    cur = await db.execute(f"SELECT COUNT(*) FROM call_logs WHERE {where_sql}", params)
    total = (await cur.fetchone())[0]
    cur = await db.execute(
        f"""SELECT c.*, d.name as device_name
           FROM call_logs c LEFT JOIN devices d ON c.device_id=d.id
           WHERE {where_sql} ORDER BY c.call_time DESC LIMIT ? OFFSET ?""",
        params + [per_page, offset]
    )
    rows = await cur.fetchall()
    await db.close()
    return {"total": total, "page": page, "per_page": per_page, "items": [dict(r) for r in rows]}


# ══════════════════════════════════════════════════════════════════
#  FRONTEND (embedded single-file SPA)
# ══════════════════════════════════════════════════════════════════

TCOLOR = "#10b981"

CSS = r"""
:root {
  --c: {C}; --bg: #f8fafc; --card: #fff; --text: #1e293b;
  --sub: #64748b; --border: #e2e8f0; --danger: #ef4444; --warn: #f59e0b;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);line-height:1.6;min-height:100vh}
/* nav */
nav{background:var(--card);border-bottom:1px solid var(--border);padding:0 24px;display:flex;align-items:center;height:56px;gap:4px}
nav .logo{font-size:18px;font-weight:700;color:var(--c);margin-right:24px;white-space:nowrap}
nav a{text-decoration:none;color:var(--sub);padding:8px 16px;border-radius:8px;font-size:14px;font-weight:500;transition:all .2s}
nav a:hover,nav a.active{color:var(--c);background:rgba(16,185,129,0.08)}
/* layout */
main{max-width:1200px;margin:0 auto;padding:20px 24px}
/* cards */
.card{background:var(--card);border-radius:12px;border:1px solid var(--border);padding:20px;margin-bottom:16px}
.card h2{font-size:16px;margin-bottom:12px;color:var(--text)}
.stats-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px}
.stat-card{background:var(--card);border-radius:10px;border:1px solid var(--border);padding:16px;text-align:center}
.stat-card .num{font-size:28px;font-weight:700;color:var(--c)}
.stat-card .label{font-size:12px;color:var(--sub);margin-top:4px}
/* device grid */
.device-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px}
.device-card{background:var(--card);border-radius:12px;border:1px solid var(--border);overflow:hidden;transition:box-shadow .2s}
.device-card:hover{box-shadow:0 4px 12px rgba(0,0,0,0.06)}
.device-card .head{padding:14px 16px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--border)}
.device-card .head .name{font-weight:600;font-size:15px}
.device-card .head .ip{font-size:12px;color:var(--sub)}
.device-card .head .status-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.device-card .head .online{background:var(--c);box-shadow:0 0 6px var(--c)}
.device-card .head .offline{background:var(--danger)}
.device-card .body{padding:12px 16px}
.device-card .sim-row{display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--border);font-size:13px}
.device-card .sim-row:last-child{border:none}
.device-card .sim-name{font-weight:500}
.device-card .sim-status{color:var(--sub)}
.device-card .sim-phone{color:var(--c);font-size:12px}
.device-card .sim-signal{display:flex;align-items:center;gap:4px;font-size:12px}
.device-card .sim-signal .bar{width:4px;background:var(--c);border-radius:2px}
.device-card .sim-signal .bar.gray{background:var(--border)}
.device-card .foot{display:flex;gap:8px;padding:0 16px 14px;flex-wrap:wrap}
/* buttons */
.btn{display:inline-flex;align-items:center;gap:4px;padding:6px 14px;border-radius:8px;font-size:13px;font-weight:500;border:1px solid var(--border);background:var(--card);color:var(--text);cursor:pointer;transition:all .2s}
.btn:hover{background:var(--bg)}
.btn-p{background:var(--c);color:#fff;border-color:var(--c)}
.btn-p:hover{opacity:.9}
.btn-r{background:var(--danger);color:#fff;border-color:var(--danger)}
.btn-r:hover{opacity:.9}
.btn-sm{padding:4px 10px;font-size:12px}
/* table */
.tbl{width:100%;border-collapse:collapse;font-size:13px}
.tbl th,.tbl td{padding:10px 12px;text-align:left;border-bottom:1px solid var(--border)}
.tbl th{font-weight:600;color:var(--sub);font-size:12px;text-transform:uppercase;letter-spacing:.5px;background:var(--bg);position:sticky;top:0}
.tbl td{white-space:nowrap}
.tbl td.content{max-width:300px;white-space:normal;word-break:break-all}
.tbl tr:hover td{background:#f1f5f9}
.badge{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600}
.badge-rec{background:#dcfce7;color:#166534}
.badge-sent{background:#dbeafe;color:#1e40af}
.badge-incoming{background:#fef3c7;color:#92400e}
.badge-outgoing{background:#ede9fe;color:#5b21b6}
/* forms */
.form-row{display:flex;gap:12px;margin-bottom:12px;flex-wrap:wrap}
.form-group{flex:1;min-width:140px}
.form-group label{display:block;font-size:12px;color:var(--sub);margin-bottom:4px;font-weight:500}
.form-group input,.form-group select{width:100%;padding:8px 12px;border:1px solid var(--border);border-radius:8px;font-size:14px;background:var(--card);color:var(--text);transition:border .2s}
.form-group input:focus,.form-group select:focus{outline:none;border-color:var(--c);box-shadow:0 0 0 3px rgba(16,185,129,0.1)}
/* search */
.search-bar{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap;align-items:center}
.search-bar input,.search-bar select{padding:8px 12px;border:1px solid var(--border);border-radius:8px;font-size:13px;background:var(--card)}
.search-bar input:focus,.search-bar select:focus{outline:none;border-color:var(--c)}
.search-bar input{min-width:180px}
/* pagination */
.pg{display:flex;gap:6px;align-items:center;justify-content:center;margin-top:16px}
.pg button{padding:6px 12px;border:1px solid var(--border);border-radius:6px;background:var(--card);cursor:pointer;font-size:13px}
.pg button:disabled{opacity:.4;cursor:default}
.pg span{font-size:13px;color:var(--sub)}
/* modal */
.modal-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.4);z-index:100;justify-content:center;align-items:center}
.modal-overlay.show{display:flex}
.modal-box{background:var(--card);border-radius:12px;padding:24px;width:90%;max-width:480px;max-height:80vh;overflow-y:auto}
.modal-box h3{font-size:18px;margin-bottom:16px}
.modal-box .actions{display:flex;gap:8px;justify-content:flex-end;margin-top:16px}
/* toast */
.toast{position:fixed;bottom:24px;right:24px;padding:12px 20px;border-radius:10px;font-size:14px;color:#fff;z-index:200;opacity:0;transition:opacity .3s;pointer-events:none}
.toast.show{opacity:1}
.toast.ok{background:var(--c)}
.toast.err{background:var(--danger)}
/* loading */
.spin{display:inline-block;width:20px;height:20px;border:2px solid var(--border);border-top-color:var(--c);border-radius:50%;animation:s .6s linear infinite}
@keyframes s{to{transform:rotate(360deg)}}
/* refresh bar */
.refresh-bar{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--sub);margin-bottom:12px}
.mr{display:none}
/* editable name */
.name-edit{cursor:pointer;border-bottom:2px dotted transparent;transition:all .2s}
.name-edit:hover{border-bottom-color:var(--c)}
.name-edit input{font-size:15px;font-weight:600;border:1px solid var(--c);border-radius:6px;padding:2px 8px;width:160px;outline:none}
/* responsive */
@media (max-width:768px) {
  nav{flex-wrap:wrap;height:auto;padding:8px 12px;gap:2px}
  nav .logo{width:100%;margin-bottom:4px}
  nav a{padding:6px 10px;font-size:13px}
  main{padding:12px 10px}
  .device-grid{grid-template-columns:1fr}
  .stats-grid{grid-template-columns:repeat(2,1fr)}
  .search-bar{flex-direction:column}
  .search-bar input{min-width:auto;width:100%}
  .form-row{flex-direction:column}
  .device-card .head{flex-direction:column;align-items:flex-start}
  .device-card .foot{gap:4px}
  .device-card .foot .btn{flex:1;justify-content:center;padding:8px 6px;font-size:12px}
  .tbl{font-size:11px}
  .tbl th,.tbl td{padding:6px 4px}
}
"""


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__ - 设备群控</title>
<style>__CSS__</style>
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
  <div class="card" style="margin-bottom:16px" id="quickAddCard">
    <div style="display:flex;justify-content:space-between;align-items:center;cursor:pointer" onclick="document.getElementById('quickAddForm').classList.toggle('mr')">
      <h2 style="margin:0">➕ 快速添加设备</h2><span style="color:var(--sub);font-size:12px">展开/收起</span>
    </div>
    <form id="quickAddForm" class="mr" onsubmit="quickAddDevice(event)" style="margin-top:12px">
      <div class="form-row">
        <div class="form-group"><label>名称</label><input id="qaName" placeholder="可选，如：客厅路由器"></div>
        <div class="form-group"><label>IP 地址 *</label><input id="qaIP" placeholder="192.168.x.x" required></div>
        <div class="form-group"><label>管理员密码 *</label><input id="qaPwd" type="password" placeholder="管理员密码" required></div>
      </div>
      <button class="btn btn-p" type="submit">+ 添加设备</button>
    </form>
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

<script>__JS__</script>
</body>
</html>"""


JS = r"""
const API = '/api';
let smsPage = 1, logPage = 1;

// ── Nav highlight ─────────────────────────────────────────────
(function(){
  const links = document.querySelectorAll('nav a');
  const path = location.pathname;
  links.forEach(a => {
    if (a.pathname === path) a.classList.add('active');
  });
})();

// ── Toast ─────────────────────────────────────────────────────
function toast(msg, ok=true) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show ' + (ok?'ok':'err');
  setTimeout(() => t.classList.remove('show'), 2500);
}

// ── Formatting ────────────────────────────────────────────────
function fmtTime(ts) { if(!ts) return '-'; return ts.replace('T',' ').substring(0,19); }
function fmtUptime(m) { if(!m&&m!==0) return '-'; m=parseInt(m); const d=Math.floor(m/1440),h=Math.floor((m%1440)/60),s=m%60; let r=''; if(d>0) r+=d+'天'; if(h>0||d>0) r+=h+'时'; r+=s+'分'; return r; }
function signalBars(dbm) {
  let v = parseInt(dbm)||0, bars='';
  for(let i=1;i<=5;i++) bars += `<span class="bar${i*20>v?' gray':''}" style="height:${i*4}px"></span>`;
  return bars;
}

// ══════════ Dashboard ══════════════════════════════════════════
async function refreshAll() {
  const btn = document.getElementById('refreshBtn');
  btn.disabled = true; btn.textContent = '⟳ 刷新中...';
  try {
    const r = await fetch(API+'/devices/status/all');
    const data = await r.json();
    renderDevices(data);
    document.getElementById('lastRefresh').textContent = new Date().toLocaleTimeString();
  } catch(e) { toast('刷新失败', false); }
  btn.disabled = false; btn.textContent = '🔄 刷新';
}

function renderDevices(data) {
  let online = 0, offline = 0;
  let html = '';
  data.forEach(d => {
    const s = d.status || {};
    const ok = s.code === 0;
    ok ? online++ : offline++;
    const wifi = s.wifi || {};
    const si = s.slotInfo || {};
    const simNet = s.simNet || [false, false];

    html += `<div class="device-card">
      <div class="head">
        <div>
          <div class="name"><span class="name-edit" onclick="editName(event,'${esc(d.device.id)}',&quot;${esc(d.device.name)}&quot;)" title="点击改名">${esc(d.device.name)}</span> <span style="font-weight:400;color:var(--sub);font-size:12px">${esc(d.device.ip)}</span></div>
          <div class="ip">${ok ? wifi.ssid||'-' : '离线'} · ${ok ? s.hwVer||'' : ''} · 运行${fmtUptime(s.uptime)}</div>
        </div>
        <span class="status-dot ${ok?'online':'offline'}"></span>
      </div>
      <div class="body">`;

    // SIM slots
    for(let sl=1; sl<=2; sl++) {
      const key = `sim${sl}`;
      const sta = si[`slot${sl}_sta`] || 'NOSIM';
      const dbmv = si[`sim${sl}_dbm`] || '0';
      const phone = si[`sim${sl}_msIsdn`] || '';
      const scName = si[`sim${sl}_scName`] || '';
      const netOn = simNet[sl-1];
      const label = sta === 'NOSIM' ? '无卡' : sta === 'OK' ? (scName || '在线') : sta;

      html += `<div class="sim-row">
        <span class="sim-name">卡${sl}</span>
        <span class="sim-status" style="color:${sta==='NOSIM'?'var(--sub)':netOn?'var(--c)':'var(--warn)'}">${label}</span>
        <span class="sim-phone">${phone||'-'}</span>
        <span class="sim-signal">${signalBars(dbmv)} ${dbmv}%</span>
      </div>`;
    }

    html += `</div>
      <div class="foot">
        <button class="btn btn-p btn-sm" onclick="sendSMSModal('${esc(d.device.id)}')">📩 发短信</button>
        <button class="btn btn-sm" onclick="toggleNet('${esc(d.device.id)}','sim1')">卡1联网</button>
        <button class="btn btn-sm" onclick="toggleNet('${esc(d.device.id)}','sim2')">卡2联网</button>
        <button class="btn btn-sm" onclick="cmdAction('${esc(d.device.id)}','restart','确认重启设备？')" title="重启设备">🔄</button>
        <button class="btn btn-sm" onclick="refreshSMS('${esc(d.device.id)}')">📥 同步短信</button>
      </div>
      <div style="padding:8px 16px;border-top:1px solid var(--border);font-size:12px;color:var(--sub);display:flex;gap:12px;align-items:center">
        📦 短信存储：<span id="sms-store-${esc(d.device.id)}" style="color:var(--warn)">查询中...</span>
      </div>
    </div>`;
  });

  document.getElementById('deviceGrid').innerHTML = html;
  document.getElementById('statsCards').innerHTML =
    statCard(online, '在线设备', 'var(--c)') +
    statCard(offline, '离线设备', 'var(--danger)');
  // Check SMS storage status for each device
  data.forEach(d => { if (d.status&&d.status.code===0) checkSmsStore(d.device); });
}

function statCard(num, label, color) {
  return `<div class="stat-card"><div class="num" style="color:${color}">${num}</div><div class="label">${label}</div></div>`;
}

function esc(s) { return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }

// ── Inline name edit ──────────────────────────────────────────
function editName(e, devId, oldName) {
  e.stopPropagation();
  const el = e.target;
  const input = document.createElement('input');
  input.value = oldName;
  input.onblur = async () => {
    const newName = input.value.trim();
    if (newName && newName !== oldName) {
      try {
        await fetch(API+'/devices/'+devId, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:newName})});
        toast('改名成功');
      } catch(e) { toast('改名失败',false); }
    }
    refreshAll();
  };
  input.onkeydown = ev => { if (ev.key==='Enter') input.blur(); if (ev.key==='Escape') { input.value=oldName; input.blur(); } };
  el.innerHTML = '';
  el.appendChild(input);
  input.focus();
  input.select();
}

async function checkSmsStore(dev) {
  const el = document.getElementById('sms-store-'+dev.id);
  if (!el) return;
  try {
    const r = await fetch(API+'/devices/'+dev.id+'/cmd/storesmsen', {method:'POST'});
    const d = await r.json();
    if (d.code===0 && d.val) {
      const parts = d.val.split(';');
      let h = '';
      parts.forEach(p => { const [s,v]=p.split(':'); h += `卡${s}:<span style="color:${v==='on'?'var(--c)':'var(--danger)'}">${v==='on'?'✅':'❌'}</span> `; });
      h += `<button class="btn btn-sm" style="margin-left:4px;padding:2px 6px;font-size:10px" onclick="toggleSmsStore('${esc(dev.id)}')">${d.val.includes('off')?'开启':'关闭'}</button>`;
      el.innerHTML = h;
    }
  } catch(e) {}
}

async function toggleSmsStore(devId) {
  if (!confirm('切换短信存储后需要重启设备才能生效。继续？')) return;
  try {
    const r1 = await fetch(API+'/devices/'+devId+'/cmd/storesmsen', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({p1:'0',p2:'on'})});
    const d1 = await r1.json();
    if (d1.code!==0) { toast('设置失败: '+(d1.note||''), false); return; }
    toast('已开启，正在重启设备...');
    await fetch(API+'/devices/'+devId+'/cmd/restart', {method:'POST'});
    setTimeout(refreshAll, 10000);
  } catch(e) { toast('操作失败', false); }
}

// ══════════ Commands ═══════════════════════════════════════════
async function cmdAction(devId, cmd, confirmMsg, params) {
  if (confirmMsg && !confirm(confirmMsg)) return;
  try {
    const opts = params ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(params)} : {method:'POST'};
    const r = await fetch(API+'/devices/'+devId+'/cmd/'+cmd, opts);
    const d = await r.json();
    toast(d.code===0 ? '执行成功' : '失败: '+(d.note||''), d.code===0);
    if (d.code===0) setTimeout(refreshAll, 1000);
  } catch(e) { toast('请求失败', false); }
}

async function toggleNet(devId, slot) {
  try {
    // Get current status
    const r = await fetch(API+'/devices/'+devId+'/status');
    const d = await r.json();
    const s = d.status || {};
    const simNet = s.simNet || [false, false];
    const idx = slot==='sim1'?0:1;
    const newVal = simNet[idx] ? 0 : 1;
    const label = newVal ? '开启' : '关闭';
    if (!confirm(`确定${label} ${slot} 联网？`)) return;
    await cmdAction(devId, 'slotnet', null, {sid: slot, v: String(newVal)});
  } catch(e) { toast('获取状态失败', false); }
}

async function refreshSMS(devId) {
  try {
    const r = await fetch(API+'/sms/refresh/'+devId, {method:'POST'});
    const d = await r.json();
    toast('同步完成', d.ok);
  } catch(e) { toast('同步失败', false); }
}

// ══════════ Send SMS Modal ═════════════════════════════════════
function sendSMSModal(devId) {
  const html = `
    <div class="modal-box">
      <h3>📩 发送短信</h3>
      <form onsubmit="sendSMS(event,'${esc(devId)}')">
        <div class="form-group" style="margin-bottom:12px">
          <label>卡槽</label>
          <select id="smsSlot"><option value="sim1">SIM卡1</option><option value="sim2">SIM卡2</option></select>
        </div>
        <div class="form-group" style="margin-bottom:12px">
          <label>目标号码 *</label><input id="smsTarget" placeholder="手机号" required>
        </div>
        <div class="form-group" style="margin-bottom:12px">
          <label>短信内容 *</label><input id="smsContent" placeholder="短信内容" required>
        </div>
        <div class="actions">
          <button type="button" class="btn" onclick="closeModal()">取消</button>
          <button type="submit" class="btn btn-p">发送</button>
        </div>
      </form>
    </div>`;
  showModal(html);
}

function showModal(html) {
  let o = document.getElementById('modal');
  if (!o) {
    o = document.createElement('div'); o.id = 'modal'; o.className = 'modal-overlay';
    o.onclick = e => { if (e.target===o) closeModal(); };
    document.body.appendChild(o);
  }
  o.innerHTML = html; o.classList.add('show');
}
function closeModal() {
  const o = document.getElementById('modal');
  if (o) o.classList.remove('show');
}

async function sendSMS(e, devId) {
  e.preventDefault();
  const slot = document.getElementById('smsSlot').value;
  const phone = document.getElementById('smsTarget').value.trim();
  const content = document.getElementById('smsContent').value.trim();
  if (!phone || !content) return;
  await cmdAction(devId, 'sendsms', null, {p1: slot.replace('sim',''), p2: phone, p3: content});
  closeModal();
}

// ══════════ SMS Page ═══════════════════════════════════════════
async function loadSMS(pg) {
  if (pg) smsPage = pg;
  const dev = document.getElementById('smsDevFilter').value;
  const phone = document.getElementById('smsPhoneFilter').value.trim();
  const kw = document.getElementById('smsKwFilter').value.trim();
  const dir = document.getElementById('smsDirFilter').value;
  const params = new URLSearchParams({page: smsPage, per_page: 30});
  if (dev) params.set('device_id', dev);
  if (phone) params.set('phone', phone);
  if (kw) params.set('keyword', kw);
  if (dir) params.set('direction', dir);

  try {
    const r = await fetch(API+'/sms?'+params);
    const d = await r.json();
    let html = '';
    d.items.forEach(m => {
      html += `<tr>
        <td>${fmtTime(m.sms_time)}</td>
        <td>${esc(m.device_name||m.device_id)}</td>
        <td>${esc(m.sim_slot)}</td>
        <td><span class="badge ${m.direction==='received'?'badge-rec':'badge-sent'}">${m.direction==='received'?'接收':'发送'}</span></td>
        <td>${esc(m.phone)}</td>
        <td class="content">${esc(m.content)}</td>
      </tr>`;
    });
    document.getElementById('smsTableBody').innerHTML = html || '<tr><td colspan="6" style="text-align:center;color:var(--sub)">暂无记录</td></tr>';
    renderPagination('sms', d.total, d.page, d.per_page);
  } catch(e) { toast('加载失败', false); }
}

async function loadLogs(pg) {
  if (pg) logPage = pg;
  const dev = document.getElementById('logDevFilter').value;
  const phone = document.getElementById('logPhoneFilter').value.trim();
  const params = new URLSearchParams({page: logPage, per_page: 30});
  if (dev) params.set('device_id', dev);
  if (phone) params.set('phone', phone);

  try {
    const r = await fetch(API+'/logs?'+params);
    const d = await r.json();
    let html = '';
    d.items.forEach(l => {
      html += `<tr>
        <td>${fmtTime(l.call_time)}</td>
        <td>${esc(l.device_name||l.device_id)}</td>
        <td><span class="badge ${l.direction==='incoming'?'badge-incoming':'badge-outgoing'}">${l.direction==='incoming'?'呼入':'呼出'}</span></td>
        <td>${esc(l.phone)}</td>
        <td>${esc(l.action)}</td>
        <td>${l.duration||0}秒</td>
      </tr>`;
    });
    document.getElementById('logTableBody').innerHTML = html || '<tr><td colspan="6" style="text-align:center;color:var(--sub)">暂无记录</td></tr>';
    renderPagination('log', d.total, d.page, d.per_page);
  } catch(e) { toast('加载失败', false); }
}

function renderPagination(type, total, page, pp) {
  const max = Math.ceil(total / pp);
  const el = document.getElementById(type+'Pagination');
  if (max <= 1) { el.innerHTML = ''; return; }
  let h = `<button ${page>1?'':'disabled'} onclick="load${type==='sms'?'SMS':'Logs'}(${page-1})">‹</button>`;
  h += `<span>${page}/${max} (共${total}条)</span>`;
  h += `<button ${page<max?'':'disabled'} onclick="load${type==='sms'?'SMS':'Logs'}(${page+1})">›</button>`;
  el.innerHTML = h;
}

// ══════════ Device Management ══════════════════════════════════
async function loadDevices() {
  try {
    const r = await fetch(API+'/devices');
    const data = await r.json();
    let html = '';
    if (data.length === 0) html = '<tr><td colspan="6" style="text-align:center;color:var(--sub)">暂无设备，请添加</td></tr>';
    else data.forEach(d => {
      html += `<tr>
        <td style="font-size:12px;font-family:monospace">${esc(d.id)}</td>
        <td>${esc(d.name)}</td>
        <td>${esc(d.ip)}</td>
        <td>${esc(d.notes)}</td>
        <td style="font-size:12px">${fmtTime(d.created_at)}</td>
        <td><button class="btn btn-r btn-sm" onclick="delDevice('${esc(d.id)}')">删除</button></td>
      </tr>`;
    });
    document.getElementById('devTableBody').innerHTML = html;

    // Populate filters
    ['smsDevFilter','logDevFilter'].forEach(fid => {
      const sel = document.getElementById(fid);
      if (!sel) return;
      const v = sel.value;
      sel.innerHTML = '<option value="">全部设备</option>';
      data.forEach(d => { sel.innerHTML += `<option value="${esc(d.id)}">${esc(d.name)}</option>`; });
      sel.value = v;
    });
  } catch(e) { toast('加载设备列表失败', false); }
}

async function addDevice(e) {
  e.preventDefault();
  const name = document.getElementById('addName').value.trim();
  const ip = document.getElementById('addIP').value.trim();
  const pwd = document.getElementById('addPwd').value.trim();
  try {
    const r = await fetch(API+'/devices', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({name, ip, password: pwd})
    });
    if (!r.ok) { const d = await r.json(); toast(d.detail||'添加失败', false); return; }
    toast('添加成功');
    document.getElementById('addName').value = '';
    document.getElementById('addIP').value = '';
    document.getElementById('addPwd').value = '';
    loadDevices();
  } catch(e) { toast('请求失败', false); }
}

async function quickAddDevice(e) {
  e.preventDefault();
  const name = document.getElementById('qaName').value.trim();
  const ip = document.getElementById('qaIP').value.trim();
  const pwd = document.getElementById('qaPwd').value.trim();
  try {
    const r = await fetch(API+'/devices', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({name, ip, password: pwd})
    });
    if (!r.ok) { const d = await r.json(); toast(d.detail||'添加失败', false); return; }
    toast('添加成功');
    document.getElementById('qaName').value = '';
    document.getElementById('qaIP').value = '';
    document.getElementById('qaPwd').value = '';
    document.getElementById('quickAddForm').classList.add('mr');
    refreshAll();
    loadDevices();
  } catch(e) { toast('请求失败', false); }
}

async function delDevice(id) {
  if (!confirm('确定删除该设备？短信和通话记录也将被删除。')) return;
  try {
    await fetch(API+'/devices/'+id, {method:'DELETE'});
    toast('已删除');
    loadDevices();
  } catch(e) { toast('删除失败', false); }
}

// ══════════ Init ═══════════════════════════════════════════════
function getCurrentPage() {
  const path = location.pathname;
  if (path === '/sms') return 'sms';
  if (path === '/logs') return 'logs';
  if (path === '/devices') return 'devices';
  return 'dashboard';
}

document.addEventListener('DOMContentLoaded', () => {
  const p = getCurrentPage();
  if (p === 'dashboard') refreshAll();
  else if (p === 'sms') { loadDevices(); loadSMS(); }
  else if (p === 'logs') { loadDevices(); loadLogs(); }
  else if (p === 'devices') loadDevices();
});
"""


# ── Entry ──────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=34567)
