# NinjaTrader MCP capture

- Captured: 2026-10-06 (about 04:36 to 04:46 UTC)
- Environment: DEMO (the server announces `MODE: DEMO`)
- Read-only. Only `describe`, `user_profile`, `my_portfolio`, `search_contracts`, `market_snapshot`,
  `market_history`, `order_history`, `fill_history`, `position_history`, `risk_settings` and
  `estimate_order` (a dry run) were called. No order was placed, changed or cancelled, and no risk setting,
  alert or position was touched.
- Each file is the server's answer as returned (compact JSON). The only edits are that the account name became
  `DEMO_ACCOUNT_1`, the user name `USER_NAME` and the email `USER_EMAIL`. No tokens are stored.
- `place_order_input_schema.json` is the MCP tool's input schema as the client listed it (the tool was not called).
- Market data on this account was `dataFeedMode: "Delayed"` (about 10 minutes behind).
- The account had no orders, fills or positions in the last 30 days, so those three history files are empty.

## Files

- `describe_action.json`
- `describe_barType.json`
- `describe_cancel_order_examples.json`
- `describe_cancel_order_output.json`
- `describe_close_position_examples.json`
- `describe_close_position_output.json`
- `describe_environment_isolation.json`
- `describe_estimate_order_examples.json`
- `describe_estimate_order_output.json`
- `describe_fields.json`
- `describe_fill_history_output.json`
- `describe_index.json`
- `describe_market_history_examples.json`
- `describe_market_history_output.json`
- `describe_market_snapshot_output.json`
- `describe_modify_order_examples.json`
- `describe_modify_order_output.json`
- `describe_my_portfolio_output.json`
- `describe_ordStatus.json`
- `describe_orderType.json`
- `describe_order_details_output.json`
- `describe_order_history_output.json`
- `describe_place_order_examples.json`
- `describe_place_order_output.json`
- `describe_position_history_output.json`
- `describe_productType.json`
- `describe_response_fields.json`
- `describe_response_format.json`
- `describe_resultStatus.json`
- `describe_risk_settings_output.json`
- `describe_search_contracts_examples.json`
- `describe_search_contracts_output.json`
- `describe_timeInForce.json`
- `describe_user_profile_output.json`
- `estimate_order_MESZ6_buy1_market.json`
- `fill_history_last30d.json`
- `market_history_MESZ6_15min_x20.json`
- `market_history_MESZ6_1min_x5.json`
- `market_snapshot_MES.json`
- `my_portfolio.json`
- `order_history_last30d.json`
- `place_order_input_schema.json`
- `position_history_last30d.json`
- `risk_settings.json`
- `search_contracts_MES.json`
- `user_profile.json`
