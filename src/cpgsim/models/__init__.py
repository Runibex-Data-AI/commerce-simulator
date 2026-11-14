"""Pure models: no state, no I/O.

Calendar, seasonality, arrivals (NHPP thinning), delays, baskets, payments,
repurchase, pricing and elasticity. Functions here take explicit inputs
(config values, dates, an ``Rng``) and never touch ``world.store``.
"""
