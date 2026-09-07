"""Browser-worker supervision from the service process.

Authority: docs/plans/slice-0-worker-implementation-plan-v0313.md S0.9;
docs/spec/v0.3.1.3/05_windows_packaging_and_operations.md sections 4.2/4.3
(WIN-05: supervision owns lifecycle, restart/backoff, restart-rate ceiling,
process-tree cleanup).

The supervisor talks to the worker only over the typed stdin/stdout
protocol — the service process never imports Playwright and never runs
Chromium itself (ARC-06).
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from jobscraper.browser_worker.protocol import (
    SupervisorError if False else ProtocolError,
)
