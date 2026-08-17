"""Versioned wire contracts.

Everything that crosses a process boundary — inbound webhooks, outbound events,
agent handoffs, gateway calls, LLM structured output — has a Pydantic model
here, versioned from day one. Nothing else in the codebase parses a foreign
payload by hand.
"""
