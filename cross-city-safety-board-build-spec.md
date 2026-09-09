# Cross-City Vehicle & Pedestrian Safety Board — Build Spec
**VAST Builders Challenge — Build Day dry run**
Built entirely on the pre-loaded, pre-indexed corpus — Pack A (Vehicle & Pedestrian), officially supported path

## 0. Why this pivot (context for whoever picks this up)

Two earlier directions were explored and dropped before this one:
1. A live NYC 511NY traffic-incident puller — dropped because 511NY's developer key process is broken (self-service option removed from the account page; only a slow manual-approval email path remains).
2. A live London TfL JamCam puller — technically working (real API key, verified endpoints), but the hackathon's own rules make it a poor fit for the *judged* deliverable: `HACKATHON_GUIDELINES.md`'s `ingest/reingest-videos` skill states **"Never upload a new file, run batch sync, copy an object manually, or write directly to S3"** — re-ingest is the only sanctioned way to touch the archive, and whether outside footage is even allowed is an open question the hackathon docs themselves haven't answered yet.

This version drops external data entirely. Each team's VSS archive already comes **pre-loaded with a real, indexed multi-city corpus** — that's the actual supported path, it's what the judging rubric is built around, and it needs zero API keys, zero auth setup, zero external dependencies. Confirmed working per the hackathon's own docs (not tested live yet — see §8 action items).

## 0.5 Confirmed Team-A environment (real values)

Team assignment and VM access are confirmed. **Credentials are intentionally not repeated in this doc** — they're already in `/config/team-a.config` and the k8s secrets on the VM, which is where every skill in this repo expects to read them from (never typed into prompts, never committed). If you're pasting this spec into another AI session or tool, that's exactly why: nothing here needs a password, key, or token to be useful.

**VM access:** `workshop.thecosmoslabs.com/labs/builders-challenge` (page password shared separately), select **Team A** once in.

**Confirmed non-secret config** (useful for writing accurate Cursor prompts and understanding pipeline behavior):

| Setting | Value |
|---|---|
| Team / namespace | `team-a` |
| `INGRESS_URL` (backend/UI) | `http://video-lab-team-a.cosmos.vastdata.com` |
| DataEngine UI / tenant | `10.146.15.201`, tenant `builder-series-poc` |
| S3 buckets | `team-a-vss-chunks` (chunks), `team-a-vss-chunks-segments` (segments) |
| VastDB | bucket `team-a-vss-db`, schema `vss-schema`, collection `vss-collection`, prompts collection `vss-prompts-events` |
| GPU host | `166.19.38.112` — Reason2 `:8001`, YOLO `:8002`, Embed1 `:8003` (matches the general hackathon docs exactly) |
| Cosmos Reason2 model | `nvidia/cosmos-reason2-8b`, temperature `0.2`, max tokens `6000` (ingest) / `2000` (synthesis) |
| Cosmos Embed1 model | `nvidia/cosmos-embed1`, dimensions `256` |
| YOLO model | `yolo11s.pt`, confidence threshold `0.4` |
| Segment duration | `5s` per clip, output `mp4`/`libx264` |
| Hybrid search text weight | `0.6` (caption vs. visual blend — skews toward text/caption matching over pure visual similarity) |
| Default ingest scenario | `general` (not `traffic` — worth knowing when deciding whether Pack A needs a re-ingest with a more targeted scenario/prompt, per §4's optional step) |
| Max upload size | `100 MB` per video |
| Display timezone | UTC (keep this in mind when reading/reporting timestamps in the demo) |

This confirms the earlier hackathon docs were accurate for this specific deployment (the DataEngine login URL and tenant name match exactly), so the rest of this spec's API details can be trusted as-is.

## 1. One-liner

A cross-city board that answers one anchor question — *"show me every clip, from any camera in any city, where a person is close to a moving vehicle"* — pulling hits from Bangkok, Dublin, and London traffic/crowd cameras into one grouped, browsable result set with before/event/after clips, then layers a simple "which camera has the most near-miss-style moments" ranking on top.

This is the hackathon guide's own suggested demo (`HACKATHON_GUIDELINES.md`, workflow example 2, "Cross-camera 'person near vehicle' board") — building it as specified is a safe bet against the judging rubric rather than a novel risk.

## 2. What you already have (no setup required)

- **Pack A — Vehicle & Pedestrian**, already indexed in your team's archive:

  | Folder | City | Category | `camera_id` |
  |---|---|---|---|
  | `bangkok_intersection2` | bangkok | Traffic | `bangkok_cam-1` |
  | `bankgog_intersection1` *(sic, typo in source)* | bangkok | Traffic | `bangkok_cam-2` |
  | `dublin_surv_cam` | dublin | Crowds | `surveillance_1` |
  | `london_surv_cam` | london | Crowds | `london_surveillance_1` |

- A **VM with Cursor pre-loaded**, this repo cloned, credentials already in your environment as env vars (`/config/<team>.config`) — no keys to type.
- Retrieval skills that do the API work for you: `login`, `search`, `list-metadata`, `videos`, `dashboard`, `agent-qa`, `suggest-prompts`.
- Ingest skill for sharpening what's searchable: `reingest-videos` (custom prompt/scenario/metadata on existing segments — this is the *only* ingest action available, and it's all you need here).

## 3. Architecture

```
Pack A archive (pre-indexed)
        │
        ├─ (optional) reingest-videos with a safety-focused custom prompt ─→ sharper captions for this use case
        │
        ▼
POST /api/v1/search  "person close to a moving vehicle"
   grouped by city / camera_id (chunk_results[])
        │
        ▼
Small web app:
  - grouped hit list (Bangkok / Dublin / London)
  - triptych per hit: before (~5s) / event / after (~5s) via /videos/stream
  - optional YOLO bbox overlay via /videos/detections
  - a simple "camera ranking" panel: hit count + object counts per camera_id
```

## 4. Confirmed API details (from the actual skill docs, not guessed)

### Auth — `retrieval/login`
```bash
mapfile -t TEAM_CONFIGS < <(find /config -maxdepth 1 -type f -name '*.config' | sort)
TEAM_CONFIG="${TEAM_CONFIGS[0]}"; set -a && source "$TEAM_CONFIG" && set +a
BACKEND="$INGRESS_URL"
TOKEN=$(curl -s -X POST "$BACKEND/api/v1/auth/login" -H "Content-Type: application/json" \
  -d "{\"username\":\"$USERNAME\",\"password\":\"$PASSWORD\"}" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
```
Verify with `GET /api/v1/auth/me`. Re-login on any 401 — don't assume the token is long-lived.

### Search — `retrieval/search`
```bash
curl -s -X POST "$BACKEND/api/v1/search" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "query": "person close to a moving vehicle",
    "top_k": 30,
    "llm_top_n": 5,
    "min_similarity": 0.35,
    "metadata_filters": {},
    "include_public": true
  }'
```
- `results[]` = individual segment hits; `chunk_results[]` = grouped by parent video (`original_video`, `best_match_start_sec/end_sec`, `preview_source`) — **use `chunk_results` for the board**, it's already the "jump to moment" shape you want.
- Raise `min_similarity` toward 0.4–0.5 if results feel noisy across three cities' worth of footage.
- There's no dedicated `/locations` or `/cameras` route — get valid `city`/`camera_id` values from `list-metadata` first (below), don't guess spellings (note the `bankgog_intersection1` typo in the source folder name — the `city`/`camera_id` metadata values themselves may or may not carry that typo; check via `list-metadata` rather than assuming).

### Discover filter values — `retrieval/list-metadata`
```bash
curl -s "$BACKEND/api/v1/metadata/schema" -H "Authorization: Bearer $TOKEN"
curl -s "$BACKEND/api/v1/metadata/values?field=city" -H "Authorization: Bearer $TOKEN"
curl -s "$BACKEND/api/v1/metadata/values?field=camera_id" -H "Authorization: Bearer $TOKEN"
```
Do this **before** building filters into the app — confirms exact `city`/`camera_id` values rather than assuming they match the folder names in the doc table above.

### Playback (for the triptych) — `retrieval/videos`
```bash
# token goes in the query string here, not the header (browser <video> limitation)
"$BACKEND/api/v1/videos/stream?source=<segment-s3-uri>&token=$TOKEN"
```
Build before/event/after by offsetting from `chunk_results[].best_match_start_sec` / `best_match_end_sec` by ~5s each way, clamped to the clip's actual bounds.

### Optional: object detections for the ranking panel
```bash
curl -s "$BACKEND/api/v1/videos/detections?source=<segment-s3-uri>" -H "Authorization: Bearer $TOKEN"
```
404 just means no YOLO sidecar for that segment (detector disabled or nothing detected) — not an error to chase.

### Optional: sharpen the archive first — `ingest/reingest-videos`
If early search results feel thin (remember: *"anything the prompt didn't ask about isn't in there"* — the original indexing prompt determines searchability), re-ingest Pack A's folders with a **custom prompt** aimed at this use case, e.g. *"Describe pedestrian and vehicle proximity, congestion, and any near-miss moments in this traffic camera footage."* This goes through the officially supported `dashboard/reingest` endpoint the skill wraps — never a manual S3 write. Confirm via the dashboard (`pipeline_alignment`, `recent_videos`) that it actually re-indexed before building the app on top.

## 5. Build plan (single day, rough time budget)

| Phase | Time | Output |
|---|---|---|
| 0. Setup | 30 min | VM/Cursor working, `login` + `dashboard` skill confirm you're authenticated and can see Pack A's four sources indexed |
| 1. Baseline search | 45 min | Run the anchor query as-is (`retrieval/search`), see what comes back across the three cities before touching anything |
| 2. (Optional) Re-ingest | 1 hr | If baseline results are thin, re-ingest Pack A with a safety-focused custom prompt; verify via dashboard |
| 3. App: grouped board | 2 hr | Small web app — hit list grouped by city/camera, triptych playback per hit |
| 4. App: ranking panel | 1 hr | Per-camera hit counts + object counts (from `dashboard` `objects[]` and/or `videos/detections`) — the "insight" layer beyond raw search |
| 5. Polish + demo prep | 1.5 hr | Confirm it runs from a clean start (per the submission checklist), pick 3–4 strongest cross-city hits to lead the demo with |

## 6. Demo script

1. Open with the anchor line: *"Show me every clip, from any camera in any city, where a person is close to a moving vehicle."*
2. Show the grouped board — Bangkok, Dublin, London hits side by side, same query, different cities/cameras.
3. Click into one hit, show the before/event/after triptych.
4. Show the ranking panel — which camera has the most such moments — as the "insight" beyond raw search.
5. If time allows, a live follow-up query typed on the spot (e.g. *"busiest minute in Dublin"* or *"pedestrians on a zebra crossing while a bus waits"*) to show it's not a canned demo.

## 7. Risks & mitigations

- **Thin results on the raw archive** — the corpus was indexed with whatever prompt the organizers used originally, which may not emphasize "proximity" specifically. Mitigation: the optional re-ingest step in §4/Phase 2; don't skip checking baseline results first, since re-ingesting unnecessarily burns time.
- **Metadata value mismatches** (e.g. the `bankgog_intersection1` typo, or `city`/`camera_id` not matching the doc table exactly) — always confirm via `list-metadata` before hardcoding filter values into the app.
- **Playback CORS/token issues** — `videos/stream` needs the JWT as a `?token=` query param, not a header, specifically because it's used in a browser `<video>` tag. Easy to get backwards if you copy the pattern from the other (header-based) endpoints.
- **Detections 404s** — normal for segments without YOLO sidecars; don't treat as broken.

## 8. Before Build Day — action items
- [ ] Once the VM is available (see the separate note on that — its delivery mechanism isn't finalized in the docs yet, check `#builders-challenge-dry-run`), run `login` + `dashboard` to confirm you can see Pack A's four sources and get a real feel for current indexing/quality
- [ ] Run the anchor query (`"person close to a moving vehicle"`) as a baseline before deciding whether re-ingest is worth the time
- [ ] Pull `city`/`camera_id` values from `list-metadata` rather than trusting the doc table's spelling verbatim

## 9. Don't forget — submission
There's a `submission` skill that interviews your team and writes `SUBMISSION.md` — run it early (even mid-build) to see what's still missing, not just at the end. It needs: team name/emails, a 2–3 sentence project description, tech stack (which skills/models you used), a GitLab code link, and a demo video link (**record on your own laptop, not the VM** — `Cmd+Shift+5` on Mac / `Win+G` on Windows). Four eligibility confirmations are also required and can't be inferred — someone has to answer them explicitly.
