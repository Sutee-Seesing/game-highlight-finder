# Codex Work Plan — M8 Highlight Quality / Gemini 3.7 Flash

Updated: 2026-08-23 Asia/Bangkok
Repo: `C:\Data\Works\Personal_Projects\Active_Products\game-highlight-finder`
Branch: `m8-v12-revision-planner`
Expected HEAD: `ea8eadde92951817412dc11e367dea812667c7ba`
`origin/main` must remain untouched at `c8b4d29b6dfdc5e1af8154dcdcce77b71a4823b9`.

## Mission
Finish the calibration-quality investigation and turn the evidence into a production-safe Scout strategy.
Priority: **quality -> reliability -> cost-effectiveness -> cost optimization**.
Do not reduce quality merely to save a few baht.
Avoid waste by reusing proxies/cache and never repeating settled provider calls.

## Current architecture
Long gameplay -> local ingest/proxy/signals -> Gemini Scout -> reconcile -> extract -> rank -> report.
Scout is detection-first; ranking is a separate short-form-potential layer.
Current v15 defaults: 300 s windows, 30 s overlap, prompt `gemini-scout-window-v15`, high media resolution, max output 4096.
Default model remains `gemini-3.5-flash-lite`; Gemini 3.7 is explicit/selectable only.
## Hard rules
- Calibration data may be used for tuning; do NOT inspect/tune against validation holdout.
- Never upload original raw source; only analysis/window proxies may reach Gemini.
- Never print or commit API key values.
- `.env` has `GEMINI_API_KEY1`...`GEMINI_API_KEY7`; load one slot into process memory only.
- No automatic provider-generation retry.
- After timeout, inspect process/ledger/artifacts before any retry.
- Every provider call must end with cost accounted and remote-file deletion verified.
- Do not stage/delete/modify pre-existing untracked `.t/` blindly.
- Do not merge or modify `main`.

## What v15 proved
Two full calibration cases were run on Gemini 3.5 Flash-Lite with v15, 3 windows each.
Actual v15 settled cost: cal-01 `2.264809 THB`, cal-02 `2.291234 THB`.
cal-01: 1 TP / 5 FP / 2 FN, recall 0.333, MUST_CATCH recall 0.
cal-02: 0 TP / 7 FP / 2 FN, recall 0, MUST_CATCH recall 0.
Shorter 300 s windows alone did not solve quality.
Offline boundary sensitivity showed that deterministic padding can raise recall, but this is diagnostic only; do not game the evaluator with arbitrary padding.
The evidence points to both temporal localization error and model capability limits.
## Gemini 3.7 Flash evidence
Commit `ea8eadd` added production-safe Gemini 3.7 Flash support, pricing identity, model capabilities, and tests.
Provider-free verification passed: focused 47/47; full suite 259/259; Ruff, format check, mypy, diff check all passed.

A/B used the exact same v15 proxy/window/prompt/resolution/output ceiling; only model/thinking changed.
Gemini 3.7 Flash used thinking `medium`.

- cal-02 tail truth MUST_CATCH 574-590 s: 3.5 Lite predicted 560-578 s and failed; 3.7 predicted 570-579 s, evaluator TP, IoU 0.250.
- cal-01 middle truth MUST_CATCH 340-356 s: 3.5 Lite predicted 317-329 s and failed; 3.7 predicted 325-352 s, evaluator TP, IoU 0.387.
- cal-01 truth 543-559 s: 3.7 predicted 545-562 s, evaluator TP, IoU 0.737.

Settled diagnostic costs known:
- cal-02 tail 3.7: `0.711775 THB`, remote deleted.
- cal-01 middle 3.7: `2.570881 THB`, remote deleted.
This is strong cross-game evidence that model capability matters and 3.7 is worth further calibration testing.

## Current blocker — DO THIS FIRST
A cal-01 first-window 3.7 call (`0-300 s`) was started after preflight `3.683015 THB`.
The provider generation appears to have completed, but cost settlement failed with:
`CostGateError: Pricing does not define a cached-input rate`.
Do **not** retry this generation until the original call is reconciled.
## Required next steps
1. Inspect the failed cal-01 first-window session/ledger/raw/canonical/remote metadata. Confirm whether generation completed and whether remote file deletion happened.
2. Add Gemini 3.7 cached-input pricing to the production pricing snapshot using the current official rate and tests. Keep exact dated pricing identity and fail-closed behavior.
3. Add a regression test proving settlement succeeds when Gemini 3.7 actual usage includes cached-input tokens.
4. Recover/reconcile the existing failed settlement if safely possible. Do not create a duplicate provider generation merely to repair accounting.
5. Evaluate the existing cal-01 first-window response offline against calibration truth 120-136 s if a canonical response exists.
6. Only after step 1-5 are clean, run the remaining useful A/B window: cal-02 middle 270-570 s, using an available key slot and the exact same v15 inputs except model/thinking.
7. Combine all 3.7 calibration predictions and compute deterministic evaluator metrics against all 5 calibration highlights.
8. Compare quality and settled THB against v15 Flash-Lite. Report recall, MUST_CATCH recall, precision, FP count, and cost per matched/usable highlight.

## Decision rule after calibration
If 3.7 materially improves MUST_CATCH/overall recall across both games at reasonable incremental cost, prepare v17 to make 3.7 the preferred quality Scout configuration.
Do not switch the default merely because one diagnostic looks good; base the decision on combined calibration evidence.
If boundary errors remain after the model upgrade, design a separate boundary-refinement stage rather than arbitrary event padding.
Do not touch validation until a fresh holdout is locked before predictions.

## Verification before commit
Run focused tests for pricing/capabilities/settlement, then `ruff check .`, Ruff format-check on touched Python, `mypy src`, `git diff --check`, and full provider-free `pytest`.
Audit diff for API keys, private paths, calibration labels/timestamps in production prompt/code, and accidental validation references.
Stage only intentional tracked files. Leave `.t/` alone.
Commit/push only to `m8-v12-revision-planner`; verify local HEAD == remote feature HEAD and `origin/main` is unchanged.

## Useful private run area
Current diagnostic/work artifacts live under `data/agent_runs/` and `data/benchmarks/private/`; these are local evidence, not production docs.
Do not commit private benchmark artifacts or source paths.
When uncertain after a timeout: inspect first, retry last.
## Post-Codex checkpoint — 2026-08-24
Feature HEAD is now `ac5f98e67f59ebd9a3221b99daa4500f349b4b6a` (`fix: price Gemini 3.7 cached input`).
Remote feature HEAD matches local; `origin/main` remains `c8b4d29b6dfdc5e1af8154dcdcce77b71a4823b9`.
Gemini 3.7 cached-input rate is now `$0.075 / 1M tokens`; settlement regression coverage is committed.
The previously failed cal-01 first-window accounting was reconciled without a provider retry: settled `0.427216 THB`, remote deleted.
The cal-02 middle call settled `2.867242 THB`, remote deleted, but provider output was incomplete/malformed, so it has no production canonical prediction.

Independent verification after `ac5f98e`:
- full provider-free pytest rerun with repo-local basetemp: **260/260 passed**, exit 0;
- durable log: `data/agent_runs/m8-v16-gemini37-support-2026-08-23/full_pytest_after_ac5f98e.log`.

Exact offline evaluator on recovered cal-01 first canonical response:
- predictions: 33-38 s and 133-141 s;
- truth of interest: 120-136 s;
- **no deterministic temporal match** at current policy.
cal-02 middle raw response contains strong **non-canonical diagnostic evidence** before truncation:
- provider returned first candidate at window-relative 129-142 s;
- window source range is 270-570 s, so global candidate is 399-412 s;
- calibration truth is 401-417 s, which would be a strong temporal match if the response had completed canonically;
- the response then failed while emitting an absurdly long integer for optional `setup_start_ms` and was truncated/incomplete.
Do not count this as a formal TP, but do record it as evidence that 3.7 detected the correct moment and the blocker is now output reliability.

## Next Codex task — v17 structured-output reliability
Do **not** retry the exact failed v16 cal-02 middle generation.
First make a provider-free reliability patch focused on reducing malformed timestamp output.
Preferred design direction: simplify the **window Scout provider contract only**, not the canonical domain model.
Consider removing optional `setup_start_ms` / `payoff_end_ms` from `gemini_window_scout_schema()` while retaining them as optional domain fields for backward compatibility and other producers.
Update the window prompt accordingly so event `start_ms` / `end_ms` carry the meaningful visual sequence; local clip-boundary derivation remains responsible for preroll/postroll context.
Do not add unsupported numeric JSON-schema bounds: the contract intentionally avoids numeric bounds because Gemini API surfaces have compatibility constraints.

Acceptance criteria:
1. Existing persisted responses containing setup/payoff remain readable.
2. Window Scout provider schema no longer asks Gemini for unnecessary optional context timestamps if this design is adopted.
3. Prompt/schema identity changes explicitly (v17); no silent cache collision with v15/v16 requests.
4. Add focused schema/prompt/canonicalization/reconcile tests proving missing setup/payoff is safe.
5. Run Ruff, touched-file format check, mypy, `git diff --check`, and full provider-free pytest.
6. No provider call until the provider-free patch is clean and reviewed.
7. Any future live v17 calibration must use a new request identity/version; never reuse/retry the exact failed v16 request merely to obtain a cleaner JSON response.
8. Continue to keep validation holdout untouched.
