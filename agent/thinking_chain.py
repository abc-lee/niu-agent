"""
Thinking Chain Processor

Unified processing for thinking chain formats from different LLM providers.
"""

import re
from dataclasses import dataclass


@dataclass
class ThinkingChain:
    """Thinking chain result"""

    content: str
    provider: str
    signature: str | None = None


# Thinking patterns for different providers
THINKING_PATTERNS = {
    "deepseek": [
        r" cockscomb(.*?) cockscomb",
        r"<thinking>(.*?)</thinking>",
    ],
    "minimax": [
        r"<FLUX>(.*?)</FLUX>",
        r"FLUX_REASONING_CONTENT:\s*(.*?)(?=FLUX_|$)",
    ],
    "qwen": [
        r"<thought>(.*?)</thought>",
        r" cockscomb(.*?) cockscomb",
    ],
    "generic": [
        r"<thinking>(.*?)</thinking>",
        r" cockscomb(.*?) cockscomb",
        r"\[THINKING\](.*?)\[/THINKING\]",
    ],
}


def extract_thinking(
    text: str, provider: str = "auto", strip: bool = True
) -> tuple[ThinkingChain | None, str]:
    """
    Extract thinking chain content from text.

    Args:
        text: Original text
        provider: Provider name or "auto" for auto-detection
        strip: Whether to strip whitespace

    Returns:
        (ThinkingChain or None, cleaned text)
    """
    thinking = None
    remaining_text = text

    providers_to_try = [provider] if provider != "auto" else list(THINKING_PATTERNS.keys())

    for prov in providers_to_try:
        patterns = THINKING_PATTERNS.get(prov, [])

        for pattern in patterns:
            try:
                match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
                if match:
                    thinking_content = match.group(1)
                    if strip:
                        thinking_content = thinking_content.strip()

                    if thinking_content:
                        thinking = ThinkingChain(content=thinking_content, provider=prov)
                        remaining_text = re.sub(
                            pattern, "", text, flags=re.DOTALL | re.IGNORECASE
                        ).strip()
                        return thinking, remaining_text
            except Exception:
                continue

    return None, text


def extract_thinking_from_content_blocks(
    content_blocks: list[dict],
) -> tuple[ThinkingChain | None, list[dict]]:
    """
    Extract thinking chain from API content blocks.

    Handles Claude/OpenAI native thinking blocks.
    """
    thinking = None
    cleaned_blocks = []

    for block in content_blocks:
        block_type = block.get("type", "")

        # Claude thinking block
        if block_type == "thinking":
            thinking = ThinkingChain(
                content=block.get("thinking", ""),
                provider="anthropic",
                signature=block.get("signature"),
            )
            continue

        # OpenAI reasoning_content
        if block_type == "reasoning_content" or "reasoningContent" in block:
            reasoning = block.get("reasoningContent", block.get("reasoning_content", {}))
            if isinstance(reasoning, dict):
                thinking = ThinkingChain(content=reasoning.get("text", ""), provider="openai")
            elif isinstance(reasoning, str):
                thinking = ThinkingChain(content=reasoning, provider="openai")
            continue

        cleaned_blocks.append(block)

    return thinking, cleaned_blocks


def format_thinking_for_display(thinking: ThinkingChain) -> str:
    """Format thinking chain for display."""
    lines = [f"[Thinking Chain - {thinking.provider.upper()}]", thinking.content]

    if thinking.signature:
        lines.append(f"[Signature: {thinking.signature[:20]}...]")

    return "\n".join(lines)


def format_thinking_for_injection(thinking: ThinkingChain) -> str:
    """Format thinking chain for injection into message history."""
    return f'<thinking provider="{thinking.provider}">\n{thinking.content}\n</thinking>'
