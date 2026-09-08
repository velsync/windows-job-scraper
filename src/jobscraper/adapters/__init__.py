"""Source adapters (S1.5).

Authority: docs/spec/v0.3.1.3/02_acquisition_adapters_and_crawler.md ACQ-02
(corrected adapter protocol), ACQ-03 (parse outcome model), ACQ-04
(detail-child planning), ACQ-09 (versioned contracts), §19 (crawl cursor
and pagination contract), §22 (recipe/locator discipline).

Adapters plan and parse. Executors perform I/O (ARC-05). Adapters NEVER
write canonical jobs — they emit immutable observation *proposals* which
the host pipeline (S1.6) persists under the request fence.
"""
