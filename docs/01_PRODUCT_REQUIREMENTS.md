# Product Requirements

## 1. Product statement

Game Highlight Finder reduces a 1–4+ hour gameplay recording to a small, ranked set of useful clips without discarding other worthwhile moments. It is a personal, local-first review assistant. The human remains the editor and final publishing authority.

## 2. Primary user journey

1. The user records gameplay normally.
2. The user runs `highlight analyze <video>` (the input-folder workflow can be added later).
3. The tool verifies prerequisites, source readability, storage, configuration, and budget.
4. Local stages inspect the recording and create a lightweight proxy and optional signals/transcript.
5. A cheap Scout analyzes overlapping windows and returns match/round and moment evidence.
6. The tool validates, clamps, merges, deduplicates, and stores the session map.
7. Candidate clips are cut from the original at high quality with pre/post-roll.
8. An optional Reviewer evaluates only candidate clips when enabled and affordable.
9. A local HTML report presents all candidates and a best-of-session shortlist.
10. The user reviews and decides what, if anything, to publish.

## 3. Functional requirements

### Ingest and local processing

- **FR-001** Accept a local video path without moving, renaming, or modifying the source.
- **FR-002** Record ffprobe metadata, source identity, duration, streams, codecs, frame rate, and dimensions.
- **FR-003** Generate a timestamp-faithful, low-bandwidth proxy and extracted audio/signals when configured.
- **FR-004** Detect missing audio and variable-frame-rate/timestamp anomalies and surface warnings.
- **FR-005** Support optional local transcription without making it a V1 hard dependency.

### Session, match, and candidate behavior

- **FR-010** Preserve the hierarchy `Session -> Match/Round -> Candidate/Story`.
- **FR-011** Permit zero matches or an unknown match when reliable segmentation is unavailable.
- **FR-012** Permit zero or many candidates; no fixed quota is imposed.
- **FR-013** Support categories: `FUNNY`, `FAIL`, `CLUTCH`, `REACTION`, `SMART_PLAY`, `FRIEND_MOMENT`, `WTF_UNEXPECTED`, `TENSION_PAYOFF`, `SKILL`, and `OTHER`.
- **FR-014** Store all candidates meeting the configured threshold; best-of-session is a derived ranking, not deletion.
- **FR-015** Preserve setup, core-event, and payoff timing so related moments can form one story candidate.
- **FR-016** Apply configurable pre-roll/post-roll and clamp final clip bounds to the source duration.

### AI behavior

- **FR-020** Use a provider/model abstraction and a cheap Gemini Flash-Lite-class Scout initially.
- **FR-021** Separate Scout responsibilities from Reviewer responsibilities.
- **FR-022** Validate all model output structurally and semantically before it affects files or commands.
- **FR-023** Process long recordings in restartable windows and reconcile overlaps.
- **FR-024** Make Reviewer optional and ensure it receives candidate clips only.
- **FR-025** Store prompts, schemas, provider/model identifiers, and usage metadata required to reproduce and benchmark a run; never store secrets.

### Extraction and reporting

- **FR-030** Extract candidates from the original source, never from the proxy.
- **FR-031** Produce deterministic, collision-resistant filenames and a manifest mapping every file to a candidate ID.
- **FR-032** Generate a self-contained local HTML report with session summary, cost, match grouping, ranking, reasons, confidence, times, durations, and local paths.
- **FR-033** Do not crop to vertical, add subtitles, generate captions, or publish in V1.

### Cost, caching, and recovery

- **FR-040** Estimate cost and reserve budget before every potentially billable request.
- **FR-041** Refuse a request if committed spend plus active reservations plus conservative projected cost exceeds the monthly hard limit.
- **FR-042** Log estimates and actual provider usage locally with provider, model, stage, session, currency assumptions, and timestamp.
- **FR-043** Cache completed work and resume from the earliest incomplete or stale stage.
- **FR-044** Support deliberate reruns with `--force-stage`, invalidating only affected downstream artifacts.
- **FR-045** Explain skipped, failed, stale, and budget-blocked stages clearly.

## 4. Non-functional requirements

- **Reliability:** atomic artifact writes, explicit state, checksums, deterministic normalization, bounded retries, and no silent partial success.
- **Cost safety:** 100 THB/month default hard budget, conservative reservation, unknown-price fail-closed behavior, and optional paid stages off by default where appropriate.
- **Privacy:** original stays local; only configured proxies/candidate clips may be uploaded; remote object IDs and deletion outcomes are recorded.
- **Performance:** stream large files, use hardware encoding when verified, avoid duplicate transcodes/uploads, and make window work independently resumable.
- **Portability:** Windows-first V1 without Windows-only domain logic; subprocess argument arrays rather than shell strings.
- **Testability:** pure domain functions around time, validation, budgeting, matching, overlap, ranking, and command construction.
- **Observability:** human-readable logs plus structured stage/error metadata; secrets and API keys redacted.

## 5. V1 scope

V1 includes a CLI, dependency doctor, ingest/ffprobe, proxy foundation, local signal hooks, provider abstraction, Gemini Scout, validated match/candidate results, extraction, stage cache/resume, cost ledger, HTML report, and automated tests.

V1 excludes social APIs, automatic publishing, GUI, subtitles, vertical reframing, caption generation, facecam tracking, sophisticated auto-editing, and a broad provider catalog.

## 6. Success measures

### Product outcome

- A 1–4 hour source completes or resumes without corrupting prior work.
- The output contains a useful match-aware map, extracted candidates, and a shortlist.
- The user reviews only a small fraction of source duration. Record `candidate_review_seconds / source_duration_seconds`; initial target is <= 15%, with a stretch target <= 10%.
- The user can mark candidates accepted/rejected later so precision can be measured.

### Quality metrics for real validation

- Candidate precision: accepted or “worth reviewing” candidates / candidates generated.
- Miss rate from sampled manual audits of random non-candidate intervals.
- Match-boundary median absolute error on annotated sessions.
- Duplicate-candidate rate.
- Extraction boundary defect rate.
- End-to-end completion/resume rate.
- API THB per analyzed hour, per useful candidate, and ultimately per published clip.
- Total estimated/actual monthly AI spend <= configured hard budget (default 100 THB).

The system must not optimize score alone. A smaller shortlist with high precision is desirable, but suppressing all candidates would trivially improve review time while failing the product.

## 7. Acceptance scenario

Given a representative two-hour gameplay VOD and valid configuration, a successful V1 run:

- leaves the source byte-for-byte unchanged;
- writes a completed session manifest and metadata;
- generates a proxy and Scout window results or an explicit budget/offline stop;
- produces zero or more validated candidates grouped by matches;
- exports every qualifying candidate from the source;
- creates a ranked report containing every valid candidate;
- on rerun, reports cache hits and performs no paid call unless inputs changed or the user forces it.

