# API Quick Reference (Full Function Signatures)

## Market Data API (OpenQuoteContext)

### Subscription Management (4)

```
subscribe(code_list, subtype_list, is_first_push=True, subscribe_push=True, is_detailed_orderbook=False, extended_time=False, session=Session.NONE)  -- Subscribe (consumes subscription quota, 1 quota per stock per type; check quota with query_subscription before calling; session only for US stock real-time Candlestick/intraday/tick-by-tick, OVERNIGHT not supported)
unsubscribe(code_list, subtype_list, unsubscribe_all=False)  -- Unsubscribe (must wait at least 1 minute after subscribing)
unsubscribe_all()  -- Unsubscribe all
query_subscription(is_all_conn=True)  -- Query subscription status (check before calling subscribe)
```

### Real-time Data - Requires Subscription First (6)

```
get_stock_quote(code_list)  -- Get real-time quotes
get_cur_kline(code, num, ktype=KLType.K_DAY, autype=AuType.QFQ)  -- Get real-time Candlestick
get_rt_data(code)  -- Get real-time time-sharing
get_rt_ticker(code, num=500)  -- Get real-time tick-by-tick
get_order_book(code, num=10)  -- Get real-time order book
get_broker_queue(code)  -- Get real-time broker queue (HK only)
```

### Snapshot & Historical (4)

```
get_market_snapshot(code_list)  -- Get snapshot (no subscription needed, max 400 per call)
request_history_kline(code, start=None, end=None, ktype=KLType.K_DAY, autype=AuType.QFQ, fields=[KL_FIELD.ALL], max_count=1000, page_req_key=None, extended_time=False, session=Session.NONE)  -- Get historical Candlestick (consumes historical Candlestick quota, check remaining quota with get_history_kl_quota before calling; max_count max 1000 per call, use page_req_key for pagination; session only for US session-based historical Candlestick, OVERNIGHT not supported)
get_rehab(code)  -- Get adjustment factor
get_history_kl_quota(get_detail=False)  -- Query historical Candlestick quota (check before calling request_history_kline)
```

### Basic Info (5)

```
get_stock_basicinfo(market, stock_type=SecurityType.STOCK, code_list=None)  -- Get stock static info
get_global_state()  -- Get market states (returns dict, keys include market_hk/market_us/market_sh/market_sz/market_hkfuture/market_usfuture/server_ver/qot_logined/trd_logined etc.)
request_trading_days(market=None, start=None, end=None, code=None)  -- Get trading calendar
get_market_state(code_list)  -- Get market state
get_stock_filter(market, filter_list, plate_code=None, begin=0, num=200)  -- Stock screener
```

### Plates/Sectors (3)

```
get_plate_list(market, plate_class)  -- Get plate list
get_plate_stock(plate_code, sort_field=SortField.CODE, ascend=True)  -- Get stocks in plate
get_owner_plate(code_list)  -- Get stock's plates
```

### Derivatives (5)

```
get_option_chain(code, index_option_type=IndexOptionType.NORMAL, start=None, end=None, option_type=OptionType.ALL, option_cond_type=OptionCondType.ALL, data_filter=None)  -- Get option chain
get_option_expiration_date(code, index_option_type=IndexOptionType.NORMAL)  -- Get option expiration dates
get_referencestock_list(code, reference_type)  -- Get related stocks (underlying/warrants/CBBCs/options)
get_future_info(code_list)  -- Get futures contract info
get_warrant(stock_owner='', req=None)  -- Get warrants/CBBCs
```

### Capital (2)

```
get_capital_flow(stock_code, period_type=PeriodType.INTRADAY, start=None, end=None)  -- Get capital flow
get_capital_distribution(stock_code)  -- Get capital distribution
```

### Watchlist (3)

```
get_user_security_group(group_type=UserSecurityGroupType.ALL)  -- Get watchlist groups
get_user_security(group_name)  -- Get watchlist stocks
modify_user_security(group_name, op, code_list)  -- Modify watchlist
```

### Price Alerts (2)

```
get_price_reminder(code=None, market=None)  -- Get price alerts
set_price_reminder(code, op, key=None, reminder_type=None, reminder_freq=None, value=None, note=None)  -- Set price alert
```

### IPO (1)

```
get_ipo_list(market)  -- Get IPO list
```

**Market Data API Subtotal: 35**

---

## Trading API (OpenSecTradeContext / OpenFutureTradeContext)

### Account (3)

```
get_acc_list()  -- Get trading account list
unlock_trade(password=None, password_md5=None, is_unlock=True)  -- Unlock/lock trading (⚠️ This skill does not unlock via API; user must unlock manually in OpenD GUI)
accinfo_query(trd_env=TrdEnv.REAL, acc_id=0, acc_index=0, refresh_cache=False, currency=Currency.HKD, asset_category=AssetCategory.NONE)  -- Query account funds
```

### Order Placement & Modification (3)

```
place_order(price, qty, code, trd_side, order_type=OrderType.NORMAL, adjust_limit=0, trd_env=TrdEnv.REAL, acc_id=0, acc_index=0, remark=None, time_in_force=TimeInForce.DAY, fill_outside_rth=False, aux_price=None, trail_type=None, trail_value=None, trail_spread=None, session=Session.NONE)  -- Place order (rate limit: 15/30s; session only for US stocks, supports RTH/ETH/OVERNIGHT/ALL)
modify_order(modify_order_op, order_id, qty, price, adjust_limit=0, trd_env=TrdEnv.REAL, acc_id=0, acc_index=0, aux_price=None, trail_type=None, trail_value=None, trail_spread=None)  -- Modify/cancel order (rate limit: 20/30s)
cancel_all_order(trd_env=TrdEnv.REAL, acc_id=0, acc_index=0, trdmarket=TrdMarket.NONE)  -- Cancel all orders
```

### Order Query (3)

```
order_list_query(order_id="", order_market=TrdMarket.NONE, status_filter_list=[], code='', start='', end='', trd_env=TrdEnv.REAL, acc_id=0, acc_index=0, refresh_cache=False)  -- Query today's orders
history_order_list_query(status_filter_list=[], code='', order_market=TrdMarket.NONE, start='', end='', trd_env=TrdEnv.REAL, acc_id=0, acc_index=0)  -- Query historical orders
order_fee_query(order_id_list=[], acc_id=0, acc_index=0, trd_env=TrdEnv.REAL)  -- Query order fees
```

### Deal Query (2)

```
deal_list_query(code="", deal_market=TrdMarket.NONE, trd_env=TrdEnv.REAL, acc_id=0, acc_index=0, refresh_cache=False)  -- Query today's deals
history_deal_list_query(code='', deal_market=TrdMarket.NONE, start='', end='', trd_env=TrdEnv.REAL, acc_id=0, acc_index=0)  -- Query historical deals
```

### Position & Funds (4)

```
position_list_query(code='', position_market=TrdMarket.NONE, pl_ratio_min=None, pl_ratio_max=None, trd_env=TrdEnv.REAL, acc_id=0, acc_index=0, refresh_cache=False)  -- Query positions
acctradinginfo_query(order_type, code, price, order_id=None, adjust_limit=0, trd_env=TrdEnv.REAL, acc_id=0, acc_index=0, session=Session.NONE)  -- Query max buy/sell quantity (session only for US stocks, supports RTH/ETH/OVERNIGHT/ALL)
get_acc_cash_flow(clearing_date='', trd_env=TrdEnv.REAL, acc_id=0, acc_index=0, cashflow_direction=CashFlowDirection.NONE)  -- Query account cash flow
get_margin_ratio(code_list)  -- Query margin ratio
```

**Trading API Subtotal: 15**

---

## Push Handlers (9)

### Market Data Push (7)

```
StockQuoteHandlerBase   -- Quote push callback
OrderBookHandlerBase    -- Order book push callback
CurKlineHandlerBase     -- Candlestick push callback
TickerHandlerBase       -- Tick-by-tick push callback
RTDataHandlerBase       -- Time-sharing push callback
BrokerHandlerBase       -- Broker queue push callback
PriceReminderHandlerBase -- Price alert push callback
```

### Trade Push (2)

```
TradeOrderHandlerBase   -- Order status push callback
TradeDealHandlerBase    -- Deal push callback
```

Note: Trade pushes do not require separate subscription; they are automatically received after setting the Handler.

---

## Base Interfaces

```
OpenQuoteContext(host='127.0.0.1', port=11111, ai_type=1)  -- Create market data connection
OpenSecTradeContext(filter_trdmarket=TrdMarket.NONE, host='127.0.0.1', port=11111, security_firm=SecurityFirm.FUTUSECURITIES, ai_type=1)  -- Create securities trading connection (security_firm must be set based on the user's brokerage, see FUTU_SECURITY_FIRM enum table)
OpenFutureTradeContext(host='127.0.0.1', port=11111, security_firm=SecurityFirm.FUTUSECURITIES, ai_type=1)  -- Create futures trading connection (security_firm same as above)
ctx.close()  -- Close connection
ctx.set_handler(handler)  -- Register push callback
SysNotifyHandlerBase  -- System notification callback
```

**Total API Count: Market Data 35 + Trading 15 + Push Handlers 9 + Base 6 = 65 interfaces**

## SubType Subscription Types (Full List)

| SubType | Description | Corresponding Push Handler |
|---------|-------------|---------------------------|
| `QUOTE` | Quote | `StockQuoteHandlerBase` |
| `ORDER_BOOK` | Order Book | `OrderBookHandlerBase` |
| `TICKER` | Tick-by-tick | `TickerHandlerBase` |
| `K_1M` ~ `K_MON` | Candlestick | `CurKlineHandlerBase` |
| `RT_DATA` | Time-sharing | `RTDataHandlerBase` |
| `BROKER` | Broker Queue (HK only) | `BrokerHandlerBase` |

## Key Enum Values

- **TrdSide**: `BUY` | `SELL`
- **OrderType**: `NORMAL` (limit) | `MARKET` (market)
- **TrdEnv**: `REAL` | `SIMULATE`
- **ModifyOrderOp**: `NORMAL` (modify) | `CANCEL` (cancel)
- **TrdMarket**: `HK` | `US` | `CN` | `HKCC` | `SG`
- **Session**: `NONE` | `RTH` (regular hours) | `ETH` (extended hours) | `OVERNIGHT` | `ALL` — Subscribe only supports RTH/ETH/ALL (OVERNIGHT not supported); Place order supports RTH/ETH/OVERNIGHT/ALL
