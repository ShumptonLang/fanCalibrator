"""Relative acoustic cost of a set of fan speeds.

Fan noise follows the fan laws: sound power ~ RPM^5, i.e.
    L(rpm) = L_max + 50 * log10(rpm / rpm_max)   [dB]
Sources add as powers, so the total is 10*log10(sum 10^(L_i/10)).

Without measured dB(A) specs, every fan is assumed to make ``ref_db`` at its
max RPM; supply per-fan ``max_db`` (e.g. from the datasheet, or a fudge factor
for a fan that sits right next to your ear) to weight them differently.
"""

from __future__ import annotations

import math


def fan_db(rpm: float, max_rpm: float, max_db: float) -> float:
    if rpm <= 0 or max_rpm <= 0:
        return -math.inf
    return max_db + 50.0 * math.log10(rpm / max_rpm)


def total_db(levels: list[float]) -> float:
    power = sum(10 ** (l / 10) for l in levels if l != -math.inf)
    return 10 * math.log10(power) if power > 0 else -math.inf
