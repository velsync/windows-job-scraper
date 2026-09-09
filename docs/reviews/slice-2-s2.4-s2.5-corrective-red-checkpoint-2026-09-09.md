# Slice 2 S2.4/S2.5 Corrective Red Checkpoint — 2026-09-09

This commit intentionally runs the new S2.4 corrective regression tests against the uncorrected S2.5 production code inherited from `0624039fbe22028b975767325c64799aac35492c`.

Expected RED failures:

1. conflicting weak Greenhouse + Lever signals currently cross-subsidize confidence above the specialized-route threshold;
2. high-confidence Lever currently produces runnable `lever` candidates before the Lever adapter exists;
3. low-confidence generic fallback currently advertises unregistered adapter id `generic`;
4. Greenhouse currently advertises strategy labels not implemented by the Greenhouse adapter.

No production code has been changed at this checkpoint. The purpose is to prove the new tests detect the audited defects before the corrective implementation is applied.
