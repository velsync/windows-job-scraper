"""Network safety primitives (S1.2).

Authority: docs/spec/v0.3.1.3/02_acquisition_adapters_and_crawler.md §31
(URL normalization and direct-link preservation);
04_security_and_authentication.md §5.1 (outbound SSRF controls), SEC-02
(browser-side destination policy);
01_product_and_workflow.md PROD-05 (safe displayed links).

Three modules:

* ``urlnorm`` — deterministic URL normalization for identity comparison,
  always preserving the raw URL separately;
* ``destination`` — fail-closed outbound destination policy (scheme
  allowlist, credential rejection, DNS resolution, denied address ranges,
  host/path policy, per-hop redirect validation, narrow internal grants);
* ``safelinks`` — render-time allowlist for clickable external links.
"""
