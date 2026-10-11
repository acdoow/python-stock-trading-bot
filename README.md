# StockBot v4 — intraday growth entries and independent exits

The supplied settings use **up to $2 in each of five companies, with a $10 basket cap**, assessed separately.
New buys require observed intraday price growth by default. Qualifying stocks are ranked by recent growth.
It exits a position after a **50% decline** or places a sell limit at the mean price of a detected
near-high plateau. It supports offline examples, Alpaca paper orders, live read-only preview,
and locally enabled live execution. It now plans permitted entries, records decisions, executes
guarded orders, and reviews outcomes. These rules have **not been shown to be profitable**.

## Start on Windows

1. Install Python 3.10 or later with Tcl/Tk from https://www.python.org/downloads/windows/.
2. Extract the entire ZIP. Keep all files together in a writable folder.
3. Install Windows timezone data once: `py -3 -m pip install -r requirements.txt`.
4. Double-click `start_windows.bat`.
5. Leave **Rule agent** selected and click **Run offline agent demo**. It uses invented prices
   and a fake broker, with no network or model requests.

On macOS/Linux, run `python3 desktop.py`. Install `tzdata` if the system lacks America/New_York
timezones. The trading engine otherwise uses the Python standard library.

The desktop session settings let you set dollars per stock, the total capital cap, loss percentage,
buying rule, growth lookback, minimum session rise, and minimum recent rise.
For $10 total across five stocks, use **$2 per stock and a $10 cap**. Advanced rules and the ticker
universe are in `rules.json`. Desktop setting changes apply to the current run; edit `rules.json`
to persist defaults. API keys remain in memory and are never saved in this project.

If upgrading v2 or v3, stop the old program and wait for it to finish before replacing code. Preserve its `results` folder,
especially `basket_state.json`: v4 uses the same ownership, fill and pending-order journal.
The ZIP does not contain account state or credentials. Do not run both versions against one account.
Use the new `rules.json` for the supplied growth defaults, or add its `momentum` section to your existing
settings. Old configurations without that section preserve their configured-list entry behavior.

## The agent and its decisions

The cycle observes the account, reconciles actual fills, manages each existing position, evaluates
learning data, considers new entries, and records its decisions. The desktop now uses `TradingAgent`.

| Component | Responsibility |
| --- | --- |
| Rule agent, the default | Rank growth-qualified stocks in the configured watchlist by recent rise; no language model required |
| Optional local model agent | Propose a subset of checked candidates, or wait; reasons are model-generated text and may be inaccurate |
| Trading engine | Recheck cash, exposure, fresh quotes, growth eligibility, fractional eligibility, position count and order sizes; handle all loss and plateau exits |
| Order guard | Reject unplanned/oversized buys, conflicting orders, overselling, arbitrary stop prices and unrecorded profit targets |
| Decision history | Record entry observations, plans, order intents, submission results, exit transitions and outcome reviews |
| Learner | Test bounded plateau-setting changes on later observations, under the rules described below |

The local model receives only a small numeric observation, growth-screen results and the approved ticker list; it receives
no API keys, account UUID, broker order IDs, news, browser access or shell tools. It can propose buys
only among the first twenty eligible candidates from the configured universe. The growth filter applies
before both planners, and the engine checks it again with a fresh quote before each buy. The model cannot change
purchase amounts, add tickers, sell shares, move a stop or change learning/risk permissions.
Exits remain the strategy's independent, deterministic decisions. This is deliberate: the language
model helps choose entries, while the execution engine retains authority over orders and protection.

Model requests run on a separate thread after existing holdings have been managed. At most one request
starts per configured price-bar interval. Replies may be used on a later poll and expire within sixty
seconds of their observation; current candidates and all entry checks are validated again. An invalid,
unavailable, truncated or expired reply defers new buys. There is no automatic fallback to buying from
the rule list when the local model is selected. Existing exit management continues without waiting for
inference. A slow model may make no purchases, particularly with the default five-second request timeout.

The local interface accepts JSON text through `/v1/chat/completions`, matching the server in the supplied
Soup source. It does not require that server to support function-call response objects. Only HTTP on a
numeric loopback address is accepted, with proxies and redirects disabled. See **SOUP_GUIDE.md** for
optional setup. No model is bundled, downloaded, trained or started automatically.

`agent_config.json` holds planner defaults and the local server URL/model ID. Settings in the desktop
apply to that session. The offline agent demo always uses the rule planner, even if you selected a local
model for broker runs. With growth enabled, it starts with completed invented opening-session bars
that satisfy the filter, then demonstrates independent exits on five invented paths. Different growth
settings can produce different demo purchases. Those results say nothing about actual stock performance.

The result folder contains `agent_decisions.jsonl` and `agent_memory.json` in addition to the engine's
journal. Memory summarizes recorded outcomes; it does not turn generated explanations into trading
authority. A language model is not trained on trade outcomes during a run. The bounded exit learner
remains the only automatic parameter-update mechanism.

## How the strategy works

### Buying when prices show growth

**Momentum growth** is selected in the supplied desktop defaults. It observes share prices during the
current regular session; it does not measure company revenue, earnings, news sentiment or future growth.
It scans the configured watchlist rather than discovering companies across the entire market.

| Entry check | Supplied default |
| --- | --- |
| Completed recent observations | Six consecutive five-minute bars, covering 30 minutes |
| Growth from the session open | Current bid at least 0.5% above the opening price of the first IEX session bar |
| Recent-window growth | Both the last completed close and current bid at least 0.3% above the first window bar's opening price |
| Sustained rising observations | At least three of the five closing-price steps are upward |
| Avoid a recent reversal | Current bid no more than 0.3% below the highest price in the recent window |
| Fresh complete data | Today's opening bar must exist; the recent window must have no gaps and a recent completed bar |
| Ordering | Strongest current recent-window growth first, then session growth and ticker |

These percentages are adjustable starting points for paper experiments, not proven profitable settings.
With the defaults, the earliest qualifying decision is after the first 30 minutes of the regular session
(approximately 9:00 a.m. Chicago time). A longer window waits longer. The agent may buy fewer than five
stocks or wait with all cash if none qualify. A fresh reversal can invalidate a plan before submission.
Historical bars are cached briefly; each decision and final order check recomputes eligibility using a
fresh checked quote. Only completed current-session bars are used. Missing data causes waiting.

The log shows `ENTRY SYMBOL: PASS` or `ENTRY SYMBOL: WAIT`, with the reason. Agent decision history also
records the growth measurements. Growth settings stay fixed during a run and are not changed by the
exit learner. **Configured list** restores the earlier list-based baseline for comparison; it removes
the growth requirement, so use that selection deliberately.

The remaining price-data and account checks still apply. A rising price can reverse immediately after
buying; this filter is not a forecast. A holding that later stops qualifying is still managed by the
existing loss and plateau exits. Growth eligibility alone does not sell an existing position.

| Rule | Example default |
| --- | --- |
| Select companies | Growth-qualified stocks from the editable watchlist, ranked by recent rise; up to five holdings |
| Initial purchase | Up to $2 per company in fractional shares; no more than five positions |
| Total capital | $10 cap on new purchases, including reserved orders; never increased by learning |
| Independent loss exit | Share price reaches 50% below its actual average entry price; a $2 holding would be approximately $1 before costs |
| Observe a rise | A prospective target must be at least 2% above entry and cover the configured estimated trading costs |
| Detect a plateau | 12 completed five-minute bars, a narrow range, small slope, repeated turns, and a mean near the highest observed price |
| Ask for profit | Sell limit at the plateau's arithmetic mean, rounded upward to the allowed price increment |
| Refill a vacant slot | Wait until the next broker-calendar trading session after a completed sale before considering another purchase |

The plateau window is approximately one hour. Default observations must fluctuate within a 1% range,
have at least three direction changes, a small slope between the first and second halves, and a mean
within 1.5% of the highest price observed since purchase. The peak uses observed quotes and completed
bar highs; it is **not knowledge of the eventual highest price**. Windows reset after observation gaps.

A stock can pause, trigger a sale, and then rise again. A steadily rising stock may never trigger a
plateau. A stock may rise, fall without a plateau, and later lose money. Random selection and the sample
company names are baselines for testing, not investment recommendations or a current popularity ranking.

Up to five positions are managed independently: a loss in one does not cause the others to be sold.
Market purchases are bounded by cash and reserved orders, not margin buying power. A one-cent cash buffer
may make the last purchase $1.99 if the account has exactly $10. Existing price appreciation can lift
market exposure above the cap; the cap prevents further entries, rather than forcing liquidation.

## Connect Alpaca

Create and manage your own account at https://app.alpaca.markets/. Live-account eligibility and
permissions depend on the broker. Use a dedicated account with no manual holdings, unrelated orders,
other bots, deposits/withdrawals while running, or shared trading authority.

The desktop offers three connection modes:

- **Paper orders:** automatic virtual-money buys and sells, using PAPER API keys.
- **Live preview:** uses LIVE API keys to read the account and show candidate orders; sends no orders or cancellations.
- **Live orders:** uses LIVE API keys and submits real orders when you click Start live orders. Requires the exact account UUID.

Live preview shows the account UUID in its **local log**. Copy it into the Live account UUID field
before enabling live orders. This is the API account ID, not the public account number. The bot
verifies it against the authenticated account before submitting anything. Keep keys on your own
computer; do not send them in chat or screenshots.

Live requests use the fixed host `https://api.alpaca.markets`; paper requests use
`https://paper-api.alpaca.markets`. Read-only requests and order permissions are separate in code.
The author has not authenticated this code against either broker environment. The user's v3 paper
account/clock connection succeeded; v4 order execution still needs paper tests of complete fills,
rejected orders, cancellations, market closures and unfilled targets.

Market data uses **IEX**, a single exchange. It is not a consolidated view of all U.S. exchanges.
Orders may execute at different prices from IEX observations. Missing/stale/wide quotes cause a
symbol to be skipped; sparse IEX bars can make plateau detection slower or prevent it.

## Exits and their practical limits

After an entry is confirmed, the bot attempts to place a broker-held **fractional DAY stop order**.
This is stronger protection than relying solely on polling, but it is not a guarantee:

- The opening trade is unprotected until its fill is reconciled and the stop is accepted.
- Stop fills can be worse than the trigger, especially after a price gap. The 50% threshold is not a maximum loss.
- Fractional DAY orders expire each trading day. The running bot attempts to renew them during the next regular session.
- Fractional holdings are not given simultaneous independent stop and profit orders for the same shares.
  Before switching to a plateau sell limit, the bot requests cancellation of the stop, waits for confirmed
  cancellation/execution, and checks the remaining quantity. There is a protection gap during this transition.
- While a profit limit is active, it may stay unfilled. If a checked price reaches the loss threshold,
  the bot requests cancellation and waits for confirmation before sending a market exit for the remaining shares.
  If the program is stopped during this period, that loss response will not occur.
- Broker restrictions, halts, connectivity failures, order rejections, sparse data and partial fills can delay exits.
- A pending profit order is never counted as a realized gain. Only confirmed fills enter realized proceeds.

The bot processes partial fills, avoids blind order resubmission after a timeout, and does not deliberately sell short.
It pauses on an account mismatch, unknown holdings/orders, uncertain submissions or inconsistent fills.
Corporate actions, revised fills, external trading and every broker restriction are not fully handled.
This remains a prototype, not a production-certified financial system.

A **50% loss needs a 100% gain on the remaining money to recover**. If a $10 position loses $5,
five separate $1 gross wins merely offset that one loss, before costs. The loss threshold reproduces
your example and is editable; it has not been established as an effective risk setting.

## Learning and growth

The bot records each trade, its observed price path, gross realized profit/loss, and estimated net profit/loss.
It keeps collecting a fixed 120-bar research horizon after a sale when possible, so alternative exits
can be compared against the same later observations rather than a path truncated at the actual sale.
This requires the program and data access to keep running.

The learning routine:

1. Waits for at least 30 completed observed episodes; one win or loss does not cause a parameter change.
2. Chooses one challenger on earlier training episodes from a bounded grid of plateau windows (6, 12, 18 bars)
   and range widths (0.5%, 1%, 2%). It does not learn stock popularity, new tickers, budgets or loss percentages.
3. Tests that single challenger on the latest ten episodes. Overlapping training/validation horizons are purged;
   at least twenty training episodes must remain. Ten new complete episodes are required before reevaluation.
4. Requires average estimated improvement of at least 0.5 percentage points, improvement on at least 60% of
   validation episodes, and no worse worst validation return. These are heuristic gates, not statistical proof.
5. Can apply a passing update to **new paper trades**. Existing trades keep their original plateau profile.

**Automatic live learning is disabled by default.** The desktop has a separate switch to permit bounded,
evaluated plateau updates for new live trades. The CLI has `--enable-live-learning`. Even when enabled,
the model never increases dollars per stock, the capital cap, or the permitted loss threshold.

The counterfactual fill model is simplified: targets can first fill on a later observed bar, estimated
slippage/costs are used, fractional DAY targets expire, and a loss event is assumed first when both
thresholds are touched. API/cancellation latency and every market-microstructure effect are not simulated.
Stocks in the same basket can be correlated, and small/repeated samples can overfit. An evaluated update
can perform worse later. The learner does not establish that the strategy is profitable.

`estimated_roundtrip_cost` defaults to **$0.02 as a modeling placeholder**, not a verified broker fee quote.
It excludes taxes and may understate actual trading costs. The journal labels gross proceeds separately
from estimated net results. Open positions marked at a research horizon are not recorded as realized sales.

## Settlement and account rules

Most U.S. equity trades currently settle T+1. Reusing unsettled sale proceeds can cause cash-account
violations. This bot conservatively waits until the next broker-calendar session after a sale before
new entries, but it is not a complete settlement/accounting engine and does not certify the account's
available settled funds. Account funding, deposit holds and broker permissions still matter.

Use current broker/account rules. Current Alpaca documentation describes intraday margin rules. It does not borrow
against margin buying power or override broker restrictions. Official current sources are listed below.

## Stop, restart and recovery

Click Stop agent, close the window, or press Ctrl+C. An in-flight broker request may finish first.
Existing orders and holdings remain in your brokerage account. Inspect and manage them in its dashboard.
Broker-held DAY exits will expire; keeping the computer awake is necessary for renewal and exit changes.

Create `results/paper/STOP_BOT` or `results/live/STOP_BOT` to block that run. Remove it only when ready to restart.
Keep `basket_state.json` in the appropriate results folder: it records ownership, pending client order IDs,
fill accounting, profiles and settlement pauses. Do not start copies in other folders against the same account.
The results-folder lock and an account-wide OS lock prevent two runs using the same account on one computer.
They do not protect against another computer using the same account.

If a submission is uncertain, look up the exact journaled client order ID in the correct broker dashboard.
Wait for the API and dashboard to agree. If it exists, restart to reconcile it. If the broker confirms
it was never accepted, stop the program and remove only that pending entry. For an unfilled rejected
entry with zero owned quantity, remove its empty trade record as well; keep `seen_buys` to avoid another
same-day purchase. Do not clear a slow/uncertain order merely because it has not appeared yet.
Rejected protection or exit orders require immediate manual account review.

## Terminal commands

Run from this folder. On Windows, replace `python3` with `py -3`.

```bash
python3 agent.py demo
python3 agent.py run --mode paper --execute
python3 agent.py run --mode live --once
python3 -m unittest discover -s tests -v
```

Optional local model planning with a server you have started:

```bash
python3 agent.py run --mode paper --execute --planner local --model output
```

Use a continuous run for local-model planning. `--once` performs one broker-management cycle;
it can start a model request but ends before a later cycle could use its reply.

The previous engine commands are also available:

```bash
python3 adaptive.py demo
python3 adaptive.py learn adaptive_demo/synthetic_episodes.json
python3 adaptive.py run --mode paper --execute
python3 adaptive.py run --mode live --once
```

The terminal reads `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` from your local environment.
Use the desktop to avoid putting secrets into command history.

Real-money execution, after local paper validation and account binding:

```bash
python3 agent.py run --mode live --execute --account-id YOUR_ACTUAL_ACCOUNT_UUID
```

Optional bounded live learning adds `--enable-live-learning`. Selecting live without `--execute`
is read-only. Separate result folders are used for paper, live preview and live execution.

Export recorded entry choices for optional later model-training experiments:

```bash
python3 agent.py export-training --folder results/paper --output training/entry_examples.jsonl
```

These chat-format examples teach imitation of recorded decisions. They are labeled with their source,
not as profitable trades. The export excludes account IDs and API keys, does not train a model, and
cannot overwrite the decision journal. See the Soup guide before using them.

## Validation and files

86 offline checks passed at v3 validation, including individual loss exits, plateau detection,
partial fills, cancellation confirmation, settlement pauses, expired stops, uncertain submissions,
read-only live mode, account binding, stale data, and separation of training/validation observations.
Agent checks cover rejected model commands/risk changes, unapproved tickers, expired plans, oversized
buys, overselling, conflicting exits, restart memory, and loss handling during blocked inference.
Synthetic scenarios include losses and a plateau followed by another rise. No real trades were placed.
Authenticated paper/live requests, a running Soup model, and the Windows desktop window have not been
tested in this environment. The model HTTP contract was checked with mock replies, not real inference.

- `desktop.py`: v3 trading-agent desktop interface.
- `agent.py`: entry planners, order guard, decision memory, outcome review, CLI and training-data export.
- `agent_config.json`: rule/local planner defaults and local model settings.
- `simulator.py`: fake broker and invented paths for the full offline agent demo.
- `SOUP_GUIDE.md`: optional local model setup and training limitations.
- `adaptive.py`: fractional order adapter, strategy, journal and learner.
- `rules.json`: editable example defaults.
- `requirements.txt`: timezone data for systems that need it.
- `tests/test_adaptive.py`: v2 offline checks.
- `tests/test_agent.py`: agent and local model interface checks.
- `bot.py`, `gui.py`, `config.json`, `LEGACY_README.md`, `tests/test_bot.py`: original moving-average paper version, preserved separately.

Official sources checked October 3, 2026:

- https://docs.alpaca.markets/us/docs/fractional-trading
- https://docs.alpaca.markets/us/docs/orders-at-alpaca
- https://docs.alpaca.markets/us/reference/stockbars
- https://docs.alpaca.markets/us/docs/paper-trading
- https://docs.alpaca.markets/us/docs/user-protection
- https://docs.alpaca.markets/us/docs/the-intraday-margin-rule
- https://www.finra.org/investors/insights/frequent-intraday-trading
