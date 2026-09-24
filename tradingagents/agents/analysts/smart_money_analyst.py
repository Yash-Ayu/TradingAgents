# tradingagents/agents/analysts/smart_money_analyst.py

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
    get_option_chain_oi_data,
    get_volume_spike_analysis,
)

def create_smart_money_analyst(llm):
    def smart_money_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = get_instrument_context_from_state(state)

        tools = [
            get_option_chain_oi_data,
            get_volume_spike_analysis,
        ]

        system_message = (
            """You are an expert Institutional Order Flow Analyst. Your sole purpose is to track "Smart Money" 
            (FIIs, DIIs, and Large Operators) to identify where the big players are positioning themselves.
            
            Your analysis must focus on:
            1. **Option Chain Analysis**: Look for heavy Put Writing (Bullish) or Call Writing (Bearish). 
               Analyze the Put-Call Ratio (PCR) and sudden shifts in Open Interest (OI).
            2. **Volume Footprints**: Detect unusual volume spikes that indicate institutional accumulation 
               or distribution.
            3. **Order Blocks**: Identify price zones where big players have left significant footprints 
               (Demand/Supply zones) that are likely to act as strong magnets or barriers for price.
            
            Your Goal: Determine if the "Smart Money" is currently Bullish, Bearish, or Neutral. 
            If you see a massive mismatch between retail sentiment and institutional positioning, 
            highlight this as a high-probability reversal signal.
            
            Write a detailed report highlighting:
            - Current Institutional Sentiment.
            - Key Order Block levels.
            - PCR and OI trends.
            - Final verdict on whether the "Big Players" are supporting the current move.
            
            Append a Markdown table at the end of the report summarizing the Smart Money signals.
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
            "smart_money_report": report,
        }

    return smart_money_analyst_node
