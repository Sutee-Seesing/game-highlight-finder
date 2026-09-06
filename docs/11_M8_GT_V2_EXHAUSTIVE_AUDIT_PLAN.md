# M8 Amendment — Exhaustive Calibration GT v2 Before More Tuning

Date: 2026-09-04
Status: **ACTIVE — supersedes the immediate Round-B human-proposal-review NEXT where it conflicts with the ground-truth integrity finding below**

## Why this amendment exists

The eight previously unlabelled proposal clips were reviewed from actual video pixels, not signal intensity alone. The result was **1 POSITIVE / 7 BORING / 0 UNCERTAIN**. During that visual review, a genuine cal-01 kill around 439–459 s was found even though it is absent from the old calibration annotation set.

The old calibration provenance was not based on complete full-video human review. Therefore the current calibration ruler is not exhaustive enough to safely drive Scout/ranking/threshold tuning or a paid Round-B precision claim.

The product architecture is not being discarded. The immediate blocker is benchmark truth quality.

## Canonical execution location and preserved evidence

Canonical latest execution branch/worktree remains:

`C:\Data\Works\Personal_Projects\Active_Products\game-highlight-finder-media`

branch `m8-v19-media-local`.

The manual visual-audit conversation worked in the sibling historical worktree:

`C:\Data\Works\Personal_Projects\Active_Products\game-highlight-finder`

That worktree is behind the latest branch and must **not** become the new source-code canonical branch. However, its private review evidence is valid handoff material and must be preserved, especially:

- `.t\calibration-full-video-audit\...`
- `.t\round-b-8-visual-adjudication.json`
- `docs\CURRENT_STATE.md`
- `docs\NEXT_ACTION.md`
- `docs\DECISIONS.md`

Use the sibling audit artifacts read-only when useful. Do not reset/clean/stash/drop either worktree merely to simplify reconciliation.

## Current visual evidence

Eight-candidate adjudication:

- 1 positive
- 7 boring
- 0 uncertain

Known calibration findings to preserve:

- cal-01 12–26 s: not a highlight; Buy Phase plus shooting at a teammate.
- cal-01 old 120–136 s annotation: contains a genuine engagement/kill, but setup appears to begin before 120 s and the tail may include unnecessary aftermath.
- cal-01 439–459 s: genuine visible kill omitted from the old calibration annotation set.
- cal-02 exhaustive full-video audit has not yet been completed.

These candidate labels are evidence, not exhaustive benchmark ground truth by themselves.

## Required order

### A. Finish exhaustive cal-01 visual audit

1. Reuse the existing local review proxy/dense strips where valid; do not regenerate expensive media without need.
2. Inspect the complete cal-01 timeline from actual pixels.
3. For suspicious intervals distinguish enemy engagement from misleading activity such as Buy Phase, teammate shooting, menu/settings transitions, spectating, traversal and post-round reset.
4. Record accepted highlights with setup start, semantic event bounds, payoff end, category, importance, modality, confidence and visible evidence.
5. Preserve useful negative/boring intervals when they help later false-positive evaluation.
6. Revisit the old annotations only after whole-timeline coverage is complete.

### B. Audit cal-02 with the same exhaustive policy

7. Reuse or create a dense full-timeline visual pack for cal-02.
8. Review the entire source under the same rubric and record positive and useful negative evidence.

### C. Create a separate GT v2 / exhaustive revision

9. Do not overwrite the old annotation revision.
10. Bind the new annotation revision to source hash, duration, schema/evaluation policy and explicit exhaustive-review provenance.
11. Mark the revision exhaustive only after both calibration videos have complete timeline coverage.

### D. Re-score existing experiments offline first

12. Re-evaluate existing Scout/hybrid outputs against GT v2 wherever artifact identity is compatible.
13. Make **zero new provider generations** merely to recompute metrics.
14. Recompute precision, recall, MUST_CATCH recall, false positives/hour, review burden, duplicate/boundary metrics and Best-of behavior.
15. Attribute remaining errors by layer: semantic detection/localization, reconcile/dedupe, boundary refinement, ranking/filtering.

### E. Only then decide what source/tuning change is warranted

16. Do not tune thresholds or Scout/ranking against the incomplete old GT.
17. If GT v2 proves a source defect, make the smallest evidence-backed implementation change and re-run provider-free verification first.
18. Any new Gemini/OpenRouter/provider experiment requires a fresh explicit authorization with exact purpose, attempts and exposure cap.

## Explicit non-goals / stop conditions

- No montage/TikTok assembly work as a substitute for candidate quality.
- No semantic labels inferred from audio/motion/activity intensity alone.
- No provider call to perform human/visual GT repair.
- No use of the revealed validation holdout as tuning data.
- No overwrite/delete of historical annotations or paid evidence.
- No source-code work on the 54-commit-behind sibling branch merely because the audit artifacts live there.

## Exit gate

This amendment is complete when cal-01 and cal-02 both have whole-timeline visual coverage, a separate validated GT v2/exhaustive revision exists, existing compatible outputs have been re-scored offline, and the next source/tuning action is justified by the new error attribution rather than by incomplete historical labels.
