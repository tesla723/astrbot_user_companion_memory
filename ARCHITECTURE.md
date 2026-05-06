# Evolution Engine Architecture

## Runtime Flow

1. `main.py` receives AstrBot LLM request/response events.
2. `TriggerManager` applies active-hour and cooldown gates.
3. `EvolutionPipeline` records a normalized turn:
   - user text
   - assistant text when available
   - session id
4. The pipeline dispatches:
   - knowledge/capability: normalized user + assistant turn for evidence
   - learning: user text only, so assistant wording does not pollute user profile
5. `ContextInjectionPlanner` builds a bounded, low-priority reference block before LLM calls.
6. `ConversationStrategyManager` adds the human-facing strategy:
   - natural companion style
   - memory relevance weight
   - compactness / verbosity tendency
   - stricter Skill proposal behavior when proposals are rejected often

## Design Rules

- Evolution modules should not read raw AstrBot events independently.
- Injection must be small, relevant, and useful for natural conversation.
- The agent should remember durable preferences, projects, corrections, and relationship style.
- The agent should not expose internal memory retrieval unless the user asks.
- Capability creation must stay approval-first.
- Generated data belongs in the plugin data directory; package code should stay clean.
- AstrBot zip packages must start with an explicit top-level folder entry.

## Main Goal

The architecture optimizes for a simple product goal: make the agent feel like a
long-term conversational partner that remembers useful things and gradually
adjusts how it talks, not like a plugin that randomly hoards facts.
