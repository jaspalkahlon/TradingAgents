from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.analysts.turn import take_turn
from tradingagents.agents.context import get_instrument_context_from_state, get_language_instruction
from tradingagents.agents.tools import (
    get_corporate_announcements,
    get_global_news,
    get_india_market_news,
    get_macro_indicators,
    get_news,
    get_prediction_markets,
)
from tradingagents.dataflows.vendors.india.common import indian_listing

# The tools offered for any instrument.
BASE_TOOLS = (
    get_news,
    get_global_news,
    get_macro_indicators,
    get_prediction_markets,
)
# Added for NSE/BSE listings.
INDIA_TOOLS = (
    get_corporate_announcements,
    get_india_market_news,
)
# Its tool node is built from every tool it can be offered.
TOOLS = BASE_TOOLS + INDIA_TOOLS

INDIA_BRIEF = (
    " The instrument is listed in India, so also use get_corporate_announcements(start_date, end_date)"
    " for what the company filed with BSE in the window (results, board meetings, orders, ratings,"
    " insider and SAST disclosures, promoter pledges), which the market treats as primary news, and"
    " get_india_market_news(curr_date, look_back_days) for Indian market and economy headlines"
    " (RBI, FII/DII flows, Nifty, rupee). FRED covers US and euro-area data, not India."
)


def tools_for(ticker: str) -> tuple:
    """The tools offered for ``ticker``: the Indian ones only for an NSE/BSE listing."""
    return TOOLS if indian_listing(ticker or "") else BASE_TOOLS


def create_news_analyst(llm):
    def news_analyst_node(state):
        current_date = state["trade_date"]
        asset_type = state.get("asset_type", "stock")
        asset_label = "company" if asset_type == "stock" else "asset"
        instrument_context = get_instrument_context_from_state(state)

        system_message = (
            f"You are a news researcher tasked with analyzing recent news and trends over the past week. Please write a comprehensive report of the current state of the world that is relevant for trading and macroeconomics. Use the available tools: get_news(start_date, end_date) for news about the {asset_label} under analysis, get_global_news(curr_date, look_back_days, limit) for broader macroeconomic news, get_macro_indicators(indicator, curr_date, look_back_days) to ground macro commentary in actual data from FRED (e.g. 'cpi', 'core_pce', 'unemployment', 'fed_funds_rate', '10y_treasury', 'yield_curve'), and get_prediction_markets(topic, limit) for live market-implied probabilities of forward-looking events (e.g. 'Fed rate cut', 'recession 2026', geopolitical or sector events). Provide specific, actionable insights with supporting evidence to help traders make informed decisions."
            + (INDIA_BRIEF if indian_listing(state.get("company_of_interest") or "") else "")
            + """ Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."""
            + get_language_instruction()
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " Report what your tools support; another agent decides the trade."
                    " You have access to the following tools: {tool_names}."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}\n"
                    "{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        tools = tools_for(state.get("company_of_interest"))
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        result, report = take_turn(prompt, llm, tools, state["messages"])

        return {
            "messages": [result],
            "news_report": report,
        }

    return news_analyst_node
