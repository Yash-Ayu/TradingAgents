# tradingagents/agents/analysts/global_macro_analyst.py

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
    get_global_news,
    get_macro_indicators,
)

def create_global_macro_analyst(llm):
    def global_macro_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = get_instrument_context_from_state(state)

        tools = [
            get_global_news,
            get_macro_indicators,
        ]

        system_message = (
            """You are a Global Macro Strategist. Your role is to analyze the worldwide economic 
            landscape to determine the broader market tide that will influence the local instrument.
            
            Your analysis must focus on:
            1. **Global Market Correlates**: Analyze the movement of major global indices (e.g., S&P 500, Nasdaq, DAX) 
               and the US Dollar Index (DXY). A strong DXY often pressures emerging markets.
            2. **Macro Indicators**: Monitor central bank decisions (Fed, RBI), interest rate changes, 
               inflation data (CPI), and employment reports.
            3. **Global Risk Sentiment**: Use Global VIX and news from major financial hubs 
               to determine if the world is in a 'Risk-On' (Bullish) or 'Risk-Off' (Bearish) mood.
            4. **Geopolitical Events**: Track major international news that could trigger 
               sudden volatility in the local market.
            
            Your Goal: Provide a "Global Tide" verdict. Is the global environment providing a 
            tailwind (supportive) or a headwind (resistive) for the instrument?
            
            Write a detailed report highlighting:
 la. The overall Global Sentiment (Risk-On/Risk-Off).
            - Key macro triggers and their potential impact.
            - Correlation analysis with global peers.
            - Final verdict on the Global Macro influence.
            
            Append a Markdown table at the end of the report summarizing the Global Macro signals.
            """
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
                    " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable,"
                    " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop."
                    " You have access to the following tools: {tool_names}."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}\n"
                    "{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        chain = prompt | llm.bind_tools(tools)
        result = chain.invoke(state["messages"])

        report = ""
        if len(result.tool_calls) == 0:
            report = result.content

        return {
            "messages": [result],
            "global_macro_report": report,
        }

    return global_macro_analyst_node
