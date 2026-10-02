"""Energy estimate: an order-of-magnitude proxy computed from token counts, never a measurement.

The constants are the README's "Energy estimation methodology" mid estimates (mWh per 1,000 tokens); the stated uncertainty is at least a factor of
3 in each direction. Claude tiers get a multiplier relative to the Opus-anchored constants; a model that is not a Claude tier (provider not
anthropic, or no tier keyword) is counted with multiplier 1 and marked unweighted, so the page and the facts can state how many requests that is.
Reasoning tokens are a subset of output (as in pricing) and are not counted again.
"""
import math

PER_1K = dict(fresh_input=390, output=1400, cache_read=15, cache_write=490)  # mWh per 1,000 tokens
TIERS = dict(haiku=0.3, sonnet=0.6, opus=1.0)
UNCERTAINTY = 3  # low = mid / 3, high = mid x 3
UNITS = ((1e9, 'MWh'), (1e6, 'kWh'), (1e3, 'Wh'), (1, 'mWh'))  # in mWh


def tier(provider, model):
    """'haiku', 'sonnet' or 'opus' for an Anthropic model whose id names a tier, else None (unweighted)."""
    m = (model or '').lower()
    return next((k for k in TIERS if k in m), None) if (provider or '').lower() == 'anthropic' else None


def multiplier(provider, model):
    """(multiplier, weighted): the tier's multiplier, or (1.0, False) for a model that is not a Claude tier."""
    t = tier(provider, model)
    return (TIERS[t], True) if t else (1.0, False)


def parts(tokens, mult=1.0):
    """Mid estimate in mWh per token class for one observation's token counters (an unknown counter is 0)."""
    return {k: mult * (tokens.get(k) or 0) / 1000 * e for k, e in PER_1K.items()}


def mid_mwh(tokens, mult=1.0):
    return sum(parts(tokens, mult).values())


def bounds(mid):
    """(low, high) in mWh: the stated uncertainty of at least a factor of 3 in each direction."""
    return mid / UNCERTAINTY, mid * UNCERTAINTY


def snap(mwh):
    """Nearest 1, 2, 5 or 10 times a power of ten (boundaries at log10 fractions 0.15, 0.5, 0.85 as in the retired statusline.py); below 1 mWh unchanged."""
    if mwh < 1:
        return mwh
    decade = math.floor(math.log10(mwh))
    frac = math.log10(mwh) - decade
    return (1 if frac < 0.15 else 2 if frac < 0.5 else 5 if frac < 0.85 else 10) * 10 ** decade


def fmt(mwh):
    """'~2 Wh', '~500 mWh', '<1 mWh', '0 mWh': the snapped value in the largest unit that keeps it >= 1."""
    if mwh <= 0:
        return '0 mWh'
    if mwh < 1:
        return '<1 mWh'
    v = snap(mwh)
    scale, unit = next(u for u in UNITS if v >= u[0])
    return f'~{v / scale:g} {unit}'
