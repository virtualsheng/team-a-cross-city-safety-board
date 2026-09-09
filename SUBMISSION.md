# Team A

## Team
| Name |
|---|
| Sheng Sheen |

## Project
A safety board for traffic operators that runs one natural-language query across Bangkok, Dublin, and London Pack A cameras, groups hits by city, and shows a briefing strip plus before/event/after clips. Built on the pre-indexed VSS archive so a judge can search, play, and compare cities without new uploads.

**Stack:** VSS retrieval (`login`, `search`, `videos` stream/detections); Cosmos Reason2 captions; Cosmos Embed1 hybrid search; YOLO11 detections; local Python HTTP app (`safety-board/app.py`) + static HTML
**Code:** https://github.com/virtualsheng/team-a-cross-city-safety-board
**Demo video:** NOT PROVIDED
**Supplementary:** none

## Feedback
The retrieval skills and pre-indexed Pack A were enough to ship a searchable board without new uploads. Re-ingest was a poor fit for a timed build: one Bangkok job stalled partway, there is no cancel API, and after a backend restart the job simply disappeared while `pending_index` stayed at 62. Hybrid search at the documented 0.35 threshold returned nothing on the spec query because ingest is `general`, not traffic-tuned; dropping min_similarity and scoping by camera was the workable path. Captions also over-claim scenes (a “bus” hit that is a car in frame), so we had to filter on YOLO box size. The starter `origin` is a shared template, and `SUBMISSION.md` is gitignored there, so a separate GitHub repo was required for judges to open the code.
