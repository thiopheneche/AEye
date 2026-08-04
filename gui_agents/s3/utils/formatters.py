"""This file contains various formatting checks used to reprompt an agent for correctly formatted responses."""

import ast
import inspect
import re

from gui_agents.s3.utils.common_utils import (
    extract_agent_functions,
    parse_code_from_string,
    split_thinking_response,
)

single_action_check = (
    lambda response: len(extract_agent_functions(parse_code_from_string(response))) == 1
)
single_action_error_msg = (
    "Incorrect code: There must be a single agent action in the code response."
)
SINGLE_ACTION_FORMATTER = lambda response: (
    single_action_check(response),
    single_action_error_msg,
)


REQUIRED_PLAN_SECTIONS = (
    "Previous action verification",
    "Screenshot Analysis",
    "Pre-action confirmation",
    "Next Action",
    "Grounded Action",
)


def required_plan_sections_check(response):
    """Require every verification section to be present and non-empty."""
    positions = []
    for section_name in REQUIRED_PLAN_SECTIONS:
        match = re.search(
            rf"\({re.escape(section_name)}\)", response, flags=re.IGNORECASE
        )
        if match is None:
            return False
        positions.append((match.start(), match.end()))
    if positions != sorted(positions):
        return False
    for index, (_, content_start) in enumerate(positions):
        content_end = (
            positions[index + 1][0] if index + 1 < len(positions) else len(response)
        )
        if not response[content_start:content_end].strip():
            return False
    return True


required_plan_sections_error_msg = (
    "Incorrect response: Include non-empty sections in this exact order: "
    "(Previous action verification), (Screenshot Analysis), "
    "(Pre-action confirmation), (Next Action), and (Grounded Action)."
)
REQUIRED_PLAN_SECTIONS_FORMATTER = lambda response: (
    required_plan_sections_check(response),
    required_plan_sections_error_msg,
)


def _attempt_code_creation(agent, code, obs):
    """Validate one literal agent call without executing its expensive action."""
    try:
        expression = ast.parse(code, mode="eval").body
        if not isinstance(expression, ast.Call):
            return None
        if not isinstance(expression.func, ast.Attribute):
            return None
        if not isinstance(expression.func.value, ast.Name):
            return None
        if expression.func.value.id != "agent":
            return None

        method = getattr(agent, expression.func.attr, None)
        if not callable(method) or not hasattr(method, "is_agent_action"):
            return None
        args = [ast.literal_eval(argument) for argument in expression.args]
        kwargs = {
            keyword.arg: ast.literal_eval(keyword.value)
            for keyword in expression.keywords
            if keyword.arg is not None
        }
        inspect.signature(method).bind(*args, **kwargs)
        return True
    except (SyntaxError, ValueError, TypeError, AttributeError):
        return None


code_valid_check = (
    lambda agent, obs, response: _attempt_code_creation(
        agent, parse_code_from_string(response), obs
    )
    is not None
)
code_valid_error_msg = "Incorrect code: The agent action must be a valid function and use valid parameters from the docstring list."
CODE_VALID_FORMATTER = lambda agent, obs, response: (
    code_valid_check(agent, obs, response),
    code_valid_error_msg,
)

thoughts_answer_tag_check = lambda response: split_thinking_response(response)[1] != ""
thoughts_answer_tag_error_msg = "Incorrect response: The response must contain both <thoughts>...</thoughts> and <answer>...</answer> tags."
THOUGHTS_ANSWER_TAG_FORMATTER = lambda response: (
    thoughts_answer_tag_check(response),
    thoughts_answer_tag_error_msg,
)

integer_answer_check = (
    lambda response: split_thinking_response(response)[0].strip().isdigit()
)
integer_answer_error_msg = (
    "Incorrect response: The <answer>...</answer> tag must contain a single integer."
)
INTEGER_ANSWER_FORMATTER = lambda response: (
    integer_answer_check(response),
    integer_answer_error_msg,
)
