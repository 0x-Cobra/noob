import math

from apebot.chain import from_raw, sqrt_price_to_mid, to_raw

Q96 = 2**96


def sqrt_from_price(p_token1_per_token0_raw: float) -> int:
    return int(math.sqrt(p_token1_per_token0_raw) * Q96)


def test_base_is_token0_18_vs_6_decimals():
    # base 18d, quote 6d; human price 150 quote per base => raw token1/token0 = 150e6/1e18
    raw = 150e6 / 1e18
    mid = sqrt_price_to_mid(sqrt_from_price(raw), True, 18, 6)
    assert abs(mid - 150) < 1e-6


def test_base_is_token1():
    # quote is token0 (6d), base token1 (18d); raw token1/token0 = 1e18/150e6
    raw = 1e18 / 150e6
    mid = sqrt_price_to_mid(sqrt_from_price(raw), False, 18, 6)
    assert abs(mid - 150) < 1e-6


def test_raw_roundtrip():
    assert to_raw(1.5, 6) == 1_500_000
    assert from_raw(1_500_000, 6) == 1.5
    assert to_raw(0.000001, 18) == 10**12
