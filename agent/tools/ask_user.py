"""
Ask User - Human-in-the-loop interaction

For asking questions that require user input.
"""

from typing import Dict, Any, List, Optional
from loguru import logger


def ask_user(question: str, candidates: List[str] = None) -> Dict[str, Any]:
    """
    Ask user a question and wait for response

    This returns a special status that tells the agent loop to pause
    and wait for user input.

    Args:
        question: Question to ask
        candidates: Optional list of choices

    Returns:
        {'status': 'INTERRUPT', 'intent': 'HUMAN_INTERVENTION',
         'data': {'question': str, 'candidates': list}}
    """
    logger.info(f"ask_user: {question}")

    return {
        "status": "INTERRUPT",
        "intent": "HUMAN_INTERVENTION",
        "data": {"question": question, "candidates": candidates or []},
    }


def format_options(options: List[str]) -> str:
    """Format a list of options for display"""
    return "\n".join(f"{i + 1}. {opt}" for i, opt in enumerate(options))
