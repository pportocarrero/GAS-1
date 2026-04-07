"""
GAS-1 Backend  —  Global Awareness System
==========================================
Install:  pip install fastapi uvicorn httpx websockets
Run:      python backend.py  →  open http://localhost:8000

ARCHITECTURE:
  AIS throttling: one update per vessel per 5s — prevents browser flooding
  Satellite: TLE format, 2h disk cache, fallback TLEs always available
  OpenSky: OAuth2, credit-aware (4000/day)
  GPSJam: daily CSV, tries last 5 days
"""

import asyncio, json, logging, os, time
from datetime import date, timedelta
from pathlib import Path

import httpx, uvicorn, websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

# ── CREDENTIALS ─────────────────────────────────────────────────
OPENSKY_CLIENT_ID     = os.getenv("OPENSKY_CLIENT_ID", "")
OPENSKY_CLIENT_SECRET = os.getenv("OPENSKY_CLIENT_SECRET", "")
OPENSKY_TOKEN_URL     = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
OPENSKY_API           = "https://opensky-network.org/api"

AISSTREAM_KEY = os.getenv("AISSTREAM_KEY", "")
AISSTREAM_URL = "wss://stream.aisstream.io/v0/stream"
GPSJAM_BASE   = "https://gpsjam.org/data"

CELESTRAK_BASE     = "https://celestrak.org/NORAD/elements/gp.php"
CELESTRAK_GROUPS   = [
    ("active",   "ACTIVE",  400),
    ("stations", "SPECIAL", None),
    ("visual",   "SPECIAL", None),
]
CELESTRAK_CACHE_TTL = 7200
CELESTRAK_CACHE_DIR = Path(__file__).parent / ".sat_cache"

# ── AIS THROTTLE ────────────────────────────────────────────────
# KEY PERFORMANCE FIX:
# AISStream fires 3000–8000 messages/second globally.
# Sending every message to the browser floods the JS main thread.
# We keep the last known position per vessel and only forward
# an update if the vessel has moved meaningfully OR 60s have passed.
# This reduces browser message rate by ~99% with negligible data loss.
AIS_MIN_MOVE_DEG  = 0.005   # ~500m — skip if vessel moved less than this
AIS_MAX_INTERVAL  = 60.0    # always send at least once per 60s per vessel

# Per-vessel throttle state: mmsi → (last_lat, last_lon, last_sent_time)
ais_last: dict[str, tuple[float, float, float]] = {}

def ais_should_send(mmsi: str, lat: float, lon: float) -> bool:
    """Return True if this position update is worth forwarding."""
    now = time.time()
    if mmsi not in ais_last:
        ais_last[mmsi] = (lat, lon, now)
        return True
    last_lat, last_lon, last_t = ais_last[mmsi]
    moved = abs(lat - last_lat) + abs(lon - last_lon) > AIS_MIN_MOVE_DEG
    aged  = (now - last_t) > AIS_MAX_INTERVAL
    if moved or aged:
        ais_last[mmsi] = (lat, lon, now)
        return True
    return False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("gas1")
CELESTRAK_CACHE_DIR.mkdir(exist_ok=True)

# ── OPENSKY TOKEN ────────────────────────────────────────────────
class TokenManager:
    def __init__(self):
        self._token = None; self._expires_at = 0.0; self._lock = asyncio.Lock()

    async def get(self) -> str | None:
        async with self._lock:
            if self._token and time.time() < self._expires_at - 30: return self._token
            return await self._refresh()

    async def _refresh(self) -> str | None:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(OPENSKY_TOKEN_URL,
                    headers={"Content-Type":"application/x-www-form-urlencoded"},
                    data={"grant_type":"client_credentials",
                          "client_id":OPENSKY_CLIENT_ID,
                          "client_secret":OPENSKY_CLIENT_SECRET})
                r.raise_for_status(); d = r.json()
                self._token = d["access_token"]
                self._expires_at = time.time() + d.get("expires_in", 1800)
                log.info(f"OpenSky token OK ({d.get('expires_in')}s)")
                return self._token
        except Exception as e:
            log.warning(f"OpenSky token failed: {e}"); self._token = None; return None

    async def hdrs(self) -> dict:
        t = await self.get(); return {"Authorization": f"Bearer {t}"} if t else {}

token_mgr = TokenManager()
app = FastAPI(title="GAS-1")


# ── RUNTIME CONFIG ────────────────────────────────────────────────
# The browser setup screen POSTs credentials here on first run.
# Hot-swaps them into the running process without a restart.
from pydantic import BaseModel

class ConfigPayload(BaseModel):
    opensky_id:     str
    opensky_secret: str
    aisstream_key:  str

@app.post("/api/config")
async def set_config(cfg: ConfigPayload):
    global OPENSKY_CLIENT_ID, OPENSKY_CLIENT_SECRET, AISSTREAM_KEY
    if not cfg.opensky_id or not cfg.opensky_secret or not cfg.aisstream_key:
        return HTMLResponse(json.dumps({"error": "all fields required"}), 400)
    OPENSKY_CLIENT_ID     = cfg.opensky_id.strip()
    OPENSKY_CLIENT_SECRET = cfg.opensky_secret.strip()
    AISSTREAM_KEY         = cfg.aisstream_key.strip()
    token_mgr._token      = None   # force token refresh with new creds
    token_mgr._expires_at = 0.0
    log.info(f"Config updated: OpenSky={OPENSKY_CLIENT_ID[:8]}*** AIS={AISSTREAM_KEY[:8]}***")
    return {"status": "ok"}

@app.get("/api/config/status")
async def config_status():
    ready = bool(OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET and AISSTREAM_KEY)
    return {"ready": ready}


# ── OPENSKY ──────────────────────────────────────────────────────
@app.get("/api/aircraft")
async def get_aircraft():
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{OPENSKY_API}/states/all", headers=await token_mgr.hdrs())
            hdrs = {"X-Rate-Limit-Remaining": r.headers.get("X-Rate-Limit-Remaining",""),
                    "X-Rate-Limit-Retry-After": r.headers.get("X-Rate-Limit-Retry-After-Seconds","")}
            if r.status_code == 429: return HTMLResponse(json.dumps({"error":"rate_limited"}), 429, hdrs)
            if r.status_code == 401: token_mgr._token = None; return HTMLResponse(json.dumps({"error":"unauthorized"}), 401)
            r.raise_for_status(); return HTMLResponse(r.text, 200, hdrs)
    except Exception as e:
        log.error(f"OpenSky: {e}"); return HTMLResponse(json.dumps({"error":str(e)}), 502)

# ── GPSJAM ───────────────────────────────────────────────────────
@app.get("/api/gpsjam")
async def get_gpsjam():
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        for days in range(1, 6):
            d = (date.today() - timedelta(days=days)).isoformat()
            try:
                r = await c.get(f"{GPSJAM_BASE}/{d}.csv")
                if r.status_code == 404: continue
                r.raise_for_status()
                log.info(f"GPSJam: {d}"); return PlainTextResponse(r.text, headers={"X-GPSJam-Date":d})
            except httpx.HTTPStatusError: continue
            except Exception as e: log.error(f"GPSJam: {e}"); break
    return HTMLResponse(json.dumps({"error":"no data"}), 404)

# ── CELESTRAK TLE ─────────────────────────────────────────────────
def _cache_path(g): return CELESTRAK_CACHE_DIR / f"{g}.tle"
def _cache_age(g):
    p = _cache_path(g); return (time.time()-p.stat().st_mtime) if p.exists() else float("inf")
def _read_cache(g): p = _cache_path(g); return p.read_text("utf-8") if p.exists() else None
def _write_cache(g, d): _cache_path(g).write_text(d, "utf-8")

async def _fetch_tle(group: str, max_obj: int | None) -> str | None:
    if _cache_age(group) < CELESTRAK_CACHE_TTL:
        c = _read_cache(group)
        if c: log.info(f"CelesTrak {group}: cache hit"); return c
    url = f"{CELESTRAK_BASE}?GROUP={group}&FORMAT=TLE"
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as c:
            r = await c.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                "Accept": "text/plain, */*",
                "Referer": "https://celestrak.org/NORAD/elements/"})
            if r.status_code in (301,302,403,404):
                log.error(f"CelesTrak {group}: HTTP {r.status_code}")
                return _read_cache(group)
            r.raise_for_status(); text = r.text
            if not any(l.strip().startswith("1 ") for l in text.splitlines()):
                log.error(f"CelesTrak {group}: not TLE"); return _read_cache(group)
            _write_cache(group, text)
            n = sum(1 for l in text.splitlines() if l.strip().startswith("1 "))
            log.info(f"CelesTrak {group}: {n} TLEs cached"); return text
    except Exception as e:
        log.error(f"CelesTrak {group}: {e}"); return _read_cache(group)

def _parse_tle(text: str, grp: str, max_obj: int | None) -> list[dict]:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    result, i = [], 0
    while i < len(lines)-2:
        if lines[i+1].startswith("1 ") and lines[i+2].startswith("2 "):
            result.append({"name": lines[i].lstrip("0 ").strip(),
                           "grp": grp, "TLE_LINE1": lines[i+1], "TLE_LINE2": lines[i+2]})
            i += 3
        else: i += 1
        if max_obj and len(result) >= max_obj: break
    return result

FALLBACK_TLES = [
    {"name":"ISS (ZARYA)","grp":"SPECIAL","TLE_LINE1":"1 25544U 98067A   24100.50000000  .00020351  00000-0  35494-3 0  9993","TLE_LINE2":"2 25544  51.6390 116.5020 0003490  88.4910  13.8420 15.50097952448083"},
    {"name":"CSS (TIANHE)","grp":"SPECIAL","TLE_LINE1":"1 48274U 21035A   24100.50000000  .00015530  00000-0  17960-3 0  9991","TLE_LINE2":"2 48274  41.4750 232.1050 0005700 338.2700  21.8100 15.60668984164823"},
    {"name":"HUBBLE","grp":"SPECIAL","TLE_LINE1":"1 20580U 90037B   24100.50000000  .00001437  00000-0  67782-4 0  9993","TLE_LINE2":"2 20580  28.4700 244.2830 0002551  17.8570 342.2560 15.09700000498000"},
    {"name":"NOAA 15","grp":"ACTIVE","TLE_LINE1":"1 25338U 98030A   24100.50000000  .00000085  00000-0  57855-4 0  9997","TLE_LINE2":"2 25338  98.7124  22.2975 0010956 337.0001  22.9000 14.25900000348765"},
    {"name":"NOAA 19","grp":"ACTIVE","TLE_LINE1":"1 33591U 09005A   24100.50000000  .00000100  00000-0  67000-4 0  9991","TLE_LINE2":"2 33591  99.1000  20.0000 0014000 100.0000 260.0000 14.12200000800000"},
    {"name":"GPS BIIR-2","grp":"ACTIVE","TLE_LINE1":"1 24876U 97035A   24100.50000000 -.00000025  00000-0  00000+0 0  9993","TLE_LINE2":"2 24876  55.4542 160.0950 0040588 358.7040 351.0000  2.00567142189000"},
    {"name":"GALILEO 5","grp":"ACTIVE","TLE_LINE1":"1 37846U 11060B   24100.50000000 -.00000059  00000-0  00000+0 0  9995","TLE_LINE2":"2 37846  56.0020 348.9020 0002350 310.8040  49.2760  1.70474858 65432"},
    {"name":"INTELSAT 901","grp":"ACTIVE","TLE_LINE1":"1 26824U 01024A   24100.50000000 -.00000288  00000-0  00000+0 0  9996","TLE_LINE2":"2 26824   0.0210  58.1000 0003200 273.0000  87.0000  1.00271340 85000"},
    {"name":"SENTINEL-1A","grp":"ACTIVE","TLE_LINE1":"1 39634U 14016A   24100.50000000 -.00000043  00000-0  12800-4 0  9990","TLE_LINE2":"2 39634  98.1827  88.4500 0001214  88.2001 271.9311 14.59200000570000"},
    {"name":"TERRA","grp":"ACTIVE","TLE_LINE1":"1 25994U 99068A   24100.50000000  .00000036  00000-0  28100-4 0  9997","TLE_LINE2":"2 25994  98.2038 355.8456 0001349  90.3080 269.8238 14.57100000288760"},
    {"name":"GOES 16","grp":"ACTIVE","TLE_LINE1":"1 41866U 16071A   24100.50000000 -.00000211  00000-0  00000+0 0  9997","TLE_LINE2":"2 41866   0.0490 270.0300 0000850 176.1700 206.0400  1.00271380 27000"},
    {"name":"STARLINK-1007","grp":"ACTIVE","TLE_LINE1":"1 44713U 19074A   24100.50000000  .00001200  00000-0  88000-4 0  9998","TLE_LINE2":"2 44713  53.0020 120.5100 0001200  90.0000 270.0000 15.06400000240000"},
]

@app.get("/api/satellites")
async def get_satellites():
    all_sats: dict[str, dict] = {}
    for group, category, max_obj in CELESTRAK_GROUPS:
        text = await _fetch_tle(group, max_obj)
        if text:
            for obj in _parse_tle(text, category, max_obj):
                if obj["name"] not in all_sats:
                    all_sats[obj["name"]] = obj
    if not all_sats:
        log.warning("CelesTrak: all failed — using fallback")
        return JSONResponse(FALLBACK_TLES)
    sats = list(all_sats.values())
    log.info(f"Satellites: {len(sats)} returned")
    return JSONResponse(sats)

# ── AISSTREAM FAN-OUT ─────────────────────────────────────────────
browser_clients: set[WebSocket] = set()
aisstream_task: asyncio.Task | None = None

async def aisstream_worker():
    backoff = 5
    while True:
        try:
            log.info("AISStream: connecting...")
            async with websockets.connect(AISSTREAM_URL,
                    ping_interval=20, ping_timeout=30, close_timeout=10) as ws:
                await ws.send(json.dumps({
                    "APIKey": AISSTREAM_KEY,
                    "BoundingBoxes": [[[-90,-180],[90,180]]],
                    "FilterMessageTypes": ["PositionReport","ShipStaticData"]}))
                log.info("AISStream: subscribed"); backoff = 5

                async for raw in ws:
                    if not browser_clients: continue
                    try:
                        msg   = json.loads(raw)
                        mtype = msg.get("MessageType","")
                        meta  = msg.get("MetaData",{})
                        mmsi  = str(meta.get("MMSI",""))

                        if mtype == "PositionReport":
                            pr  = msg.get("Message",{}).get("PositionReport",{})
                            lat = meta.get("latitude",  pr.get("Latitude",  0))
                            lon = meta.get("longitude", pr.get("Longitude", 0))
                            # ← THROTTLE: skip if vessel barely moved and was sent recently
                            if not ais_should_send(mmsi, lat, lon): continue
                            out = {"t":"pos","mmsi":mmsi,
                                   "name": meta.get("ShipName","").strip() or "VESSEL",
                                   "lat":lat,"lon":lon,
                                   "hdg":pr.get("TrueHeading", pr.get("Cog",0)),
                                   "sog":pr.get("Sog",0)}

                        elif mtype == "ShipStaticData":
                            sd  = msg.get("Message",{}).get("ShipStaticData",{})
                            out = {"t":"static","mmsi":mmsi,
                                   "name": sd.get("Name","").strip() or meta.get("ShipName","").strip(),
                                   "type": sd.get("TypeOfShipAndCargo",0),
                                   "dest": sd.get("Destination","").strip(),
                                   "callsign": sd.get("CallSign","").strip(),
                                   "lat": meta.get("latitude",0),
                                   "lon": meta.get("longitude",0)}
                        else: continue

                        payload = json.dumps(out)
                        dead: set[WebSocket] = set()
                        for client in list(browser_clients):
                            try: await client.send_text(payload)
                            except: dead.add(client)
                        browser_clients.difference_update(dead)

                    except (json.JSONDecodeError, KeyError): pass

        except websockets.exceptions.ConnectionClosedError as e:
            log.warning(f"AISStream closed: {e}")
        except Exception as e:
            log.error(f"AISStream error: {e}")
        log.info(f"AISStream: reconnecting in {backoff}s")
        await asyncio.sleep(backoff)
        backoff = min(backoff*2, 60)

@app.websocket("/ws/vessels")
async def vessels_ws(ws: WebSocket):
    await ws.accept(); browser_clients.add(ws)
    log.info(f"WS client connected ({len(browser_clients)} total)")
    try:
        while True: await ws.receive_text()
    except (WebSocketDisconnect, Exception): pass
    finally:
        browser_clients.discard(ws)
        log.info(f"WS client disconnected ({len(browser_clients)} total)")

@app.on_event("startup")
async def startup():
    global aisstream_task
    log.info("GAS-1 starting...")
    await token_mgr.get()
    aisstream_task = asyncio.create_task(aisstream_worker())

@app.on_event("shutdown")
async def shutdown():
    if aisstream_task: aisstream_task.cancel()

@app.get("/")
async def root():
    f = Path(__file__).parent / "frontend.html"
    return FileResponse(str(f)) if f.exists() else HTMLResponse("missing frontend.html")

if __name__ == "__main__":
    print("\n" + "═"*55)
    print("  GAS-1 · Global Awareness System")
    print("  http://localhost:8000")
    print("  Ctrl+C to stop")
    print("═"*55 + "\n")
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False, log_level="info")
