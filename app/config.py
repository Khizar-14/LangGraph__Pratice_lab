"""Central config: model factory + settings. Swap providers with env vars only."""
import os

from langchain.chat_models import init_chat_model

# "provider:model" strings - change provider without touching agent code.
SMART_MODEL = os.getenv("SMART_MODEL", "anthropic:claude-sonnet-4-5")
FAST_MODEL = os.getenv("FAST_MODEL", "anthropic:claude-haiku-4-5")

MAX_REVIEW_LOOPS = int(os.getenv("MAX_REVIEW_LOOPS", "3"))
REQUIRE_APPROVAL = os.getenv("REQUIRE_APPROVAL", "false").lower() == "true"
DATABASE_URL = os.getenv("DATABASE_URL", "")          # postgres://... for prod
GATEWAY_API_KEYS = set(filter(None, os.getenv("GATEWAY_API_KEYS", "dev-key").split(",")))
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "30"))


def get_llm(tier: str = "smart"):
    """tier='fast' for routing/guardrails (cheap), 'smart' for reasoning/coding."""
    name = FAST_MODEL if tier == "fast" else SMART_MODEL
    return init_chat_model(name, temperature=0, max_retries=3, timeout=60)
