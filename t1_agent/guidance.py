"""Select one generic finance guide from task text, without extra inference."""

from __future__ import annotations

import re
from pathlib import Path

CUES = {
    "api_migration": {"polars": 8, "api migration": 8, "library migration": 8},
    "documents": {"10-k": 8, "form 4": 8, "ixbrl": 8, "filing": 4, "xml": 3},
    "fixed_income": {
        "yield curve": 8,
        "ois": 7,
        "swap": 5,
        "swaption": 5,
        "bond": 5,
        "immunization": 6,
    },
    "numerical": {
        "option": 4,
        "greeks": 5,
        "monte carlo": 5,
        "calibration": 3,
        "expected shortfall": 7,
        "value-at-risk": 5,
        "copula": 4,
    },
    "market_data": {
        "realized volatility": 7,
        "timestamp": 3,
        "time zone": 4,
        "stationarity": 5,
        "missing values": 2,
        "ohlc": 5,
    },
    "portfolios": {
        "backtest": 6,
        "rebalance": 5,
        "kelly": 7,
        "leverage": 4,
        "portfolio": 1,
        "hedge": 2,
        "turnover": 3,
    },
}


def select_guide(instruction: str) -> tuple[str, str] | None:
    text = instruction.casefold()
    scores = {
        name: sum(
            weight
            for cue, weight in cues.items()
            if re.search(r"(?<!\w)" + re.escape(cue) + r"(?!\w)", text)
        )
        for name, cues in CUES.items()
    }
    name = max(scores, key=scores.get)
    if not scores[name]:
        return None
    body = (Path(__file__).with_name("guides") / (name + ".md")).read_text()
    return name, body
