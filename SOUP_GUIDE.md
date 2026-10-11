# Optional Soup model for the StockBot agent

You can run the trading agent without Soup: select **Rule agent** and begin with the offline demo,
then your own paper account. Soup is optional tooling for model training and serving. The supplied
`Soup-main.zip` contains source code, not a trained stock-selection model or broker credentials.

## What connects

StockBot's local planner sends numeric observations to a loopback chat-completions server and expects
one assistant text response in this form:

```json
{"symbols":["AAPL","MSFT"],"reason":"Choose these permitted candidates."}
```

The permitted symbols and available slots depend on the current observation. With **Momentum growth**
selected, only candidates passing the current-session price-growth filter are offered to the model.
The engine rechecks growth before buying; the model cannot override that filter. An empty symbols list
means wait. The response may not include order amounts, sell commands, stop prices, new ticker names,
code or changes to risk limits. StockBot checks the result and constructs any orders itself.
The model never receives broker API keys or direct brokerage tools. Exits are always handled by the
trading engine. Model-generated reasons are explanations to inspect, not verified financial facts.

The supplied Soup server defines text-only chat messages. The integration therefore uses JSON in
assistant text rather than assuming that server implements native tool-call response objects.

## Start a model you already have

The supplied Soup project requires **Python 3.10–3.12**. Use a separate virtual environment for it,
following its README and hardware guidance. For its transformers server, the serve extra alone only
installs the web server; the model also needs the training/inference dependencies. From its extracted
project folder, the documented extras can be installed with:

```bash
python -m venv .venv
```

Activate `.venv` for your operating system, then:

```bash
python -m pip install -e ".[serve,train]"
```

This installs a large dependency stack. It has not been installed or tested for this delivery.
Have a compatible, trusted model in `./output` first: neither ZIP provides its weights. Follow Soup's
documentation for acquiring or training one and assessing the hardware it needs. Do not assume that
fine-tuning a small sample of trades produces a useful financial model.

Start the server in its own terminal:

```bash
soup serve --model ./output --host 127.0.0.1 --port 8000 --max-tokens 256
```

Keep it bound to loopback. In StockBot choose **Local model agent**, set the model ID to the ID exposed
by the server (the documented `./output` example uses `output`), and keep the URL
`http://127.0.0.1:8000/v1`. Use paper mode to inspect responses and skipped buys. Broker keys belong
only in StockBot's credential fields, never in the model configuration or prompt.

StockBot makes one asynchronous request per price-bar interval. Its default timeout is five seconds;
its plans expire after sixty seconds. If your model is too slow or doesn't reliably return the required
JSON, the agent waits on new entries. It continues managing existing holdings without waiting for
model inference. The HTTP request timeout can be configured up to ten seconds; this does not extend
the lifetime of an entry plan. Rule agent ranks the checked candidates without a language model.
Select **Configured list** separately if you deliberately want the earlier entry baseline.

## Learning, fine-tuning and Agent Forge

StockBot already records outcomes and evaluates bounded plateau-setting changes on separate later
observations. Those strategy checks continue to operate with either planner. Live parameter changes
are disabled by default and require your separate local setting. Purchase budgets and loss thresholds
never change automatically.

You can separately export entry decisions as chat-format imitation examples:

```bash
python3 agent.py export-training --folder results/paper --output training/entry_examples.jsonl
```

Use the exported messages with Soup's documented chat-data/SFT workflow if you choose to train a
model. The file explicitly marks its label source and says it is not a profitability label. It
teaches what the prior planner selected, including waiting; it does not teach which stocks will win.
Training, selecting a model checkpoint and replacing your served model are separate operator steps.
No automatic model retraining or deployment has been implemented.

Soup's **Agent Forge** can synthesize tool-calling datasets from API specifications, plan a training
recipe, and score tool-call shapes. In the supplied implementation, `soup agent train` prints a plan;
it does not launch a trader. Schema-valid API calls are also not evidence that trades are profitable.
There is no need to give a trained model unrestricted brokerage API access for this agent to work.

This guide is based on the supplied Soup source: `docs/serving-and-export.md`,
`src/soup_cli/commands/serve.py`, `src/soup_cli/commands/agent.py`, and `pyproject.toml`.
The Soup project has not been modified or redistributed inside StockBot.
