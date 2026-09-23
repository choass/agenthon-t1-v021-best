"""High-confidence relationships in reported quantities, independent of task IDs.

No source code is executed and no inputs/reference answers are read. These are
necessary consistency checks, not a replacement for task-specific verification.
"""

from __future__ import annotations

import csv
import math
import re
from pathlib import Path


def scalar(value):
    if isinstance(value, dict) and "value" in value:
        value = value["value"]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            result = float(value)
        except OverflowError:
            return None
        return result if math.isfinite(result) else None
    return None


def semantic_issues(value, path="$") -> list[str]:
    issues = []
    if isinstance(value, list):
        for i, item in enumerate(value):
            issues.extend(semantic_issues(item, f"{path}[{i}]"))
        return issues
    if not isinstance(value, dict):
        return issues
    numbers = {str(k).lower(): scalar(v) for k, v in value.items()}
    numbers = {k: v for k, v in numbers.items() if v is not None}

    # Any named allocation family, not a task-specific Kelly answer or formula.
    families = {}
    for key, number in numbers.items():
        match = re.fullmatch(r"(.+)_fraction_(\d+)", key)
        if match:
            families.setdefault(match[1], {})[int(match[2])] = number
    for family, components in families.items():
        indexes = sorted(components)
        if len(indexes) < 2 or indexes != list(range(len(indexes))):
            continue
        for total in (f"gross_{family}_leverage", f"total_{family}_leverage"):
            if total in numbers:
                gross = sum(abs(x) for x in components.values())
                if not math.isclose(numbers[total], gross, rel_tol=1e-7, abs_tol=1e-9):
                    issues.append(
                        f"{path}.{total}: reported {numbers[total]:.12g}, but gross exposure from all {family}_fraction_i is {gross:.12g}; signed sum is net exposure. Reconcile the definition and recompute the reported checkpoint."
                    )

    # Identify a raw SVI tuple by all five names. A standalone rho may be an option
    # interest-rate Greek and is deliberately not treated as a correlation.
    if {"a", "b", "rho", "m", "sigma"} <= numbers.keys():
        a, b, rho, sigma = (numbers[k] for k in ("a", "b", "rho", "sigma"))
        if b < 0 or sigma <= 0 or abs(rho) >= 1:
            issues.append(
                f"{path}: raw SVI requires b>=0, sigma>0 and |rho|<1; inspect calibration constraints."
            )
        elif a + b * sigma * math.sqrt(1 - rho * rho) < -1e-10:
            issues.append(
                f"{path}: raw SVI minimum total variance a+b*sigma*sqrt(1-rho^2) is negative."
            )

    for key, number in numbers.items():
        if (
            re.fullmatch(
                r"(?:[a-z0-9]+_)*(call|put)_price(?:_(?:usd|eur|gbp|jpy|btc|eth))?", key
            )
            and number < -1e-10
            and not any(
                term in key.split("_") for term in ("net", "short", "spread", "pnl")
            )
        ):
            issues.append(
                f"{path}.{key}: negative option price {number:.12g}; a nonnegative payoff has nonnegative value. Check whether this is a price or a signed position/P&L."
            )
    option_type = str(value.get("type", value.get("option_type", ""))).lower()
    if option_type in {"call", "put", "c", "p"}:
        for key in ("cf_price", "mc_price"):
            if key in numbers and numbers[key] < -1e-10:
                issues.append(
                    f"{path}.{key}: negative {option_type} option price {numbers[key]:.12g}; check payoff, closed-form signs and units."
                )
    for key, item in value.items():
        if isinstance(item, (dict, list)):
            issues.extend(semantic_issues(item, f"{path}.{key}"))
    return issues


def csv_semantic_issues(path: Path) -> list[str]:
    """Only read small tables with recognizable relationships; never assume all NaNs invalid."""
    if path.stat().st_size > 8 * 1024 * 1024:
        return []
    issues = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        fields = {s.lower() for s in (reader.fieldnames or [])}
        relevant = (
            {"a", "b", "rho", "m", "sigma"} <= fields
            or any(
                re.fullmatch(
                    r"(?:[a-z0-9]+_)*(call|put)_price(?:_(?:usd|eur|gbp|jpy|btc|eth))?",
                    key,
                )
                for key in fields
            )
            or (
                bool(fields & {"cf_price", "mc_price"})
                and bool(fields & {"type", "option_type"})
            )
        )
        if not relevant:
            return []
        for i, row in enumerate(reader):
            if i >= 50_000:
                break
            values = {}
            for key, val in row.items():
                if key is None:
                    continue
                try:
                    values[key.lower()] = float(val)
                except (ValueError, TypeError):
                    values[key.lower()] = val
            issues.extend(semantic_issues(values, f"CSV row {i + 2}"))
            if len(issues) >= 12:
                return issues[:12] + [
                    "Additional rows omitted; inspect the full table."
                ]
    return issues
