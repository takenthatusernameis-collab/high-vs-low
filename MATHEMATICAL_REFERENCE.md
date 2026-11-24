# Mathematical Reference

For instrument i at completed daily timestamp t:

ATH_i(t) = maximum daily high observed through t.
ATL_i(t) = minimum daily low observed through t.
d_ATH = 100 × (ATH_i(t) / close_i,t − 1).
d_ATL = 100 × (close_i,t / ATL_i(t) − 1).

Long rank is ascending d_ATH. Short rank is ascending d_ATL. Tie-break deterministically by instrument key. Require positive valid OHLC and close between historical extremes. No future bar is allowed.

## Primary sizing
Target notional per leg = NAV × 0.50 × 2.0 = 1.0 × NAV.
Base quantity = floor_to_step(target_notional / reference_price, volUnit × contractSize).
Never round up to minimum. If either leg cannot be sized or filled safely, reject that pair and try the next candidates.

## Inverse-volatility shadow
Use 30 common completed daily returns available at each primary rebalance. Let sigma_L and sigma_S be sample standard deviations.
w_L = sigma_S / (sigma_L + sigma_S); w_S = 1 − w_L.
Equal pair return = r_L − r_S.
Alternative base return = 2 × (w_L × r_L − w_S × r_S).
The volatility overlay lambda = max(1, sigma_equal / sigma_alt), only when both vols are positive. Insufficient data or zero volatility falls back to equal 50/50 and lambda 1, with status disclosed. This is a shadow counterfactual, not a leverage recommendation.
