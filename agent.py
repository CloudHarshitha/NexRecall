import os
import operator
from typing import TypedDict, Annotated, Sequence

from langchain_openai import ChatOpenAI
from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.graph import StateGraph, END

from shared_schema import retrieve_meeting_context

# Configure the standard openai Python client via langchain_openai to point to Nexus AI
llm = ChatOpenAI(
    model="gpt-4o",  # Can be configured based on requirements
    api_key=os.environ.get("NEXUS_API_KEY", "default-key"),
    base_url=os.environ.get("NEXUS_API_BASE_URL", "https://nexusapi.navigatelabs.ai")
)

class AgentState(TypedDict):
    """
    The state for our LangGraph state machine.
    - messages: Holds the conversation history and current user prompt.
    - context: Holds the retrieved meeting context from our mock DB.
    - intent: The classified intent (Action Items vs General History).
    - final_response: The generated response to show to the user.
    """
    messages: Annotated[Sequence[BaseMessage], operator.add]
    context: list[dict]
    intent: str
    final_response: str

def router_node(state: AgentState):
    """
    Router Node: Analyzes the user's prompt using the Nexus AI LLM to classify 
    it as either a request for "Action Items" or "General History".
    """
    user_query = state["messages"][-1].content
    
    prompt = (
        "Analyze the following user query and classify it into one of two categories: "
        "'Action Items' or 'General History'.\n\n"
        f"Query: '{user_query}'\n\n"
        "Respond with EXACTLY the category name, nothing else."
    )
    
    # Classify the intent
    response = llm.invoke([HumanMessage(content=prompt)])
    intent_classification = response.content.strip()
    
    # Normalize intent string to handle small variations from the LLM
    if "action" in intent_classification.lower():
        intent = "Action Items"
    else:
        intent = "General History"
        
    # Pre-fetch the context based on the query for the downstream nodes
    context = retrieve_meeting_context(user_query)
        
    return {"intent": intent, "context": context}

def router_edges(state: AgentState):
    """
    Conditional edge logic: Routes to the Extractor Node or Synthesizer Node 
    based on the classified intent from the Router Node.
    """
    if state.get("intent") == "Action Items":
        return "extractor_node"
    return "synthesizer_node"

def extractor_node(state: AgentState):
    """
    Extractor Node (Tasks): Uses a specialized prompt to extract only actionable tasks 
    from the context provided by retrieve_meeting_context.
    """
    user_query = state["messages"][-1].content
    context = state.get("context", [])
    
    # Format the context for the LLM
    context_str = "\n".join([f"- {item['text']} (Date: {item['metadata'].get('date')})" for item in context])
    
    prompt = (
        "You are an expert project manager. Extract ONLY actionable tasks from the following meeting context. "
        "If there are no actionable tasks related to the query, clearly state so.\n\n"
        f"Context:\n{context_str}\n\n"
        f"User Query: {user_query}"
    )
    
    response = llm.invoke([HumanMessage(content=prompt)])
    return {"final_response": response.content}

def synthesizer_node(state: AgentState):
    """
    Synthesizer Node (History): Uses a general Q&A prompt to answer historical questions 
    based on the retrieved context.
    """
    user_query = state["messages"][-1].content
    context = state.get("context", [])
    
    # Format the context for the LLM
    context_str = "\n".join([f"- {item['text']} (Date: {item['metadata'].get('date')})" for item in context])
    
    prompt = (
        "You are an AI meeting assistant. Answer the following historical question based ONLY on the "
        "provided meeting context. If the context does not contain the answer, say so.\n\n"
        f"Context:\n{context_str}\n\n"
        f"User Query: {user_query}"
    )
    
    response = llm.invoke([HumanMessage(content=prompt)])
    return {"final_response": response.content}

# Construct the LangGraph state machine
graph = StateGraph(AgentState)

# Add nodes
graph.add_node("router_node", router_node)
graph.add_node("extractor_node", extractor_node)
graph.add_node("synthesizer_node", synthesizer_node)

# Add edges and conditional routing
graph.set_entry_point("router_node")
graph.add_conditional_edges("router_node", router_edges)
graph.add_edge("extractor_node", END)
graph.add_edge("synthesizer_node", END)

# Compile the graph
app_graph = graph.compile()
