from functools import partial
import logging
import re
import textwrap
from typing import Dict, List, Tuple

from gui_agents.s3.agents.grounding import ACI
from gui_agents.s3.core.module import BaseModule
from gui_agents.s3.memory.procedural_memory import PROCEDURAL_MEMORY
from gui_agents.s3.utils.common_utils import (
    call_llm_safe,
    call_llm_formatted,
    parse_code_from_string,
    split_thinking_response,
    create_pyautogui_code,
)
from gui_agents.s3.utils.formatters import (
    SINGLE_ACTION_FORMATTER,
    CODE_VALID_FORMATTER,
    REQUIRED_PLAN_SECTIONS_FORMATTER,
)

logger = logging.getLogger("desktopenv.agent")


class Worker(BaseModule):
    def __init__(
        self,
        worker_engine_params: Dict,
        grounding_agent: ACI,
        platform: str = "ubuntu",
        max_trajectory_length: int = 8,
        enable_reflection: bool = True,
        system_prompt_addendum: str = "",
    ):
        """
        Worker receives the main task and generates actions, without the need of hierarchical planning
        Args:
            worker_engine_params: Dict
                Parameters for the worker agent
            grounding_agent: Agent
                The grounding agent to use
            platform: str
                OS platform the agent runs on (darwin, linux, windows)
            max_trajectory_length: int
                The amount of images turns to keep
            enable_reflection: bool
                Whether to enable reflection
        """
        super().__init__(worker_engine_params, platform)

        self.temperature = worker_engine_params.get("temperature", 0.0)
        self.use_thinking = worker_engine_params.get("model", "") in [
            "claude-opus-4-20250514",
            "claude-sonnet-4-20250514",
            "claude-3-7-sonnet-20250219",
            "claude-sonnet-4-5-20250929",
            "claude-opus-4-5-20251101",
        ]
        self.grounding_agent = grounding_agent
        self.max_trajectory_length = max_trajectory_length
        self.enable_reflection = enable_reflection
        self.system_prompt_addendum = system_prompt_addendum.strip()

        self.reset()

    def reset(self):
        if self.platform != "linux":
            skipped_actions = ["set_cell_values"]
        else:
            skipped_actions = []

        # Hide code agent action entirely if no env/controller is available
        if not getattr(self.grounding_agent, "env", None) or not getattr(
            getattr(self.grounding_agent, "env", None), "controller", None
        ):
            skipped_actions.append("call_code_agent")

        # A target-window session must not let the planner deliberately leave
        # the user-authorized application boundary.
        if getattr(self.grounding_agent, "restricted_to_window", False):
            skipped_actions.extend(["switch_applications", "open", "call_code_agent"])

        sys_prompt = PROCEDURAL_MEMORY.construct_simple_worker_procedural_memory(
            type(self.grounding_agent), skipped_actions=skipped_actions
        ).replace("CURRENT_OS", self.platform)
        if not getattr(self.grounding_agent, "restricted_to_window", False):
            sys_prompt += (
                "\n\nFULL-DESKTOP MODE: The screenshot covers the entire virtual desktop, "
                "not a cropped application window. Use Alt+Tab for the next recent "
                "window or switch_applications(name) to activate an already-open "
                "named window. switch_applications must not be used to launch a second "
                "instance. Never visually click a taskbar icon for a named application; "
                "use switch_applications(name), or open(name) when it is not already "
                "running. Re-observe after every window switch before clicking."
            )
        sys_prompt += (
            "\n\nACTION SETTLING RULE: After any state-changing interaction, treat the "
            "action as pending until system interaction feedback says the interface has "
            "settled. Never repeat reveal/open/start/submit actions because of an "
            "intermediate animation frame. Judge only the latest stable screenshot."
        )
        if self.system_prompt_addendum:
            sys_prompt += "\n\n" + self.system_prompt_addendum

        self.generator_agent = self._create_agent(sys_prompt)
        self.reflection_agent = self._create_agent(
            PROCEDURAL_MEMORY.REFLECTION_ON_TRAJECTORY
        )

        self.turn_count = 0
        self.worker_history = []
        self.reflections = []
        self.cost_this_turn = 0
        self.screenshot_inputs = []

    def flush_messages(self):
        """Flush messages based on the model's context limits.

        This method ensures that the agent's message history does not exceed the maximum trajectory length.

        Side Effects:
            - Modifies the messages of generator, reflection, and bon_judge agents to fit within the context limits.
        """
        engine_type = self.engine_params.get("engine_type", "")

        # Flush strategy for long-context models: keep all text, only keep latest images
        if engine_type in ["anthropic", "openai", "gemini"]:
            max_images = 1
            for agent in [self.generator_agent, self.reflection_agent]:
                if agent is None:
                    continue
                # keep latest k images
                img_count = 0
                for i in range(len(agent.messages) - 1, -1, -1):
                    for j in range(len(agent.messages[i]["content"])):
                        if "image" in agent.messages[i]["content"][j].get("type", ""):
                            img_count += 1
                            if img_count > max_images:
                                del agent.messages[i]["content"][j]
            max_messages = 2 * self.max_trajectory_length
            if len(self.generator_agent.messages) > max_messages + 1:
                self.generator_agent.messages = [
                    self.generator_agent.messages[0],
                    *self.generator_agent.messages[-max_messages:],
                ]

        # Flush strategy for non-long-context models: drop full turns
        else:
            # generator msgs are alternating [user, assistant], so 2 per round
            if len(self.generator_agent.messages) > 2 * self.max_trajectory_length + 1:
                self.generator_agent.messages.pop(1)
                self.generator_agent.messages.pop(1)
            # reflector msgs are all [(user text, user image)], so 1 per round
            if len(self.reflection_agent.messages) > self.max_trajectory_length + 1:
                self.reflection_agent.messages.pop(1)

    @staticmethod
    def _remove_historical_images(agent):
        """Keep text trajectory while ensuring only the current screenshot is sent."""
        for message in agent.messages:
            message["content"] = [
                item
                for item in message.get("content", [])
                if "image" not in item.get("type", "")
            ]

    @staticmethod
    def _execution_with_safe_fallback(grounding_agent, plan_code: str, obs: Dict):
        """Create executable code, converting malformed model output into a valid wait."""
        try:
            if not plan_code or not plan_code.strip():
                raise ValueError(
                    "The model response did not contain an action code block."
                )
            exec_code = create_pyautogui_code(grounding_agent, plan_code, obs)
            return plan_code.strip(), exec_code, ""
        except Exception as exc:
            fallback_plan_code = "agent.wait(1.333)"
            exec_code = create_pyautogui_code(grounding_agent, fallback_plan_code, obs)
            reason = f"{type(exc).__name__}: {exc}"
            logger.error(
                "Could not evaluate model plan code; using safe wait. "
                "plan_code=%r error=%s",
                plan_code,
                reason,
            )
            return fallback_plan_code, exec_code, reason

    def _generate_reflection(self, instruction: str, obs: Dict) -> Tuple[str, str]:
        """
        Generate a reflection based on the current observation and instruction.

        Args:
            instruction (str): The task instruction.
            obs (Dict): The current observation containing the screenshot.

        Returns:
            Optional[str, str]: The generated reflection text and thoughts, if any (turn_count > 0).

        Side Effects:
            - Updates reflection agent's history
            - Generates reflection response with API call
        """
        reflection = None
        reflection_thoughts = None
        if self.enable_reflection:
            # Load the initial message
            if self.turn_count == 0:
                text_content = textwrap.dedent(
                    f"""
                    Task Description: {instruction}
                    Current Trajectory below:
                    """
                )
                updated_sys_prompt = (
                    self.reflection_agent.system_prompt + "\n" + text_content
                )
                self.reflection_agent.add_system_prompt(updated_sys_prompt)
                self.reflection_agent.add_message(
                    text_content="The initial screen is provided. No action has been taken yet.",
                    image_content=obs["screenshot"],
                    role="user",
                )
            # Load the latest action
            else:
                self.reflection_agent.add_message(
                    text_content=self.worker_history[-1],
                    image_content=obs["screenshot"],
                    role="user",
                )
                full_reflection = call_llm_safe(
                    self.reflection_agent,
                    temperature=self.temperature,
                    use_thinking=self.use_thinking,
                )
                reflection, reflection_thoughts = split_thinking_response(
                    full_reflection
                )
                self.reflections.append(reflection)
                logger.info("REFLECTION THOUGHTS: %s", reflection_thoughts)
                logger.info("REFLECTION: %s", reflection)
        return reflection, reflection_thoughts

    def generate_next_action(self, instruction: str, obs: Dict) -> Tuple[Dict, List]:
        """
        Predict the next action(s) based on the current observation.
        """

        self.grounding_agent.assign_screenshot(obs)
        self.grounding_agent.set_task_instruction(instruction)

        generator_message = (
            ""
            if self.turn_count > 0
            else "The initial screen is provided. No action has been taken yet."
        )
        if obs.get("interaction_feedback"):
            generator_message += (
                "\nSYSTEM INTERACTION FEEDBACK: "
                f"{obs['interaction_feedback']}\n"
                "Do not repeat the previous state-changing action merely because an "
                "animation or transition frame resembled the old state. Use the latest "
                "stable screenshot as the source of truth.\n"
            )
        # Load the task into the system prompt
        if self.turn_count == 0:
            prompt_with_instructions = self.generator_agent.system_prompt.replace(
                "TASK_DESCRIPTION", instruction
            )
            self.generator_agent.add_system_prompt(prompt_with_instructions)

        # Get the per-step reflection
        reflection, reflection_thoughts = self._generate_reflection(instruction, obs)
        if reflection:
            generator_message += f"REFLECTION: You may use this reflection on the previous action and overall trajectory:\n{reflection}\n"

        # Get the grounding agent's knowledge base buffer
        generator_message += (
            f"\nCurrent Text Buffer = [{','.join(self.grounding_agent.notes)}]\n"
        )

        # Add code agent result from previous step if available (from full task or subtask execution)
        if (
            hasattr(self.grounding_agent, "last_code_agent_result")
            and self.grounding_agent.last_code_agent_result is not None
        ):
            code_result = self.grounding_agent.last_code_agent_result
            generator_message += f"\nCODE AGENT RESULT:\n"
            generator_message += (
                f"Task/Subtask Instruction: {code_result['task_instruction']}\n"
            )
            generator_message += f"Steps Completed: {code_result['steps_executed']}\n"
            generator_message += f"Max Steps: {code_result['budget']}\n"
            generator_message += (
                f"Completion Reason: {code_result['completion_reason']}\n"
            )
            generator_message += f"Summary: {code_result['summary']}\n"
            if code_result["execution_history"]:
                generator_message += f"Execution History:\n"
                for i, step in enumerate(code_result["execution_history"]):
                    action = step["action"]
                    # Format code snippets with proper backticks
                    if "```python" in action:
                        # Extract Python code and format it
                        code_start = action.find("```python") + 9
                        code_end = action.find("```", code_start)
                        if code_end != -1:
                            python_code = action[code_start:code_end].strip()
                            generator_message += (
                                f"Step {i+1}: \n```python\n{python_code}\n```\n"
                            )
                        else:
                            generator_message += f"Step {i+1}: \n{action}\n"
                    elif "```bash" in action:
                        # Extract Bash code and format it
                        code_start = action.find("```bash") + 7
                        code_end = action.find("```", code_start)
                        if code_end != -1:
                            bash_code = action[code_start:code_end].strip()
                            generator_message += (
                                f"Step {i+1}: \n```bash\n{bash_code}\n```\n"
                            )
                        else:
                            generator_message += f"Step {i+1}: \n{action}\n"
                    else:
                        generator_message += f"Step {i+1}: \n{action}\n"
            generator_message += "\n"

            # Log the code agent result section for debugging (truncated execution history)
            log_message = f"\nCODE AGENT RESULT:\n"
            log_message += (
                f"Task/Subtask Instruction: {code_result['task_instruction']}\n"
            )
            log_message += f"Steps Completed: {code_result['steps_executed']}\n"
            log_message += f"Max Steps: {code_result['budget']}\n"
            log_message += f"Completion Reason: {code_result['completion_reason']}\n"
            log_message += f"Summary: {code_result['summary']}\n"
            if code_result["execution_history"]:
                log_message += f"Execution History (truncated):\n"
                # Only log first 3 steps and last 2 steps to keep logs manageable
                total_steps = len(code_result["execution_history"])
                for i, step in enumerate(code_result["execution_history"]):
                    if i < 3 or i >= total_steps - 2:  # First 3 and last 2 steps
                        action = step["action"]
                        if "```python" in action:
                            code_start = action.find("```python") + 9
                            code_end = action.find("```", code_start)
                            if code_end != -1:
                                python_code = action[code_start:code_end].strip()
                                log_message += (
                                    f"Step {i+1}: ```python\n{python_code}\n```\n"
                                )
                            else:
                                log_message += f"Step {i+1}: {action}\n"
                        elif "```bash" in action:
                            code_start = action.find("```bash") + 7
                            code_end = action.find("```", code_start)
                            if code_end != -1:
                                bash_code = action[code_start:code_end].strip()
                                log_message += (
                                    f"Step {i+1}: ```bash\n{bash_code}\n```\n"
                                )
                            else:
                                log_message += f"Step {i+1}: {action}\n"
                        else:
                            log_message += f"Step {i+1}: {action}\n"
                    elif i == 3 and total_steps > 5:
                        log_message += f"... (truncated {total_steps - 5} steps) ...\n"

            logger.info(
                f"WORKER_CODE_AGENT_RESULT_SECTION - Step {self.turn_count + 1}: Code agent result added to generator message:\n{log_message}"
            )

            # Reset the code agent result after adding it to context
            self.grounding_agent.last_code_agent_result = None

        # Finalize the generator message
        self._remove_historical_images(self.generator_agent)
        self.generator_agent.add_message(
            generator_message,
            image_content=obs["screenshot"],
            image_detail="high",
            role="user",
        )

        # Generate the plan and next action
        self.grounding_agent.last_grounding_info = None
        format_checkers = [
            REQUIRED_PLAN_SECTIONS_FORMATTER,
            SINGLE_ACTION_FORMATTER,
            partial(CODE_VALID_FORMATTER, self.grounding_agent, obs),
        ]
        plan = call_llm_formatted(
            self.generator_agent,
            format_checkers,
            temperature=self.temperature,
            use_thinking=self.use_thinking,
            format_max_retries=3,
            call_max_retries=1,
        )
        self.worker_history.append(plan)
        self.generator_agent.add_message(plan, role="assistant")
        logger.info("PLAN:\n %s", plan)

        # Extract the next action from the plan
        plan_code = parse_code_from_string(plan)
        observation_summary, action_goal, action_reason = self._behavior_metadata(plan)
        (
            plan_code,
            exec_code,
            action_fallback_reason,
        ) = self._execution_with_safe_fallback(self.grounding_agent, plan_code, obs)

        executor_info = {
            "plan": plan,
            "plan_code": plan_code,
            "exec_code": exec_code,
            "observation_summary": observation_summary,
            "action_goal": action_goal,
            "action_reason": action_reason,
            "reflection": reflection,
            "reflection_thoughts": reflection_thoughts,
            "grounding_info": getattr(
                self.grounding_agent, "last_grounding_info", None
            ),
            "format_diagnostics": getattr(
                self.generator_agent, "last_format_diagnostics", {}
            ),
            "action_fallback_reason": action_fallback_reason,
            "code_agent_output": (
                self.grounding_agent.last_code_agent_result
                if hasattr(self.grounding_agent, "last_code_agent_result")
                and self.grounding_agent.last_code_agent_result is not None
                else None
            ),
        }
        self.turn_count += 1
        self.screenshot_inputs.append(obs["screenshot"])
        self.flush_messages()
        return executor_info, [exec_code]

    def _behavior_metadata(self, plan: str):
        def section(name, next_name):
            match = re.search(
                rf"\({re.escape(name)}\)\s*(.*?)(?=\({re.escape(next_name)}\)|$)",
                plan,
                flags=re.IGNORECASE | re.DOTALL,
            )
            return " ".join(match.group(1).split()) if match else "模型未提供"

        return (
            section("Screenshot Analysis", "Pre-action confirmation"),
            section("Next Action", "Grounded Action"),
            section("Pre-action confirmation", "Next Action"),
        )
