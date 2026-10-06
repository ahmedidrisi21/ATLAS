# Example futures configuration (futures-first)

A starting point for running ATLAS on a FundedNext Futures **Flex 50K** challenge
through NinjaTrader (its official API, the Tradovate API), trading MES. It lives here, not in `config/`, because
`config/` changes only by a signed operator commit (AGENTS.md). To use it, the
operator reviews it, copies it into the engine host's config directory and signs
the commit.

- `atlas.yaml`: one product (MES), the pinned contract (MESZ6), NinjaTrader as the platform.
- `risk.yaml`: smaller than the forex file, because the Flex 50K max loss is only 3% of the account.
- `prop_rules/fundednext_futures_flex_50k.yaml`: the firm's rules as data, checked on 2026-10-05.

Before going live, check:

1. That NinjaTrader API access works on a FundedNext account (not confirmed by any document we could read;
   the API needs a live account over $1,000, an API Access subscription and an API key). Then run on the
   NinjaTrader demo host before any FundedNext account.
2. The round-turn commission per contract, then set `execution.commission_per_lot`.
3. The pinned contract: roll to the next one (MESH7) before `roll_days` ahead of MESZ6's last trading day.

See docs/futures.md.
