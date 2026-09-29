"""Prometheus metrics. One module so the API's /metrics and every controller share a registry."""

from prometheus_client import Counter

RECONCILER_REPAIRS = Counter(
    "ipo_reconciler_repairs_total", "Drift repaired by the reconciler", ["rule"])
