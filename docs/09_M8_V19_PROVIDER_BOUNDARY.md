# M8 v19 Boundary Refiner — Provider Boundary Status

## Status

This checkpoint extends the provider-free candidate boundary-refinement work to the Gemini
provider boundary without authorizing or performing a live Gemini request.

The execution entrypoint accepts an explicitly injected `GeminiTransport`; it does not construct
`GenAITransport` on its own. Automated acceptance uses `FakeGeminiTransport`, so this checkpoint
has zero provider/network generations and zero real API cost.

## Implemented contract

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

## Deliberate limits

- No live Gemini transport is wired into the production pipeline in this checkpoint.
- No automatic candidate selection policy is added; the existing explicit bounded candidate set
  remains authoritative.
- No revealed M8 validation holdout is used for tuning.
- No V1 defaults are locked by this work.
- The real private gameplay corpus is still required for any meaningful quality decision.

## Next safe step

After offline regression remains green, wire this provider boundary into the explicit batch
orchestrator behind a separate live-provider opt-in. Before any real generation, run candidate-level
preflight and enforce the newly approved attempt/exposure budget. A real quality experiment must use
calibration data or a fresh locked holdout; the revealed v13 holdout is not tuning data.
