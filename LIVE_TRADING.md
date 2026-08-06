# Live trading

> **The live order path in this build has never been executed against real
> funds. Its first real order is its first test.**
>
> This is not boilerplate caution. Paper trading was exercised end to end
> against live market data — entries, book-walking fills, settlement, breakers.
> The live path uses Polymarket's official `py-clob-client-v2` and
> is exercised only up to the point of submission. Order signing, balance
> checks, and error handling on the live path are **unverified**.

## The three locks

An order can only reach the venue when all three are open:

1. `LIVE_TRADING_ENABLED=1` in `.env`
2. A private key and online-verified CLOB V2 credentials present in the vault
3. Execution mode set to `live` in the dashboard

The dashboard shows which locks are open under **Runtime & keys**. Removing any
one of them stops live orders immediately.

## Before you enable it

**Read the code you are about to trust.** It is one file:
[`polybot/broker/live.py`](polybot/broker/live.py). It is short on purpose.

**Understand what gets signed.** Orders go out FAK (fill-and-kill) with a limit
price one tick through the spread. FAK is chosen so that nothing can be left
resting unattended on a five-minute window. The limit still caps the worst price
you can pay, but a FAK order can fill *partially* and the remainder is killed.

**Check your signature type.** Getting this wrong is the most common way to
find that orders sign but never fill:

| Type | Use when |
|---|---|
| `0` | The private key itself holds the pUSD (a plain EOA) |
| `1` | Funds are in a legacy Polymarket email/Magic proxy wallet |
| `2` | Funds are in a legacy browser-wallet Safe |
| `3` | Funds are in a CLOB V2 deposit wallet (EIP-1271) |

For proxy/deposit wallets the **funder address** must be the address holding
pUSD, not necessarily the address your key derives.

**Migrate old collateral and credentials.** CLOB V2 uses pUSD, not USDC.e, and
the legacy Python SDK no longer works in production. Deposits made through
polymarket.com are wrapped automatically. API-only wallets must wrap USDC.e
through the Collateral Onramp. If the vault predates April 2026, click
*Re-derive V2 CLOB keys* before checking the balance.

**Confirm allowances.** Polymarket's V2 exchange contracts need pUSD approval
before an order can fill. Use *Check pUSD balance* in the dashboard. A zero
allowance means the wallet still needs the V2 approvals.

## A sane first run

1. Set `LIVE_TRADING_ENABLED=1` and restart.
2. Set **max per trade** to the venue minimum, `$5`. Set the **daily cap** to
   something you are entirely willing to lose — `$25` is plenty to learn from.
3. Switch to `live`. Confirm the dialog.
4. **Watch the first order end to end.** Check the Activity tab, then check the
   position on Polymarket's own site. Confirm side, size, and price match.
5. Only then consider raising limits.

If the first order errors, it appears in Activity and in the footer. The engine
pauses itself on a `LiveTradingDisabled` error rather than silently falling back
to paper — a live order that fails is meant to be loud.

## What is not implemented

- **No position reconciliation with the venue.** The engine tracks what it
  believes it opened. It does not poll Polymarket for positions opened
  elsewhere, and it will not notice a fill it did not initiate.
- **No settlement against actual resolution.** Live positions settle in the
  local database against spot, exactly as paper ones do. Your real pUSD settles
  according to Chainlink, on Polymarket's schedule. **These can disagree**, so
  the dashboard's live P/L is an estimate, not your account balance.
- **No retry or partial-fill accounting.** A partially filled FAK is recorded at
  the venue's reported size, but the unfilled remainder is not re-submitted.

Given those gaps, treat live mode as an instrumented experiment, not as an
unattended trading system. Do not leave it running unwatched.
