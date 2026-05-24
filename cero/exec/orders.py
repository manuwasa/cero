"""
Order placement and management.

Wraps the exchange adapter with our specific order flow:
  1. Build order params (entry, SL, TP, size)
  2. Run safety checks
  3. Submit entry order
  4. Attach OCO (SL + TP)
  5. Record trade in DB
  6. Watch for fills via WebSocket
  7. On close, update PnL stats

TODO (Claude Code):

async def place(signal: Signal) -> Trade:
    if not safety_checks_pass(signal):
        raise SafetyCheckFailed(...)
    entry_order = await exchange.create_order(...)
    sl_order = await exchange.create_stop_loss(...)
    tp_order = await exchange.create_take_profit(...)
    return await db.record_trade(entry_order, sl_order, tp_order)

def safety_checks_pass(signal: Signal) -> bool:
    # tripped? max_positions? price drift? news blackout?
    ...
