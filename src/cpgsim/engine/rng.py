"""Deterministic randomness.

Every random draw is derived from the seed plus a stable tuple of parts that
identifies *what* is being decided (entity id, event type, attempt number...).
There is no global random stream, so the same seed and config produce the same
business regardless of tick length, commit batching or restarts.

Usage::

    r = world.rng("web.payment", order_id, attempt)
    if r.random() < 0.97: ...

Draw from one ``Rng`` in a fixed order inside a handler; never share an Rng
between unrelated decisions.
"""

from __future__ import annotations

import hashlib
import math
import random
import struct
from typing import Sequence, TypeVar

T = TypeVar("T")


def derive_seed(seed: int, *parts: object) -> int:
    """64-bit integer derived from the seed and an ordered tuple of parts."""
    h = hashlib.blake2b(digest_size=8)
    h.update(str(int(seed)).encode())
    for p in parts:
        h.update(b"\x1f")
        h.update(str(p).encode())
    return struct.unpack("<Q", h.digest())[0]


class Rng(random.Random):
    """``random.Random`` with a few extra distributions used by the models.

    All methods are pure Python so results are identical across platforms for
    the same Python minor version.
    """

    def __init__(self, seed: int, *parts: object) -> None:
        super().__init__(derive_seed(seed, *parts))

    # -- distributions ---------------------------------------------------
    def poisson(self, lam: float) -> int:
        """Poisson draw. Knuth for small lambda, transformed rejection otherwise."""
        if lam <= 0:
            return 0
        if lam < 30:
            limit = math.exp(-lam)
            k, p = 0, self.random()
            while p > limit:
                k += 1
                p *= self.random()
            return k
        # PTRS (Hormann 1993)
        slam = math.sqrt(lam)
        loglam = math.log(lam)
        b = 0.931 + 2.53 * slam
        a = -0.059 + 0.02483 * b
        inv_alpha = 1.1239 + 1.1328 / (b - 3.4)
        vr = 0.9277 - 3.6224 / (b - 2)
        while True:
            u = self.random() - 0.5
            v = self.random()
            us = 0.5 - abs(u)
            k = int(math.floor((2 * a / us + b) * u + lam + 0.43))
            if us >= 0.07 and v <= vr:
                return k
            if k < 0 or (us < 0.013 and v > us):
                continue
            if (math.log(v) + math.log(inv_alpha) - math.log(a / (us * us) + b)) <= (
                -lam + k * loglam - math.lgamma(k + 1)
            ):
                return k

    def lognormal_median(self, median: float, sigma: float) -> float:
        """Log-normal draw parameterised by its median (= exp(mu))."""
        if median <= 0:
            return 0.0
        return median * math.exp(self.gauss(0.0, sigma))

    def gamma_mean(self, mean: float, shape: float) -> float:
        """Gamma draw parameterised by mean and shape (k); scale = mean / k."""
        if mean <= 0:
            return 0.0
        return self.gammavariate(shape, mean / shape)

    def bernoulli(self, p: float) -> bool:
        return self.random() < p

    def weighted_choice(self, items: Sequence[T], weights: Sequence[float]) -> T:
        return self.choices(items, weights=weights, k=1)[0]

    def weighted_index(self, cum_weights: Sequence[float]) -> int:
        """Index draw from *cumulative* weights (fast path for hot loops)."""
        import bisect

        x = self.random() * cum_weights[-1]
        return bisect.bisect_right(cum_weights, x)


def stable_uuid(seed: int, *parts: object) -> str:
    """Deterministic RFC 4122 version-4-shaped UUID string."""
    h = hashlib.blake2b(digest_size=16)
    h.update(str(int(seed)).encode())
    for p in parts:
        h.update(b"\x1f")
        h.update(str(p).encode())
    b = bytearray(h.digest())
    b[6] = (b[6] & 0x0F) | 0x40
    b[8] = (b[8] & 0x3F) | 0x80
    x = b.hex()
    return f"{x[0:8]}-{x[8:12]}-{x[12:16]}-{x[16:20]}-{x[20:32]}"
