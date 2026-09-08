"""Host-owned acquisition execution (S1.4).

Authority: docs/spec/v0.3.1.3/02_acquisition_adapters_and_crawler.md §11
(ExecutionPlanEnvelope, RequestPlan, ResultEnvelope), §21 (Page Validity
Classifier), §27 (typed failure model), ACQ-06 (side-effect restriction);
04_security_and_authentication.md §5.1 (outbound SSRF controls).

Adapters plan and parse; executors perform I/O (ARC-05). The executor
never touches the database — network waits never occur inside DB write
transactions; the run driver claims fenced work, executes I/O outside any
transaction, and persists results under the S1.3 fence.
"""
