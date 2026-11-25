# Assumptions and Limitations

1. The source venue is MEXC Futures public API. API reachability does not establish Romanian product/account eligibility.
2. All-time means earliest available MEXC Futures perpetual history from provider creation/opening metadata, not spot-market lifetime or pre-listing history.
3. The provider may omit no-trade days or have historical gaps. Earliest/latest bars and observed gaps are recorded; API access does not prove complete tick-level history.
4. 30-minute GitHub Actions monitoring is sampled and may be delayed or missed.
5. Simulated fills use visible public depth and modeled fees; they do not guarantee achievable live execution.
6. Non-USDT settlement remains research-only for the USDT account.
7. The inverse-volatility curve is a shadow counterfactual, not a second executed account.
8. No historical backtest or profitability claim exists until point-in-time universe replay and realistic execution audit are complete.
9. Public event state is hash-validated by the application but an authorized repository writer can still rewrite an unprotected branch.
