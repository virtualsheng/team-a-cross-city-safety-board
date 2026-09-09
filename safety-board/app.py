#!/usr/bin/env python3
"""Cross-city vehicle & pedestrian safety board — Pack A (Bangkok, Dublin, London)."""

from __future__ import annotations

import hashlib
import json
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PACK_A_CAMERAS = {
    "bangkok_cam-1": "bangkok",
    "bangkok_cam-2": "bangkok",
    "surveillance_1": "dublin",
    "london_surveillance_1": "london",
}
CITIES = ("bangkok", "dublin", "london")
DEFAULT_QUERY = "person close to a moving vehicle"
DEFAULT_MIN_SIM = 0.08
PER_CAM_TOP_K = 6
YOLO_LABELS = {"person", "car", "motorcycle", "bus", "truck", "bicycle"}
HOST = "127.0.0.1"
PORT = 8765
STATIC = Path(__file__).resolve().parent / "static"
POSTER_DIR = Path("/tmp/safety-board-posters")
SEARCH_CACHE_PATH = Path("/tmp/safety-board-search-cache.json")

_token = None
_backend = None
_env = {}
_poster_sem = threading.Semaphore(4)
_search_lock = threading.Lock()
_search_cache = {}
_inflight = {}


def load_team_config():
    configs = sorted(Path("/config").glob("*.config"))
    if len(configs) != 1:
        raise RuntimeError("expected exactly one /config/*.config")
    env = {}
    for line in configs[0].read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def login():
    global _token, _backend, _env
    _env = load_team_config()
    _backend = _env["INGRESS_URL"].rstrip("/")
    body = json.dumps({"username": _env["USERNAME"], "password": _env["PASSWORD"]}).encode()
    req = urllib.request.Request(
        f"{_backend}/api/v1/auth/login",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        _token = json.load(r)["access_token"]
    return _token


def api_json(method, path, payload=None, query=None, retries=4):
    global _token
    if _token is None:
        login()
    last = (502, {"detail": "unavailable"})
    for attempt in range(retries):
        url = _backend + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None if payload is None else json.dumps(payload).encode()
        headers = {"Authorization": f"Bearer {_token}", "User-Agent": "safety-board/1.0"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                raw = r.read()
                return r.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            body = e.read()
            if e.code == 401:
                login()
                continue
            if e.code in (502, 503) and attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                last = (e.code, {"detail": "upstream busy, retrying"})
                continue
            try:
                return e.code, json.loads(body.decode())
            except Exception:
                return e.code, {"detail": body.decode()[:2000]}
        except urllib.error.URLError as e:
            last = (502, {"detail": str(e)})
            time.sleep(2)
    return last


def neighbor_sources(timeline, best_n):
    by_n = {t.get("segment_number"): t.get("source") for t in (timeline or [])}
    return {
        "before": by_n.get(best_n - 1),
        "event": by_n.get(best_n),
        "after": by_n.get(best_n + 1),
    }


def parse_counts(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


def city_for(row):
    cam = row.get("camera_id")
    if cam in PACK_A_CAMERAS:
        return PACK_A_CAMERAS[cam]
    loc = (row.get("location") or "").lower()
    if loc in PACK_A_CAMERAS.values():
        return loc
    return None


def pack_a_row(row):
    return row.get("camera_id") in PACK_A_CAMERAS


def rank_cameras(results):
    by_cam = {}
    for r in results:
        cam = r.get("camera_id")
        if cam not in PACK_A_CAMERAS:
            continue
        counts = parse_counts(r.get("object_counts"))
        slot = by_cam.setdefault(
            cam,
            {
                "camera_id": cam,
                "location": PACK_A_CAMERAS[cam],
                "hits": 0,
                "person": 0,
                "car": 0,
                "motorcycle": 0,
                "bus": 0,
                "truck": 0,
                "best_score": 0.0,
            },
        )
        slot["hits"] += 1
        slot["best_score"] = max(slot["best_score"], r.get("similarity_score") or 0)
        for k in ("person", "car", "motorcycle", "bus", "truck"):
            slot[k] += int(counts.get(k) or 0)
    rows = list(by_cam.values())
    rows.sort(key=lambda x: (x["hits"], x["person"] + x["car"], x["best_score"]), reverse=True)
    return rows


def to_hit(chunk, camera_id=None, location=None, counts=None):
    best_n = chunk.get("best_segment_number") or 1
    cam = camera_id or chunk.get("camera_id")
    loc = location or city_for(chunk) or PACK_A_CAMERAS.get(cam)
    return {
        "filename": chunk.get("filename"),
        "original_video": chunk.get("original_video"),
        "similarity_score": chunk.get("similarity_score"),
        "best_match_start_sec": chunk.get("best_match_start_sec"),
        "best_match_end_sec": chunk.get("best_match_end_sec"),
        "best_segment_number": best_n,
        "matched_segment_count": chunk.get("matched_segment_count"),
        "reasoning_content": chunk.get("reasoning_content"),
        "preview_source": chunk.get("preview_source"),
        "camera_id": cam,
        "location": loc,
        "object_counts": counts or {},
        "triptych": neighbor_sources(chunk.get("timeline"), best_n),
        "radio": radio_line(chunk.get("reasoning_content")),
        "scene": scene_tag(counts or {}),
        "bus_share": chunk.get("_bus_area"),
        "car_share": chunk.get("_car_area"),
    }


def radio_line(text):
    first = (text or "").strip().split(".")[0].strip()
    if not first:
        return ""
    return first[:180]


def scene_tag(counts):
    people = int(counts.get("person") or 0)
    wheels = sum(int(counts.get(k) or 0) for k in ("car", "motorcycle", "bus", "truck", "bicycle"))
    buses = int(counts.get("bus") or 0)
    if people and wheels:
        return "people among vehicles"
    if people:
        return "people, little traffic"
    if wheels:
        return "vehicles, few people"
    return "sparse detections"


def city_signatures(grouped):
    out = {}
    for city in CITIES:
        rows = grouped.get(city) or []
        mix = {"person": 0, "car": 0, "motorcycle": 0, "bus": 0, "truck": 0}
        scores = []
        for h in rows:
            scores.append(h.get("similarity_score") or 0)
            c = h.get("object_counts") or {}
            for k in mix:
                mix[k] += int(c.get(k) or 0)
        top = rows[0] if rows else None
        out[city] = {
            "clips": len(rows),
            "mix": mix,
            "top_score": max(scores) if scores else 0,
            "lead": (top or {}).get("radio") or "",
            "scene": (top or {}).get("scene") or "",
        }
    return out


def briefing_row(grouped):
    cards = []
    for city in CITIES:
        rows = grouped.get(city) or []
        if not rows:
            cards.append({"city": city, "empty": True})
            continue
        h = rows[0]
        cards.append(
            {
                "city": city,
                "empty": False,
                "filename": h.get("filename"),
                "camera_id": h.get("camera_id"),
                "similarity_score": h.get("similarity_score"),
                "radio": h.get("radio"),
                "scene": h.get("scene"),
                "preview": (h.get("triptych") or {}).get("event") or h.get("preview_source"),
                "object_counts": h.get("object_counts") or {},
            }
        )
    return cards


def score_ladder(grouped, min_sim):
    scores = []
    for city in CITIES:
        for h in grouped.get(city) or []:
            scores.append(
                {
                    "city": city,
                    "score": h.get("similarity_score") or 0,
                    "filename": h.get("filename"),
                }
            )
    scores.sort(key=lambda x: x["score"], reverse=True)
    return {
        "used": min_sim,
        "spec_empty_at": 0.35,
        "points": scores,
    }


def cache_key(query, min_sim):
    return json.dumps({"q": query, "s": round(float(min_sim), 4), "v": 10}, sort_keys=True)


def load_search_cache():
    global _search_cache
    if SEARCH_CACHE_PATH.exists():
        try:
            _search_cache = json.loads(SEARCH_CACHE_PATH.read_text())
        except Exception:
            _search_cache = {}


def save_search_cache():
    try:
        SEARCH_CACHE_PATH.write_text(json.dumps(_search_cache))
    except Exception:
        pass


def poster_path_for(source):
    POSTER_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(source.encode()).hexdigest()[:32]
    return POSTER_DIR / f"{key}.jpg"


def ensure_poster(source):
    path = poster_path_for(source)
    if path.exists() and path.stat().st_size >= 200:
        return path
    if _token is None:
        login()
    url = (
        f"{_backend}/api/v1/videos/stream?"
        + urllib.parse.urlencode({"source": source, "token": _token})
    )
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        "0.4",
        "-i",
        url,
        "-frames:v",
        "1",
        "-vf",
        "scale=480:-2",
        "-q:v",
        "5",
        "-y",
        str(path),
    ]
    with _poster_sem:
        proc = subprocess.run(cmd, timeout=50, capture_output=True)
        if proc.returncode != 0 or not path.exists() or path.stat().st_size < 200:
            login()
            cmd[cmd.index("-i") + 1] = (
                f"{_backend}/api/v1/videos/stream?"
                + urllib.parse.urlencode({"source": source, "token": _token})
            )
            proc = subprocess.run(cmd, timeout=50, capture_output=True)
            if proc.returncode != 0:
                print(f"[board] poster ffmpeg {proc.stderr[:200]!r}", flush=True)
                return None
    return path if path.exists() and path.stat().st_size >= 200 else None


def prefetch_posters(payload):
    sources = []
    for hits in (payload.get("grouped") or {}).values():
        for h in hits:
            tri = h.get("triptych") or {}
            for key in ("before", "event", "after"):
                if tri.get(key):
                    sources.append(tri[key])
            if h.get("preview_source"):
                sources.append(h["preview_source"])
    seen = []
    for src in sources:
        if src not in seen:
            seen.append(src)

    def worker(src):
        try:
            ensure_poster(src)
        except Exception as e:
            print(f"[board] poster prefetch {e}", flush=True)

    for src in seen:
        threading.Thread(target=worker, args=(src,), daemon=True).start()


def search_one_camera(cam, loc, query, min_sim):
    payload = {
        "query": query,
        "top_k": PER_CAM_TOP_K,
        "llm_top_n": 1,
        "min_similarity": min_sim,
        "metadata_filters": {"camera_id": cam},
        "include_public": True,
    }
    if "zebra" in query.lower() or "bus wait" in query.lower():
        payload["hybrid_text_weight"] = 0.8
        payload["top_k"] = 12
    code, data = api_json("POST", "/api/v1/search", payload)
    return cam, loc, code, data


def is_zebra_query(query):
    q = (query or "").lower()
    return "zebra" in q or "bus wait" in q


def caption_has_crossing(text):
    t = (text or "").lower()
    return any(
        w in t
        for w in (
            "zebra",
            "crosswalk",
            "crosswalks",
            "crossing the",
            "crossing at",
            "pedestrian crossing",
            "on the crossing",
            "crossroads",
            "cross the",
        )
    )


def event_source(chunk):
    best_n = chunk.get("best_segment_number") or 1
    for item in chunk.get("timeline") or []:
        if item.get("segment_number") == best_n and item.get("source"):
            return item.get("source")
    return chunk.get("preview_source")


def bbox_area_frac(bbox, shape):
    if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
        return 0.0
    x1, y1, x2, y2 = map(float, bbox[:4])
    w, h = abs(x2 - x1), abs(y2 - y1)
    if isinstance(shape, (list, tuple)) and len(shape) >= 2:
        frame_h, frame_w = float(shape[0]), float(shape[1])
        if frame_w * frame_h:
            return (w * h) / (frame_w * frame_h)
    if max(x1, y1, x2, y2) <= 1.5:
        return w * h
    return 0.0


def detection_prominence(det):
    shape = (det or {}).get("video_shape")
    bus_a = car_a = 0.0
    for fr in (det or {}).get("frames") or []:
        for d in fr.get("detections") or []:
            lab = d.get("label") or ""
            area = bbox_area_frac(d.get("bbox"), shape)
            if lab == "bus":
                bus_a = max(bus_a, area)
            elif lab == "car":
                car_a = max(car_a, area)
    counts = parse_counts((det or {}).get("object_counts"))
    return bus_a, car_a, int(counts.get("person") or 0), counts


def fetch_detections(source):
    if not source:
        return {}
    code, data = api_json("GET", "/api/v1/videos/detections", query={"source": source})
    if code >= 400 or not isinstance(data, dict):
        return {}
    return data


def prominent_bus(bus_a, car_a):
    # Distant/background buses are ~1% of the frame; a bus the viewer can
    # actually see is larger than the cars in the same shot.
    return bus_a >= 0.025 and bus_a >= car_a


def chunk_caption(chunk):
    parts = [chunk.get("reasoning_content") or ""]
    for item in chunk.get("timeline") or []:
        parts.append(item.get("reasoning_content") or "")
    return " ".join(parts)


def diversify(rows, limit=4):
    by_cam = {}
    for h in rows:
        by_cam.setdefault(h.get("camera_id") or "unknown", []).append(h)
    for cam in by_cam:
        by_cam[cam].sort(key=lambda x: x.get("similarity_score") or 0, reverse=True)
    cams = sorted(by_cam, key=lambda c: by_cam[c][0].get("similarity_score") or 0, reverse=True)
    out = []
    idx = {c: 0 for c in cams}
    while len(out) < limit and any(idx[c] < len(by_cam[c]) for c in cams):
        progressed = False
        for c in cams:
            if idx[c] < len(by_cam[c]) and len(out) < limit:
                out.append(by_cam[c][idx[c]])
                idx[c] += 1
                progressed = True
        if not progressed:
            break
    return out


def run_pack_search(query, min_sim, use_cache=True):
    key = cache_key(query, min_sim)
    with _search_lock:
        if use_cache and key in _search_cache:
            payload = dict(_search_cache[key])
            payload["cached"] = True
            prefetch_posters(payload)
            return payload
        waiter = _inflight.get(key)
        mine = False
        if waiter is None:
            waiter = threading.Event()
            _inflight[key] = waiter
            mine = True
    if not mine:
        waiter.wait(timeout=180)
        with _search_lock:
            hit = _search_cache.get(key)
        if hit:
            payload = dict(hit)
            payload["cached"] = True
            return payload

    from concurrent.futures import ThreadPoolExecutor, as_completed

    all_chunks = []
    all_results = []
    errors = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [
            pool.submit(search_one_camera, cam, loc, query, min_sim)
            for cam, loc in PACK_A_CAMERAS.items()
        ]
        for fut in as_completed(futs):
            cam, loc, code, data = fut.result()
            if code >= 400:
                errors[cam] = data.get("detail") or code
                continue
            results = list(data.get("results") or [])
            by_ov = {}
            for r in results:
                r["camera_id"] = r.get("camera_id") or cam
                r["location"] = r.get("location") or loc
                by_ov[r.get("original_video")] = r
            for c in data.get("chunk_results") or []:
                meta = by_ov.get(c.get("original_video")) or {}
                c["camera_id"] = c.get("camera_id") or meta.get("camera_id") or cam
                c["location"] = c.get("location") or meta.get("location") or loc
                c["_city"] = loc
                c["_counts"] = parse_counts(meta.get("object_counts"))
                c["_caption"] = chunk_caption(c)
                all_chunks.append(c)
            all_results.extend(results)

    zebra = is_zebra_query(query)
    if zebra:
        need = [c for c in all_chunks if caption_has_crossing(c.get("reasoning_content"))]
        with ThreadPoolExecutor(max_workers=6) as pool:
            futs = {pool.submit(fetch_detections, event_source(c)): c for c in need}
            for fut in as_completed(futs):
                c = futs[fut]
                bus_a, car_a, person_n, det_counts = detection_prominence(fut.result() or {})
                c["_bus_area"] = bus_a
                c["_car_area"] = car_a
                c["_person_n"] = person_n
                if det_counts:
                    c["_counts"] = det_counts
        all_chunks = [
            c
            for c in need
            if prominent_bus(c.get("_bus_area") or 0, c.get("_car_area") or 0)
            and (c.get("_person_n") or 0) >= 1
        ]
        all_results = [r for r in all_results if caption_has_crossing(r.get("reasoning_content"))]

    hits = [
        to_hit(
            c,
            camera_id=c.get("camera_id"),
            location=c.get("_city") or city_for(c),
            counts=c.get("_counts"),
        )
        for c in all_chunks
    ]
    grouped = {city: [] for city in CITIES}
    for h in hits:
        grouped.setdefault(h["location"] or "other", []).append(h)
    for city in grouped:
        grouped[city] = diversify(grouped[city], 4)

    bits = []
    for city in CITIES:
        if grouped.get(city):
            cap = (grouped[city][0].get("reasoning_content") or "").split(".")[0]
            if cap:
                bits.append(f"{city.title()}: {cap}.")
    qlow = (query or "").lower()
    zebra_q = "zebra" in qlow or "bus wait" in qlow
    if zebra_q and errors and not bits:
        synthesis = "Search backend was busy; try Zebra / bus again in a few seconds."
    elif zebra_q and not bits:
        synthesis = (
            "No clip has a bus large in frame at a crossing. "
            "Caption-only matches (a car up close, bus in the far background) were dropped."
        )
    elif bits:
        synthesis = " ".join(bits)
    else:
        synthesis = None
    payload = {
        "query": query,
        "min_similarity": min_sim,
        "hit_count": sum(len(v) for v in grouped.values()),
        "segment_hits": len(all_results),
        "llm_synthesis": synthesis,
        "grouped": grouped,
        "ranking": rank_cameras(all_results),
        "signatures": city_signatures(grouped),
        "briefing": briefing_row(grouped),
        "ladder": score_ladder(grouped, min_sim),
        "errors": errors,
        "cached": False,
    }
    with _search_lock:
        if not (errors and not hits):
            _search_cache[key] = {k: v for k, v in payload.items() if k != "cached"}
            save_search_cache()
        _inflight.pop(key, None)
        waiter.set()
    prefetch_posters(payload)
    return payload


def warmup():
    try:
        print("[board] warming search cache…", flush=True)
        run_pack_search(DEFAULT_QUERY, DEFAULT_MIN_SIM, use_cache=True)
        run_pack_search("pedestrians near cars at an intersection", 0.2, use_cache=True)
        run_pack_search("pedestrians on a zebra crossing while a bus waits", 0.15, use_cache=True)
        print("[board] warmup done", flush=True)
    except Exception as e:
        print(f"[board] warmup failed {e}", flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"[board] {self.address_string()} {fmt % args}", flush=True)

    def _send_json(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._serve_static("index.html", "text/html; charset=utf-8")
            return
        if parsed.path == "/api/config":
            self._send_json(
                200,
                {
                    "cities": list(CITIES),
                    "cameras": PACK_A_CAMERAS,
                    "default_query": DEFAULT_QUERY,
                    "min_similarity": DEFAULT_MIN_SIM,
                    "backend": _backend,
                },
            )
            return
        if parsed.path == "/api/detections":
            qs = urllib.parse.parse_qs(parsed.query)
            source = (qs.get("source") or [None])[0]
            if not source:
                self._send_json(400, {"detail": "source required"})
                return
            code, data = api_json("GET", "/api/v1/videos/detections", query={"source": source})
            if code == 404:
                self._send_json(200, {"frames": [], "missing": True})
                return
            if code < 400 and isinstance(data, dict):
                frames = []
                for i, fr in enumerate(data.get("frames") or []):
                    if i % 5:
                        continue
                    dets = [
                        d
                        for d in (fr.get("detections") or [])
                        if (d.get("label") or "") in YOLO_LABELS
                    ][:10]
                    frames.append({"time_sec": fr.get("time_sec"), "detections": dets})
                data = {
                    "segment_source": data.get("segment_source") or source,
                    "video_shape": data.get("video_shape"),
                    "fps": data.get("fps"),
                    "object_counts": data.get("object_counts"),
                    "frames": frames,
                }
            self._send_json(code, data)
            return
        if parsed.path == "/api/poster":
            self._poster(parsed)
            return
        if parsed.path == "/api/stream":
            self._proxy_stream(parsed)
            return
        self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if parsed.path == "/api/search":
            self._search(body)
            return
        self.send_error(404)

    def _serve_static(self, name, ctype):
        path = STATIC / name
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _search(self, body):
        query = (body.get("query") or DEFAULT_QUERY).strip()
        min_sim = float(body.get("min_similarity") or DEFAULT_MIN_SIM)
        payload = run_pack_search(query, min_sim, use_cache=True)
        self._send_json(200, payload)

    def _poster(self, parsed):
        qs = urllib.parse.parse_qs(parsed.query)
        source = (qs.get("source") or [None])[0]
        if not source:
            self._send_json(400, {"detail": "source required"})
            return
        path = ensure_poster(source)
        if not path:
            self.send_error(502)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def _proxy_stream(self, parsed):
        qs = urllib.parse.parse_qs(parsed.query)
        source = (qs.get("source") or [None])[0]
        if not source:
            self._send_json(400, {"detail": "source required"})
            return
        if _token is None:
            login()
        url = (
            f"{_backend}/api/v1/videos/stream?"
            + urllib.parse.urlencode({"source": source, "token": _token})
        )
        range_hdr = self.headers.get("Range")
        headers = {"User-Agent": "safety-board/1.0"}
        if range_hdr:
            headers["Range"] = range_hdr
        req = urllib.request.Request(url, headers=headers)
        try:
            try:
                upstream = urllib.request.urlopen(req, timeout=120)
            except urllib.error.HTTPError as e:
                if e.code != 401:
                    print(f"[board] stream upstream {e.code} {source}", flush=True)
                    self.send_error(e.code)
                    return
                login()
                url = (
                    f"{_backend}/api/v1/videos/stream?"
                    + urllib.parse.urlencode({"source": source, "token": _token})
                )
                req = urllib.request.Request(url, headers=headers)
                upstream = urllib.request.urlopen(req, timeout=120)
        except Exception as e:
            print(f"[board] stream failed {e} {source}", flush=True)
            try:
                self.send_error(502, str(e))
            except Exception:
                pass
            return
        try:
            with upstream:
                self.send_response(upstream.status)
                self.send_header("Content-Type", "video/mp4")
                for h in ("Content-Length", "Content-Range", "Accept-Ranges"):
                    v = upstream.headers.get(h)
                    if v:
                        self.send_header(h, v)
                self.send_header("Cache-Control", "private, max-age=60")
                self.end_headers()
                while True:
                    chunk = upstream.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return


def main():
    ssl._create_default_https_context = ssl._create_unverified_context
    login()
    load_search_cache()
    threading.Thread(target=warmup, daemon=True).start()
    print(f"Safety board on http://{HOST}:{PORT}  cities={list(CITIES)}", flush=True)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.allow_reuse_address = True
    httpd.serve_forever()


if __name__ == "__main__":
    main()
