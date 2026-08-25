"""Node functions for the LangGraph workflow.

Each function receives AgentState and returns a partial state update dict.
Do NOT mutate input state — return new values only.

LLM REQUIREMENT:
- classify_node MUST use a real LLM call (structured output for intent classification)
- answer_node MUST use a real LLM call (grounded response generation)
- evaluate_node SHOULD use LLM-as-judge (bonus points; heuristic acceptable for base score)
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field

from .llm import get_llm
from .state import AgentState, make_event


# ─── EXAMPLE: working node (provided for reference) ──────────────────
def intake_node(state: AgentState) -> dict:
    """Normalize raw query. This node is provided as a working example."""
    query = state.get("query", "").strip()
    return {
        "query": query,
        "messages": [f"intake:{query[:40]}"],
        "events": [make_event("intake", "completed", "query normalized")],
    }


# ─── LLM-based Intent Classification Schema ──────────────────────────
class IntentClassification(BaseModel):
    """Structured output for intent classification."""

    route: Literal["simple", "tool", "missing_info", "risky", "error"] = Field(
        description="The classified route for the user query."
    )
    confidence: float = Field(
        default=1.0,
        description="Confidence score between 0.0 and 1.0."
    )
    reasoning: str = Field(
        description="Brief rationale for the chosen route."
    )


def classify_node(state: AgentState) -> dict:
    """Classify the query into a route using an LLM.

    *** MUST use a real LLM call — keyword-only heuristics will lose points. ***
    """
    query = state.get("query", "").strip()
    llm = get_llm(temperature=0.0)
    structured_llm = llm.with_structured_output(IntentClassification)

    prompt = (
        "You are an intent classifier for an agentic support workflow.\n"
        "Classify the user query into EXACTLY ONE of the 5 categories:\n\n"
        "1. 'risky': Actions with destructive side effects, payments, or requiring approval.\n"
        "   Examples: refunds, charging cards, deleting user accounts, sending external emails.\n"
        "2. 'tool': Information retrieval or lookup queries that require external tools.\n"
        "   Examples: order status lookup, tracking shipment, searching database records.\n"
        "3. 'missing_info': Queries that are extremely vague or lack actionable context.\n"
        "   Examples: 'Can you fix it?', 'Help me', 'Please solve this', 'Check issue'.\n"
        "4. 'error': System-level failures, crashes, timeout errors, or unrecoverable issues.\n"
        "   Examples: 'Timeout failure while processing', 'System failure cannot recover'.\n"
        "5. 'simple': General questions, FAQs, greetings, or guidance answered directly.\n"
        "   Examples: 'How do I reset my password?', 'What are your working hours?'.\n\n"
        "PRIORITY RULE: risky > tool > missing_info > error > simple.\n\n"
        f"User Query: {query}"
    )

    try:
        result: IntentClassification = structured_llm.invoke(prompt)  # type: ignore[assignment]
        route = result.route
        reasoning = result.reasoning
    except Exception as exc:
        route = "simple"
        reasoning = f"Classification fallback due to error: {exc}"

    risk_level = "high" if route == "risky" else "low"

    return {
        "route": route,
        "risk_level": risk_level,
        "events": [
            make_event(
                "classify",
                "completed",
                f"intent classified as {route}",
                route=route,
                risk_level=risk_level,
                reasoning=reasoning,
            )
        ],
    }


def tool_node(state: AgentState) -> dict:
    """Execute a mock tool call.

    Simulate transient failures for error-route scenarios to test retry loops.
    """
    query = state.get("query", "")
    route = state.get("route", "")
    attempt = state.get("attempt", 0)

    if route == "error" and attempt < 2:
        result_string = (
            f"ERROR: Transient failure executing tool for query: '{query}' (attempt={attempt})"
        )
    else:
        result_string = f"SUCCESS: Tool executed successfully for query: '{query}'."

    return {
        "tool_results": [result_string],
        "events": [make_event("tool", "completed", "tool executed", result=result_string)],
    }


def evaluate_node(state: AgentState) -> dict:
    """Evaluate tool results — the retry-loop gate."""
    tool_results = state.get("tool_results", [])
    latest_result = tool_results[-1] if tool_results else ""

    if "ERROR" in latest_result.upper():
        evaluation_result = "needs_retry"
    else:
        evaluation_result = "success"

    return {
        "evaluation_result": evaluation_result,
        "events": [
            make_event(
                "evaluate",
                "completed",
                f"evaluated result as {evaluation_result}",
                evaluation_result=evaluation_result,
            )
        ],
    }


def answer_node(state: AgentState) -> dict:
    """Generate a final response using an LLM.

    *** MUST use a real LLM call — hardcoded strings will lose points. ***
    """
    query = state.get("query", "")
    tool_results = state.get("tool_results", [])
    approval = state.get("approval")
    proposed_action = state.get("proposed_action")

    context_parts = [f"User Query: {query}"]
    if tool_results:
        context_parts.append("Tool Execution Results:\n" + "\n".join(tool_results))
    if approval:
        context_parts.append(f"Approval Status: {approval}")
    if proposed_action:
        context_parts.append(f"Action Taken: {proposed_action}")

    context_str = "\n\n".join(context_parts)
    prompt = (
        "You are a professional, helpful customer support agent.\n"
        "Provide a clear, grounded answer to the customer based on the context:\n\n"
        f"{context_str}\n\n"
        "Helpful Answer:"
    )

    try:
        llm = get_llm(temperature=0.2)
        response = llm.invoke(prompt)
        answer_text = response.content if hasattr(response, "content") else str(response)
    except Exception:
        answer_text = f"Thank you for reaching out regarding your request: {query}"

    return {
        "final_answer": str(answer_text),
        "events": [make_event("answer", "completed", "final answer generated")],
    }


def ask_clarification_node(state: AgentState) -> dict:
    """Ask for missing information instead of hallucinating."""
    query = state.get("query", "")
    question = f"Could you please provide more details and specific context about: '{query}'?"
    answer = f"I need additional information to assist you: {question}"

    return {
        "pending_question": question,
        "final_answer": answer,
        "events": [
            make_event(
                "clarify",
                "completed",
                "requested clarification",
                pending_question=question,
            )
        ],
    }


def risky_action_node(state: AgentState) -> dict:
    """Prepare a risky action for human approval."""
    query = state.get("query", "")
    proposed_action = f"Proposed action requiring authorization: {query}"

    return {
        "proposed_action": proposed_action,
        "events": [
            make_event(
                "risky_action",
                "completed",
                "prepared action for approval",
                proposed_action=proposed_action,
            )
        ],
    }


def approval_node(state: AgentState) -> dict:
    """Human-in-the-loop approval step.

    Default behavior: mock approval (approved=True) so tests and CI run offline.
    Extension: if env LANGGRAPH_INTERRUPT=true, use langgraph.types.interrupt() for real HITL.
    """
    is_hitl = os.getenv("LANGGRAPH_INTERRUPT", "false").lower() == "true"
    if is_hitl:
        try:
            from langgraph.types import interrupt

            decision = interrupt({
                "action": state.get("proposed_action", ""),
                "query": state.get("query", ""),
            })
            if isinstance(decision, dict):
                approved = decision.get("approved", True)
                reviewer = decision.get("reviewer", "human-reviewer")
                comment = decision.get("comment", "Reviewed by human")
            else:
                approved = bool(decision)
                reviewer = "human-reviewer"
                comment = "Reviewed by human"
        except Exception:
            approved = True
            reviewer = "mock-reviewer"
            comment = "Auto-approved fallback"
    else:
        approved = True
        reviewer = "mock-reviewer"
        comment = "Auto-approved"

    approval_payload = {
        "approved": approved,
        "reviewer": reviewer,
        "comment": comment,
    }

    return {
        "approval": approval_payload,
        "events": [
            make_event(
                "approval",
                "completed",
                "approval processed",
                approval=approval_payload,
            )
        ],
    }


def retry_or_fallback_node(state: AgentState) -> dict:
    """Record a retry attempt."""
    attempt = state.get("attempt", 0) + 1

    return {
        "attempt": attempt,
        "errors": [f"Retry attempt {attempt} due to transient failure"],
        "events": [make_event("retry", "completed", f"retry attempt {attempt}", attempt=attempt)],
    }


def dead_letter_node(state: AgentState) -> dict:
    """Handle unresolvable failures after max retries exceeded."""
    query = state.get("query", "")
    answer = (
        f"Unable to process request for '{query}' after reaching the maximum retry limit. "
        "The ticket has been routed to the dead letter queue for manual engineering intervention."
    )

    return {
        "final_answer": answer,
        "events": [
            make_event(
                "dead_letter",
                "completed",
                "routed to dead letter queue",
                final_answer=answer,
            )
        ],
    }


def finalize_node(state: AgentState) -> dict:
    """Emit a final audit event. All routes must pass through here before END."""
    return {
        "events": [make_event("finalize", "completed", "workflow finished")],
    }
