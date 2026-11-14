"""Run TradingAgents on NSE/BSE tickers with Claude.

Usage:
    .venv/bin/python run_india.py RELIANCE.NS                # today's date
    .venv/bin/python run_india.py TCS.NS INFY.NS 2026-11-13  # several tickers, given date

Needs ANTHROPIC_API_KEY (or PRODIFY_CLAUDE_KEY) in the environment. Reports are saved under ./reports.
"""
import datetime as dt
import os
import re
import sys

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

# Claude Code cloud sessions reserve ANTHROPIC_API_KEY for their own login and clear
# it, so the key can also be supplied as PRODIFY_CLAUDE_KEY.
if not os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("PRODIFY_CLAUDE_KEY"):
    os.environ["ANTHROPIC_API_KEY"] = os.environ["PRODIFY_CLAUDE_KEY"]
if not os.environ.get("ANTHROPIC_API_KEY"):
    sys.exit("Set ANTHROPIC_API_KEY (or PRODIFY_CLAUDE_KEY in a Claude Code cloud environment).")

args = sys.argv[1:] or ["RELIANCE.NS"]
date = args.pop() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", args[-1]) else dt.date.today().isoformat()
tickers = args or ["RELIANCE.NS"]

config = DEFAULT_CONFIG.copy()
config.update(
    llm_provider="anthropic",
    quick_think_llm=os.getenv("TRADINGAGENTS_QUICK_THINK_LLM", "claude-sonnet-5"),  # analysts, tool calls
    deep_think_llm=os.getenv("TRADINGAGENTS_DEEP_THINK_LLM", "claude-opus-5-5"),    # research manager, final call
    max_debate_rounds=int(os.getenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "1")),        # more rounds = more cost
    max_risk_discuss_rounds=int(os.getenv("TRADINGAGENTS_MAX_RISK_ROUNDS", "1")),
    results_dir=os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports"),
)

ta = TradingAgentsGraph(debug=False, config=config)
for t in tickers:
    print(f"\n=== {t} @ {date} ===")
    _, decision = ta.propagate(t, date)
    print(decision)
print(f"\nFull reports: {config['results_dir']}")
