# M8 v19 Boundary Refiner — Provider Boundary Status

## Status

This checkpoint extends the provider-free candidate boundary-refinement work to the Gemini
provider boundary without authorizing or performing a live Gemini request.

The execution entrypoints accept explicitly injected `GeminiTransport` objects; they do not
construct `GenAITransport` on their own. Automated acceptance uses `FakeGeminiTransport`, so this
checkpoint has zero live provider/network generations and zero real API cost.

## Implemented candidate contract

For one explicitly selected Scout candidate:

1. Reuse the committed, ffprobe-validated `slowed.mp4` boundary-refinement artifact.
2. Recompute and verify its SHA-256 before the provider upload seam.
3. Bind request identity to session ID, candidate semantics, refinement plan, prompt/schema,
   exact Gemini model/billing/media/thinking settings, and slowed-media hash.
4. Quote through the existing M4 cost gate without reservation during preflight.
5. On execution, reserve before upload and mark `IN_FLIGHT` immediately before generation.
6. Accept only the prepared candidate-local `slowed.mp4` through the custom upload validator;
   RAW source and unrelated session media are not accepted by this boundary.
7. Persist only sanitized Gemini envelope metadata plus the strict boundary response; thought steps
   are not represented or persisted.
8. Settle from provider-reported usage. Pre-dispatch failure releases the reservation; ambiguous
   post-dispatch failure is persisted as `AMBIGUOUS` and is never automatically regenerated.
9. Reuse a result only when the ledger record is `SETTLED` and local request/response fingerprints
   remain valid. A settled ledger record with missing/corrupt local response fails closed rather than
   silently generating again.
10. Map accepted slowed-clip timestamps back to source time through the existing v19 mapping and
    overlap guard.

## Aggregate batch preflight

The provider boundary also has a provider-free aggregate preflight for an explicit candidate/media
batch. It keeps the same maximum of 32 candidates and preserves caller order. Each item is quoted
with the exact candidate-level request identity used by execution, then the batch sums base and
reserved micro-THB exposure before any reservation or transport work.

The aggregate gate fails closed when the total reserved exposure exceeds the currently available
budget even when every candidate-level quote would fit by itself. It performs no ledger writes,
no upload, and no provider generation. Tests also enforce non-empty input, unique candidate IDs,
per-item session/media provenance, deterministic ordering, and zero cost-call persistence during
preflight.

## Injected-transport batch execution

The explicit batch orchestrator now carries the same contract end to end without wiring a live
provider into production:

1. Validate source/proxy/session-map identity, duration, explicit candidate IDs, and confidence.
2. Prepare or reuse each candidate-local refinement media artifact.
3. Run aggregate preflight for the whole selected batch before the first provider execution.
4. Execute candidates in caller-supplied order through the injected transport only.
5. Reuse each candidate independently when its ledger state is `SETTLED` and its persisted request
   and response fingerprints are still valid.
6. Preserve completed per-candidate ledger/artifacts if a later candidate fails, but do not persist
   a final batch artifact or refined SessionMap until the entire batch completes.
7. Replace only selected candidates, recompute clip bounds deterministically, and persist separate
   `session_map.refined.gemini.json` and `batch.gemini.json` outputs; the input SessionMap is not
   overwritten.
8. Persist batch provenance including request fingerprints, call IDs, response status/confidence,
   cache state, semantic candidate hashes, and boundary-change flags.

The integration acceptance path uses real local ingest/proxy/boundary-media FFmpeg work plus two
`FakeGeminiTransport` candidate calls. The first pass must settle both calls; a second pass must
reuse both media/provider caches without increasing generation count.

## Production CLI seam

The local CLI now exposes `highlight refine-boundaries SESSION_ID CANDIDATE_ID...` as a
separate explicit boundary-refinement workflow. Its default behavior is provider-free preflight:
it loads only committed session/source/proxy artifacts, prepares candidate-local slowed media,
quotes the whole selected batch, and prints aggregate maximum reserved THB exposure.

Real execution requires both `--execute` and a fresh `--allow-remote-upload` on the same
invocation. Persisted session configuration never carries that upload authorization forward. The
real `GenAITransport` is supplied through a lazy factory only after aggregate preflight succeeds,
and it is not constructed at all when every selected candidate is a validated settled cache hit.
The production command never replaces `session_map.json`; successful output remains the separate
`session_map.refined.gemini.json` plus `batch.gemini.json` provenance artifact.

This checkpoint adds the production wiring seam but does not execute the command with a real API
key or make a live Gemini request. Automated acceptance continues to use fake/injected transports.

## Calibration feasibility gate

Before any live boundary-refinement calibration spend, the benchmark CLI exposes
`highlight benchmark boundary-feasibility SESSION_ID --dataset <dataset.json> --annotations
<annotations.json>`. The command is provider-free and accepts calibration cases only; a dataset case
declared as validation/holdout fails closed before session/provider work. It uses the same versioned
M8 temporal evaluation policy to measure strict matches, then separately reports:

- annotations with direct Scout-anchor overlap,
- annotations reachable only inside the candidate-local refinement context,
- boundary headroom where an anchor overlaps ground truth but fails the strict ruler,
- detection gaps where no Scout anchor overlaps the annotated event,
- MUST_CATCH detection gaps and MUST_CATCH boundary headroom, and
- strict boundary-error medians for already matched pairs.

The persisted private feasibility artifact contains hashes/metrics/IDs only and records
`provider_calls=0`. Any candidate IDs are explicitly ground-truth-derived calibration diagnostics;
they must never be reused as an automatic production selection policy. This gate exists because v19
can refine the timing of an existing event but cannot recover a highlight Scout did not detect.

For cross-machine work, `highlight benchmark pack-boundary-feasibility` creates a portable
calibration-only JSON bundle containing the single-case dataset view, annotation document, sanitized
`source.json`, `session_map.json`, and feasibility result. It includes no source video, proxy, clip,
provider artifact, credential, persisted machine config, or validation/holdout case. The bundled
`source.path` is replaced by a non-existent absolute sentinel while SHA-256/duration/source identity
remain intact. `boundary-feasibility` now uses the caller-selected `data_dir` rather than requiring the
source machine's `config.resolved.json`, so the same evidence can be revalidated on another PC.

## Deliberate limits

- No live Gemini transport is wired into the production pipeline in this checkpoint.
- No automatic candidate selection policy is added; the existing explicit bounded candidate set
  remains authoritative.
- No revealed M8 validation holdout is used for tuning.
- No V1 defaults are locked by this work.
- The real private gameplay corpus is still required for any meaningful quality decision.

## Next safe step

After offline regression remains green, first run the provider-free boundary-feasibility gate on
legitimate calibration data. If it shows meaningful anchor-overlap boundary headroom, a separately
authorized live boundary-refinement calibration experiment can be considered; if detection gaps
dominate, remediate Scout detection instead of spending on boundary timing. Any real generation must
still pass aggregate preflight and a newly explicit attempt/exposure authorization. The revealed v13
validation holdout is permanently excluded from tuning, and a future unbiased decision requires a
fresh locked holdout prepared before predictions.
