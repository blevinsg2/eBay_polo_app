#!/usr/bin/env python3
"""Polo Pipeline local bridge server.

Serves the static app, handoff API, Nominatim geocode, OSRM route, and pit-stop candidate proxies.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import os

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
WEB_DIR = ROOT / "web"
DATA_DIR = Path(os.environ.get("POLO_DATA_DIR") or (ROOT / "var")).resolve()
PENDING = DATA_DIR / "handoffs" / "pending"
DONE = DATA_DIR / "handoffs" / "done"
HOST = os.environ.get("POLO_HOST", "127.0.0.1")
PORT = int(os.environ.get("POLO_PORT", "8765"))

# Nominatim's usage policy wants an identifying User-Agent. Put your own app name/contact in POLO_NOMINATIM_UA.
NOMINATIM_UA = os.environ.get("POLO_NOMINATIM_UA") or "polo-pipeline/1.0 (self-hosted; set POLO_NOMINATIM_UA)"
NOMINATIM_BASE = os.environ.get("POLO_NOMINATIM_BASE", "https://nominatim.openstreetmap.org")
OSRM_BASE = os.environ.get("POLO_OSRM_BASE", "https://router.project-osrm.org")

# Pit stops (gas/food) suggested along a route leg. This is a curated list, not a live scrape.
# The repo ships EXAMPLE stops in Columbus, OH. Point POLO_PITSTOPS_FILE at your own JSON list.
PITSTOPS_FILE = Path(os.environ.get("POLO_PITSTOPS_FILE") or (ROOT / "config" / "pitstops.example.json"))


def load_pitstops() -> list[dict]:
    try:
        data = json.loads(PITSTOPS_FILE.read_text(encoding="utf-8"))
        return list(data.get("pitstops", data) if isinstance(data, dict) else data)
    except Exception as e:
        print(f"[polo-pipeline] no pit stops loaded from {PITSTOPS_FILE.name}: {e}")
        return []


PIT_CANDIDATES = load_pitstops()


def display_path(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def home_config() -> dict | None:
    """Optional home/start location from env. No default on purpose."""
    try:
        lat = float(os.environ.get("POLO_HOME_LAT", ""))
        lng = float(os.environ.get("POLO_HOME_LNG", ""))
    except ValueError:
        return None
    return {"lat": lat, "lng": lng, "label": os.environ.get("POLO_HOME_LABEL") or "Home"}


ALLOWED_ACTIONS = {
    "plan_route": "Thrift Router",
    "process_photos": "Polo Listing Bot",
    "create_listings": "Polo Listing Bot",
    "get_price": "Polo Pricing",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_dirs() -> None:
    PENDING.mkdir(parents=True, exist_ok=True)
    DONE.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "uploads" / "pricing").mkdir(parents=True, exist_ok=True)


def seed_examples() -> None:
    """Copy the EXAMPLE fixtures into an empty data dir so the UI has something to show."""
    import shutil
    ex = ROOT / "examples"
    for name in ("receipts.json", "location-ratings.json"):
        src, dst = ex / "data" / name, DATA_DIR / name
        if src.exists() and not dst.exists():
            shutil.copyfile(src, dst)
    for src in sorted((ex / "handoffs").glob("*.json")):
        dst = DONE / src.name
        if not dst.exists():
            shutil.copyfile(src, dst)
    up = DATA_DIR / "uploads" / "pricing" / "example"
    up.mkdir(parents=True, exist_ok=True)
    for src, name in (("example_front.jpg", "front.jpg"), ("example_tag.jpg", "tag.jpg")):
        if (ex / "photos" / src).exists() and not (up / name).exists():
            shutil.copyfile(ex / "photos" / src, up / name)
    if RECEIPTS.exists():
        save_receipts(load_receipts())


def safe_id(job_id: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{8,64}", job_id))


def list_pending() -> list[dict]:
    jobs = []
    for path in sorted(PENDING.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            jobs.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return jobs


def list_done(limit: int = 20) -> list[dict]:
    jobs = []
    for path in sorted(DONE.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            jobs.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
        if len(jobs) >= limit:
            break
    return jobs


RECEIPTS = DATA_DIR / "receipts.json"
RECEIPT_STATS = DATA_DIR / "receipt-stats.json"
LOCATION_RATINGS = DATA_DIR / "location-ratings.json"


def load_ratings() -> dict:
    try:
        return json.loads(LOCATION_RATINGS.read_text(encoding="utf-8")).get("locations", {})
    except Exception:
        return {}


def save_ratings(rt: dict) -> None:
    LOCATION_RATINGS.parent.mkdir(parents=True, exist_ok=True)
    LOCATION_RATINGS.write_text(json.dumps({"updatedAt": utc_now(), "locations": rt}, indent=2) + "\n", encoding="utf-8")


def load_receipts() -> list[dict]:
    try:
        return json.loads(RECEIPTS.read_text(encoding="utf-8")).get("receipts", [])
    except Exception:
        return []


def receipt_stats(recs: list[dict]) -> list[dict]:
    """Per-location rollup; also written to $POLO_DATA_DIR/receipt-stats.json for Route Planning skip logic."""
    by: dict[str, dict] = {}
    today = datetime.now(timezone.utc).date()
    for r in recs:
        addr = re.sub(r"[^a-z0-9]", "", str(r.get("address") or "").lower().split(",")[0])
        key = ("a:" + addr) if addr else ("s:" + str(r.get("store") or "Unknown").lower().strip())
        s = by.setdefault(key, {"key": key, "store": r.get("store") or "Unknown", "address": r.get("address") or "",
                                "totalSpent": 0.0, "visits": 0, "items": 0, "lastVisit": None})
        s["totalSpent"] = round(s["totalSpent"] + float(r.get("total") or 0), 2)
        s["visits"] += 1
        s["items"] += int(r.get("itemCount") or 0)
        d = r.get("date")
        if d and (not s["lastVisit"] or d > s["lastVisit"]):
            s["lastVisit"] = d
    out = []
    ratings = load_ratings()
    for s in by.values():
        rt = ratings.get(s["key"]) or {}
        s["rating"] = rt.get("rating") or None
        s["notes"] = rt.get("notes") or ""
        try:
            s["daysSince"] = (today - datetime.strptime(s["lastVisit"], "%Y-%m-%d").date()).days
        except Exception:
            s["daysSince"] = None
        s["avgPerVisit"] = round(s["totalSpent"] / s["visits"], 2) if s["visits"] else 0
        out.append(s)
    out.sort(key=lambda s: -s["totalSpent"])
    return out


def save_receipts(recs: list[dict]) -> None:
    RECEIPTS.parent.mkdir(parents=True, exist_ok=True)
    RECEIPTS.write_text(json.dumps({"receipts": recs}, indent=2) + "\n", encoding="utf-8")
    RECEIPT_STATS.write_text(json.dumps({"updatedAt": utc_now(), "locations": receipt_stats(recs)}, indent=2) + "\n", encoding="utf-8")


def find_job(job_id: str) -> tuple[dict | None, Path | None, str | None]:
    for folder, loc in ((PENDING, "pending"), (DONE, "done")):
        for path in folder.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("id") == job_id:
                return data, path, loc
        candidate = folder / f"{job_id}.json"
        if candidate.exists():
            try:
                return json.loads(candidate.read_text(encoding="utf-8")), candidate, loc
            except (OSError, json.JSONDecodeError):
                pass
    return None, None, None


def nominatim_get(path: str, params: dict) -> dict | list:
    qs = urllib.parse.urlencode(params)
    url = f"{NOMINATIM_BASE}{path}?{qs}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": NOMINATIM_UA,
            "Accept": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        return json.loads(resp.read().decode("utf-8"))


def osrm_route(coords: str) -> dict:
    """Proxy OSRM driving route. coords = 'lng,lat;lng,lat;...'"""
    url = (
        f"{OSRM_BASE}/route/v1/driving/{coords}"
        f"?overview=full&geometries=geojson&steps=false"
    )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": NOMINATIM_UA,
            "Accept": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _point_to_segment_dist_m(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    """Approximate meters from point P to segment AB (lat/lng treated as planar locally)."""
    # Rough degrees→meters at ~38N
    m_per_deg_lat = 111320.0
    m_per_deg_lng = 111320.0 * 0.788  # cos(38°)
    axm, aym = ax * m_per_deg_lng, ay * m_per_deg_lat
    bxm, bym = bx * m_per_deg_lng, by * m_per_deg_lat
    pxm, pym = px * m_per_deg_lng, py * m_per_deg_lat
    dx, dy = bxm - axm, bym - aym
    if dx == 0 and dy == 0:
        return ((pxm - axm) ** 2 + (pym - aym) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((pxm - axm) * dx + (pym - aym) * dy) / (dx * dx + dy * dy)))
    qx, qy = axm + t * dx, aym + t * dy
    return ((pxm - qx) ** 2 + (pym - qy) ** 2) ** 0.5


def pit_candidates_for_leg(from_lat: float, from_lng: float, to_lat: float, to_lng: float, limit: int = 12) -> list[dict]:
    ranked = []
    for c in PIT_CANDIDATES:
        detour_m = _point_to_segment_dist_m(c["lng"], c["lat"], from_lng, from_lat, to_lng, to_lat)
        # Soft cap: prefer within ~2.5 mi of corridor
        item = dict(c)
        item["detourMeters"] = round(detour_m)
        item["detourMiles"] = round(detour_m / 1609.34, 2)
        ranked.append(item)
    ranked.sort(key=lambda x: x["detourMeters"])
    return ranked[: max(0, min(limit, len(ranked)))]


PIT_MIN_LEG_MILES = 2.0


def pit_geofence_for_leg(from_lat, from_lng, to_lat, to_lng, limit=12):
    """Small geofence between two neighboring stops.

    - Leg shorter than PIT_MIN_LEG_MILES (straight line) -> nothing (too close).
    - Candidate must project strictly inside the A->B segment (10%..90%), i.e. between
      the stops, not behind A or past B.
    - Corridor half-width = clamp(15% of leg, 0.5 mi, 2 mi).
    Returns (candidates, reason).
    """
    leg_m = _point_to_segment_dist_m(to_lng, to_lat, from_lng, from_lat, from_lng, from_lat)
    leg_mi = leg_m / 1609.34
    if leg_mi < PIT_MIN_LEG_MILES:
        return [], "leg_too_short", leg_mi, 0.0
    width_mi = max(0.5, min(2.0, 0.15 * leg_mi))
    import math
    mlat = math.cos(math.radians((from_lat + to_lat) / 2))
    dx, dy = (to_lng - from_lng) * mlat, to_lat - from_lat
    out = []
    for c in pit_candidates_for_leg(from_lat, from_lng, to_lat, to_lng, limit=1000):
        t = (((c["lng"] - from_lng) * mlat) * dx + (c["lat"] - from_lat) * dy) / (dx * dx + dy * dy)
        if 0.1 <= t <= 0.9 and c["detourMiles"] <= width_mi:
            c["legFraction"] = round(t, 2)
            out.append(c)
    return out[:limit], ("" if out else "none_in_corridor"), leg_mi, width_mi


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def translate_path(self, path):
        # Uploaded photos live in the data dir, everything else is the static web/ folder.
        clean = urlparse(path).path
        if clean.startswith("/uploads/"):
            rel = Path(urllib.parse.unquote(clean[len("/uploads/"):]))
            target = (DATA_DIR / "uploads" / rel).resolve()
            if str(target).startswith(str((DATA_DIR / "uploads").resolve())):
                return str(target)
            return str(WEB_DIR / "__nope__")
        return super().translate_path(path)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def _json(self, code: int, payload: dict | list):
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 1_000_000:
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        if path == "/api/health":
            return self._json(200, {"ok": True, "service": "polo-pipeline", "port": PORT})
        if path == "/api/config":
            return self._json(200, {"home": home_config(), "pitstopCount": len(PIT_CANDIDATES)})

        if path == "/api/geocode/reverse":
            try:
                lat = float((qs.get("lat") or [""])[0])
                lon = float((qs.get("lon") or qs.get("lng") or [""])[0])
            except (TypeError, ValueError):
                return self._json(400, {"error": "lat_and_lon_required"})
            try:
                data = nominatim_get(
                    "/reverse",
                    {
                        "lat": f"{lat:.7f}",
                        "lon": f"{lon:.7f}",
                        "format": "json",
                        "addressdetails": 1,
                    },
                )
            except urllib.error.HTTPError as e:
                return self._json(502, {"error": "nominatim_http", "status": e.code})
            except Exception as e:
                return self._json(502, {"error": "nominatim_failed", "detail": str(e)})
            display = data.get("display_name") if isinstance(data, dict) else None
            return self._json(
                200,
                {
                    "lat": lat,
                    "lon": lon,
                    "display_name": display,
                    "address": data.get("address") if isinstance(data, dict) else None,
                    "raw": data,
                },
            )

        if path == "/api/geocode/search":
            q = ((qs.get("q") or [""])[0] or "").strip()
            if not q:
                return self._json(400, {"error": "q_required"})
            limit = 5
            try:
                limit = min(10, max(1, int((qs.get("limit") or ["5"])[0])))
            except ValueError:
                pass
            try:
                data = nominatim_get(
                    "/search",
                    {
                        "q": q,
                        "format": "json",
                        "limit": str(limit),
                        "addressdetails": 1,
                    },
                )
            except urllib.error.HTTPError as e:
                return self._json(502, {"error": "nominatim_http", "status": e.code})
            except Exception as e:
                return self._json(502, {"error": "nominatim_failed", "detail": str(e)})
            results = []
            if isinstance(data, list):
                for item in data:
                    try:
                        results.append(
                            {
                                "display_name": item.get("display_name"),
                                "lat": float(item["lat"]),
                                "lon": float(item["lon"]),
                                "address": item.get("address"),
                            }
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
            return self._json(200, {"query": q, "results": results})

        if path == "/api/route":
            coords = ((qs.get("coords") or [""])[0] or "").strip()
            # Expect lng,lat;lng,lat;...  (at least 2 points)
            if not coords or ";" not in coords:
                return self._json(400, {"error": "coords_required", "hint": "lng,lat;lng,lat;..."})
            # Basic sanitize
            if not re.fullmatch(r"[-0-9.,;]+", coords):
                return self._json(400, {"error": "invalid_coords"})
            parts = coords.split(";")
            if len(parts) < 2 or len(parts) > 100:
                return self._json(400, {"error": "need_2_to_100_points"})
            try:
                data = osrm_route(coords)
            except urllib.error.HTTPError as e:
                return self._json(502, {"error": "osrm_http", "status": e.code})
            except Exception as e:
                return self._json(502, {"error": "osrm_failed", "detail": str(e)})
            routes = data.get("routes") if isinstance(data, dict) else None
            if not routes:
                return self._json(502, {"error": "osrm_no_route", "raw": data})
            r0 = routes[0]
            return self._json(
                200,
                {
                    "ok": True,
                    "distanceMeters": r0.get("distance"),
                    "durationSeconds": r0.get("duration"),
                    "geometry": r0.get("geometry"),
                    "code": data.get("code"),
                },
            )

        if path == "/api/pitstops/candidates":
            try:
                from_lat = float((qs.get("fromLat") or [""])[0])
                from_lng = float((qs.get("fromLng") or [""])[0])
                to_lat = float((qs.get("toLat") or [""])[0])
                to_lng = float((qs.get("toLng") or [""])[0])
            except (TypeError, ValueError):
                return self._json(400, {"error": "fromLat_fromLng_toLat_toLng_required"})
            limit = 12
            try:
                limit = min(20, max(1, int((qs.get("limit") or ["12"])[0])))
            except ValueError:
                pass
            category = ((qs.get("category") or [""])[0] or "").strip().lower()
            cands, reason, leg_mi, width_mi = pit_geofence_for_leg(from_lat, from_lng, to_lat, to_lng, limit=50)
            if category in ("gas", "food"):
                cands = [c for c in cands if c.get("category") == category]
            cands = cands[:limit]
            return self._json(
                200,
                {
                    "ok": True,
                    "note": "Gas & food from the configured pit-stop list · ranked by approx. detour from the A→B road corridor.",
                    "from": {"lat": from_lat, "lng": from_lng},
                    "to": {"lat": to_lat, "lng": to_lng},
                    "candidates": cands,
                    "reason": reason,
                    "legMiles": round(leg_mi, 2),
                    "corridorMiles": round(width_mi, 2),
                },
            )

        if path == "/api/receipts":
            recs = load_receipts()
            return self._json(200, {"receipts": recs, "stats": receipt_stats(recs)})

        if path == "/api/handoffs":
            pending = list_pending()
            done = list_done()
            return self._json(
                200,
                {
                    "pending": pending,
                    "done": done,
                    "pendingCount": len(pending),
                    "doneCount": len(done),
                    "watchPath": display_path(PENDING),
                },
            )

        m = re.fullmatch(r"/api/handoffs/([A-Za-z0-9_-]+)", path)
        if m:
            job_id = m.group(1)
            job, _, loc = find_job(job_id)
            if not job:
                return self._json(404, {"error": "not_found", "id": job_id})
            out = dict(job)
            out["_location"] = loc
            return self._json(200, out)

        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/api/handoffs":
            data = self._read_json()
            if not data:
                return self._json(400, {"error": "invalid_json"})

            action = data.get("action")
            if action not in ALLOWED_ACTIONS:
                return self._json(
                    400,
                    {
                        "error": "invalid_action",
                        "allowed": list(ALLOWED_ACTIONS.keys()),
                    },
                )

            target = data.get("targetBot") or ALLOWED_ACTIONS[action]
            if target != ALLOWED_ACTIONS[action]:
                return self._json(
                    400,
                    {
                        "error": "target_mismatch",
                        "expected": ALLOWED_ACTIONS[action],
                        "got": target,
                    },
                )

            payload = data.get("payload")
            if payload is None:
                payload = {}
            if not isinstance(payload, dict):
                return self._json(400, {"error": "payload_must_be_object"})

            job_id = data.get("id") or uuid.uuid4().hex[:12]
            if not safe_id(str(job_id)):
                job_id = uuid.uuid4().hex[:12]

            existing, _, _ = find_job(job_id)
            if existing:
                job_id = uuid.uuid4().hex[:12]

            created = data.get("createdAt") or utc_now()
            job = {
                "id": job_id,
                "createdAt": created,
                "action": action,
                "targetBot": target,
                "payload": payload,
                "status": "pending",
            }

            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            filename = f"{stamp}-{action}-{job_id}.json"
            out_path = PENDING / filename
            out_path.write_text(json.dumps(job, indent=2) + "\n", encoding="utf-8")
            return self._json(201, job)

        if path == "/api/pricing/analyze":
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 30_000_000:
                return self._json(400, {"error": "bad_size"})
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                import vision
                return self._json(200, vision.analyze(list(data.get("images") or [])))
            except Exception as e:
                return self._json(500, {"error": "vision_failed", "detail": str(e)[:300]})

        if path == "/api/receipts/analyze":
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 30_000_000:
                return self._json(400, {"error": "bad_size"})
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                import receipt
                return self._json(200, receipt.analyze(list(data.get("images") or [])))
            except Exception as e:
                return self._json(500, {"error": "ocr_failed", "detail": str(e)[:300]})

        if path == "/api/location-ratings":
            data = self._read_json()
            if not data or not isinstance(data, dict) or not str(data.get("key") or "").strip():
                return self._json(400, {"error": "key_required"})
            key = str(data["key"])[:200]
            rt = load_ratings()
            cur = rt.get(key, {})
            if "rating" in data:
                try:
                    r = int(data.get("rating") or 0)
                except Exception:
                    r = 0
                cur["rating"] = r if 1 <= r <= 5 else None
            if "notes" in data:
                cur["notes"] = str(data.get("notes") or "")[:2000]
            if "store" in data:
                cur["store"] = str(data.get("store") or "")[:200]
            cur["updatedAt"] = utc_now()
            rt[key] = cur
            save_ratings(rt)
            recs = load_receipts()
            save_receipts(recs)
            return self._json(200, {"ok": True, "stats": receipt_stats(recs)})

        if path == "/api/receipts":
            data = self._read_json()
            if not data or not isinstance(data, dict):
                return self._json(400, {"error": "invalid_json"})
            recs = load_receipts()
            rid = str(data.get("id") or uuid.uuid4().hex[:12])
            if not safe_id(rid):
                rid = uuid.uuid4().hex[:12]
            data["id"] = rid
            data.setdefault("savedAt", utc_now())
            recs = [r for r in recs if r.get("id") != rid] + [data]
            save_receipts(recs)
            return self._json(201, {"receipt": data, "stats": receipt_stats(recs)})

        if path == "/api/uploads":
            import base64
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 8_000_000:
                return self._json(400, {"error": "bad_size"})
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                return self._json(400, {"error": "invalid_json"})
            batch = str(data.get("batch") or uuid.uuid4().hex[:12])
            if not safe_id(batch):
                batch = uuid.uuid4().hex[:12]
            name = re.sub(r"[^A-Za-z0-9._-]", "_", str(data.get("name") or "photo.jpg"))[:80]
            b64 = str(data.get("data") or "")
            if "," in b64:
                b64 = b64.split(",", 1)[1]
            try:
                raw = base64.b64decode(b64)
            except Exception:
                return self._json(400, {"error": "bad_base64"})
            d = DATA_DIR / "uploads" / "pricing" / batch
            d.mkdir(parents=True, exist_ok=True)
            out = d / name
            out.write_bytes(raw)
            return self._json(201, {"batch": batch, "name": name, "path": f"uploads/pricing/{batch}/{name}", "url": f"/uploads/pricing/{batch}/{name}"})

        m = re.fullmatch(r"/api/handoffs/([A-Za-z0-9_-]+)/complete", path)
        if m:
            job_id = m.group(1)
            data = self._read_json() or {}
            job, pending_path, loc = find_job(job_id)
            if not job:
                return self._json(404, {"error": "not_found", "id": job_id})

            status = data.get("status", "completed")
            if status not in ("completed", "failed"):
                return self._json(400, {"error": "status_must_be_completed_or_failed"})

            result = {
                "id": job_id,
                "createdAt": job.get("createdAt"),
                "completedAt": utc_now(),
                "action": job.get("action"),
                "targetBot": job.get("targetBot"),
                "payload": job.get("payload", {}),
                "status": status,
                "summary": data.get("summary") or "",
                "result": data.get("result"),
            }

            done_path = DONE / f"{job_id}.json"
            done_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

            if loc == "pending" and pending_path and pending_path.exists():
                try:
                    pending_path.unlink()
                except OSError:
                    pass

            return self._json(200, result)

        self._json(404, {"error": "not_found"})

    def do_DELETE(self):
        m = re.fullmatch(r"/api/receipts/([A-Za-z0-9_-]+)", urlparse(self.path).path.rstrip("/"))
        if not m:
            return self._json(404, {"error": "not_found"})
        recs = [r for r in load_receipts() if r.get("id") != m.group(1)]
        save_receipts(recs)
        return self._json(200, {"ok": True, "stats": receipt_stats(recs)})

    def log_message(self, fmt: str, *args):
        print(f"[polo-pipeline] {self.address_string()} {fmt % args}")


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Polo Pipeline local bridge")
    ap.add_argument("--demo", action="store_true", help="seed the data dir with the EXAMPLE fixtures")
    args = ap.parse_args()
    ensure_dirs()
    if args.demo or os.environ.get("POLO_DEMO") == "1":
        seed_examples()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Polo Pipeline bridge: http://{HOST}:{PORT}/")
    print(f"Data dir: {DATA_DIR}")
    print(f"Watching pending jobs: {PENDING}")
    print("Proxies: /api/geocode/*  /api/route  /api/pitstops/candidates")
    print("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
