# GAS-1 · Global Awareness System v1

Real-time multi-domain situational awareness:
- ✈️ **Aircraft** — OpenSky Network (OAuth2, live)
- 🚢 **Vessels** — AISStream WebSocket (real-time AIS)
- 🛰️ **Satellites** — CelesTrak TLE, 6 categories, orbital propagation every 15s
- 📡 **GPS Jamming** — GPSJam.org daily interference map (live)
- 🌍 **2D map** (Leaflet) + **3D globe** (Cesium)

---

## Why a backend?

Browsers block direct API calls via CORS security rules.
- OpenSky's OAuth2 token server doesn't allow browser fetch
- GPSJam doesn't send CORS headers
- AISStream explicitly says: don't connect from a browser (exposes your key)
- Cesium 3D works properly only when served from HTTP, not file://

The backend runs locally, proxies everything, and serves the frontend — solving all of these at once.

---

## Setup (one time)

**Requirements:** Python 3.10+

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run
python backend.py

# 3. Open browser
http://localhost:8000

```

---

## Limitations and other info

In order to use this tool, please consider the following limitations:
- Me (This is just a fun weekend vibe-coding project, I'm not an intelligence expert or analyst, I just like data).
- For vessel data, you need to create an account in AISStream (free account) and get an api key. See https://aisstream.io/apikeys
- For aircraft data, you need to create a free account on OpenSky Network and get the client_id and clientSecret. See https://opensky-network.org/
- The keys are necessary to start this tool.
- Real-time aircraft data is limited to just 1k per day (due to api rate usage).

---

## Data sources

| Layer | Source | Method | Notes |
|---|---|---|---|
| Aircraft | OpenSky Network | OAuth2 → REST | 4 credits/call, polls every 90s |
| Vessels | AISStream | WebSocket (real-time) | Global AIS stream |
| Satellites | CelesTrak | TLE text | 6 categories, propagated every 15s |
| GPS Jamming | GPSJam.org | Daily CSV | Aggregated from ADS-B GPS quality |

---

## OpenSky credit budget

Standard account = 4,000 credits/day for `/states/all`.
Global call = 4 credits.  1000 calls/day max.
Polling every 90s = 960 calls/day — within budget.
The app automatically slows polling if credits run low.
