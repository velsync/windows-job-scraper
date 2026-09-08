"""Observation → canonical pipeline (S1.6).

Authority: docs/spec/v0.3.1.3/03_durable_runtime_and_persistence.md §29
(provenance-first observation model), §30 (evidence chain), §40/RUN-13
(absence authority), RUN-11 (canonical creation ordering), RUN-12
(provenance fields), RUN-15 (source-native ID reuse), RUN-21 (projection
ordering); 01 §38/PROD-03 (reuse guard), §39 (canonical source
selection), §34/§37 (normalization).

A collector never writes canonical jobs directly. Every job enters as an
immutable JobObservation under the request fence, then normalization →
entity resolution → job_sources presence → canonical projection happens
inside the SAME fenced transaction (RUN-11 atomicity).
"""
