import unittest
import threading
from itertools import islice
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from gui_agents.s3.gui_app import AgentWorker
from gui_agents.s3.utils.window_target import (
    TargetWindowError,
    map_grounding_coordinates,
    validate_desktop_action,
    validate_target_window_action,
)
from gui_agents.s3.agents.grounding import OSWorldACI
from gui_agents.s3.agents.worker import Worker
from gui_agents.s3.prompts.poker import POKER_GTO_SYSTEM_PROMPT
from gui_agents.s3.memory.procedural_memory import PROCEDURAL_MEMORY
from gui_agents.s3.utils.common_utils import call_llm_formatted, call_llm_safe
from gui_agents.s3.utils.formatters import CODE_VALID_FORMATTER


class CoordinateSpaceTests(unittest.TestCase):
    def test_window_coordinates_include_client_origin(self):
        result = map_grounding_coordinates(
            [500, 500], 800, 600, 1000, 1000, offset_x=120, offset_y=80
        )
        self.assertEqual(result, [520, 380])

    def test_coordinate_space_can_follow_a_moved_window(self):
        first = map_grounding_coordinates(
            [250, 250], 800, 600, 1000, 1000, offset_x=120, offset_y=80
        )
        second = map_grounding_coordinates(
            [250, 250], 800, 600, 1000, 1000, offset_x=300, offset_y=200
        )
        self.assertEqual(first, [320, 230])
        self.assertEqual(second, [500, 350])

    def test_virtual_desktop_coordinates_support_negative_monitor_origin(self):
        result = map_grounding_coordinates(
            [960, 540], 3840, 1080, 1920, 1080, offset_x=-1920, offset_y=0
        )
        self.assertEqual(result, [0, 540])

    def test_invalid_coordinate_space_is_rejected(self):
        with self.assertRaises(ValueError):
            map_grounding_coordinates([500, 500], 0, 600, 1000, 1000)

    def test_dynamic_grounding_image_size_preserves_screenshot_pixels(self):
        agent = OSWorldACI.__new__(OSWorldACI)
        agent.width = 1612
        agent.height = 1357
        agent.coordinate_offset_x = 628
        agent.coordinate_offset_y = 0
        agent.engine_params_for_grounding = {
            "grounding_width": 1920,
            "grounding_height": 1080,
        }
        agent.set_grounding_image_size(1612, 1357)
        self.assertEqual(agent.resize_coordinates([528, 1269]), [1156, 1269])


class TargetWindowActionTests(unittest.TestCase):
    def test_click_is_allowed(self):
        validate_target_window_action('agent.click("The save button", 1, "left")')

    def test_open_action_is_blocked(self):
        with self.assertRaises(TargetWindowError):
            validate_target_window_action('agent.open("Calculator")')

    def test_application_switch_shortcut_is_blocked(self):
        with self.assertRaises(TargetWindowError):
            validate_target_window_action("agent.hotkey(['alt', 'tab'])")

    def test_hold_and_press_is_allowed_inside_target_window(self):
        validate_target_window_action("agent.hold_and_press([], ['up', 'up', 'enter'])")


class FullDesktopActionTests(unittest.TestCase):
    def test_alt_tab_is_allowed_in_full_desktop_mode(self):
        validate_desktop_action("agent.hotkey(['alt', 'tab'])")

    def test_switch_applications_is_allowed_in_full_desktop_mode(self):
        validate_desktop_action("agent.switch_applications('Notepad')")

    def test_mouse_action_respects_keyboard_only_mode(self):
        with self.assertRaises(TargetWindowError):
            validate_desktop_action("agent.click_at(500, 500)", keyboard_only=True)

    def test_fast_prompt_describes_full_desktop_switching(self):
        prompt = PROCEDURAL_MEMORY.construct_fast_worker_procedural_memory(
            OSWorldACI, skipped_actions=[]
        )
        self.assertIn("full desktop", prompt)
        self.assertIn("Alt+Tab", prompt)

    def test_fast_prompt_keeps_locked_window_boundary(self):
        prompt = PROCEDURAL_MEMORY.construct_fast_worker_procedural_memory(
            OSWorldACI, skipped_actions=["switch_applications"]
        )
        self.assertIn("Never switch applications", prompt)


class FastCoordinateActionTests(unittest.TestCase):
    def setUp(self):
        self.agent = OSWorldACI.__new__(OSWorldACI)
        self.agent.width = 800
        self.agent.height = 600
        self.agent.coordinate_offset_x = 100
        self.agent.coordinate_offset_y = 50
        self.agent.background_input = False

    def test_click_at_maps_normalized_coordinates_without_grounding(self):
        command = self.agent.click_at(500, 500)
        self.assertIn("pyautogui.click(500, 350", command)

    def test_background_click_at_uses_window_relative_coordinates(self):
        self.agent.coordinate_offset_x = 0
        self.agent.coordinate_offset_y = 0
        self.agent.background_input = True
        command = self.agent.click_at(250, 750)
        self.assertEqual(command, "background.click(200, 450, clicks=1, button='left')")

    def test_normalized_coordinates_reject_out_of_range_values(self):
        with self.assertRaises(ValueError):
            self.agent.click_at(1001, 500)


class FormatterSideEffectTests(unittest.TestCase):
    def test_action_validation_does_not_execute_grounding(self):
        class DummyAgent:
            def __init__(self):
                self.calls = 0

            def click(self, description, clicks=1):
                self.calls += 1
                return "executed"

        DummyAgent.click.is_agent_action = True
        agent = DummyAgent()
        valid, _ = CODE_VALID_FORMATTER(
            agent, {}, '```python\nagent.click("target", 1)\n```'
        )
        self.assertTrue(valid)
        self.assertEqual(agent.calls, 0)

    def test_mouse_action_is_blocked_in_keyboard_only_mode(self):
        with self.assertRaises(TargetWindowError):
            validate_target_window_action(
                "agent.click_at(500, 500)", keyboard_only=True
            )

    def test_keyboard_press_is_allowed_in_keyboard_only_mode(self):
        validate_target_window_action(
            "agent.press(['tab', 'enter'])", keyboard_only=True
        )


class BehaviorMetadataTests(unittest.TestCase):
    def test_fast_plan_metadata_is_extracted_for_logs(self):
        worker = Worker.__new__(Worker)
        worker.fast_mode = True
        plan = (
            "OBSERVATION: A button is visible\n"
            "ACTION_GOAL: Open the panel\n"
            "ACTION_REASON: The panel is currently closed\n"
            "```python\nagent.click_at(500, 500)\n```"
        )
        self.assertEqual(
            worker._behavior_metadata(plan),
            (
                "A button is visible",
                "Open the panel",
                "The panel is currently closed",
            ),
        )

    def test_private_card_state_is_parsed_for_persistent_memory(self):
        plan = (
            "HAND_STATUS: SAME_HAND\n"
            "PRIVATE_CARDS: T♦ 5♠\n"
            "```python\nagent.press(['enter'])\n```"
        )
        self.assertEqual(Worker._private_state_metadata(plan), ("SAME_HAND", "T♦ 5♠"))


class ModelResponseFallbackTests(unittest.TestCase):
    def test_blank_api_content_is_retried(self):
        class FakeAgent:
            def __init__(self):
                self.responses = iter(["", "formatted response"])

            def get_response(self, **kwargs):
                return next(self.responses)

        agent = FakeAgent()
        with patch("gui_agents.s3.utils.common_utils.time.sleep", return_value=None):
            response = call_llm_safe(agent)

        self.assertEqual(response, "formatted response")
        self.assertEqual(agent.last_call_diagnostics["attempts"], 2)
        self.assertTrue(agent.last_call_diagnostics["succeeded"])
        self.assertIn("no text content", agent.last_call_diagnostics["errors"][0])

    def test_formatted_call_uses_feedback_after_first_empty_response(self):
        class FakeAgent:
            def __init__(self):
                self.responses = iter(["", "valid"])
                self.messages = []
                self.calls = 0
                self.engine = SimpleNamespace(model="test-model")

            def get_response(self, **kwargs):
                self.calls += 1
                return next(self.responses)

        checker = lambda response: (
            (response == "valid"),
            "response must equal valid",
        )
        agent = FakeAgent()
        with patch("gui_agents.s3.utils.common_utils.time.sleep", return_value=None):
            response = call_llm_formatted(agent, [checker], format_max_retries=2)

        self.assertEqual(response, "valid")
        self.assertEqual(agent.calls, 2)
        history = agent.last_format_diagnostics["history"]
        self.assertEqual(history[0]["call"]["attempts"], 1)
        self.assertFalse(history[0]["valid"])
        self.assertTrue(history[1]["valid"])

    def test_empty_plan_becomes_valid_safe_wait(self):
        class DummyGrounding:
            def assign_screenshot(self, obs):
                self.obs = obs

            def wait(self, seconds):
                return f"import time; time.sleep({seconds})"

        plan_code, exec_code, reason = Worker._execution_with_safe_fallback(
            DummyGrounding(), "", {"screenshot": b"test"}
        )

        self.assertEqual(plan_code, "agent.wait(1.333)")
        self.assertEqual(exec_code, "import time; time.sleep(1.333)")
        self.assertIn("did not contain an action code block", reason)
        validate_desktop_action(plan_code)


class VisualSettleTests(unittest.TestCase):
    def test_waits_for_changed_image_to_become_stable(self):
        before = Image.new("RGB", (20, 20), "black")
        changed = Image.new("RGB", (20, 20), "white")

        class FakeTarget:
            def __init__(self):
                self.images = [changed, changed, changed]

            def capture(self):
                image = self.images.pop(0) if self.images else changed
                return image, None

        worker = AgentWorker.__new__(AgentWorker)
        worker._stop_event = threading.Event()
        worker.config = {
            "settle_timeout": 0.1,
            "settle_poll_interval": 0.001,
            "settle_meaningful_change": 0.05,
            "settle_stable_threshold": 0.0,
            "settle_no_change_grace": 0.01,
            "settle_stable_frames": 2,
        }
        _, _, maximum_change, settled = worker._wait_for_visual_settle(
            FakeTarget(), before
        )
        self.assertTrue(settled)
        self.assertGreater(maximum_change, 99)


class InfiniteRunTests(unittest.TestCase):
    def test_finite_step_iterator_stops_at_configured_limit(self):
        self.assertEqual(
            list(AgentWorker._step_numbers({"infinite_run": False, "max_steps": 3})),
            [0, 1, 2],
        )

    def test_infinite_step_iterator_keeps_counting(self):
        numbers = AgentWorker._step_numbers({"infinite_run": True, "max_steps": 1})
        self.assertEqual(list(islice(numbers, 4)), [0, 1, 2, 3])


class PokerPromptPresetTests(unittest.TestCase):
    def test_prompt_enforces_gto_and_play_money_scope(self):
        self.assertIn("equilibrium-oriented", POKER_GTO_SYSTEM_PROMPT)
        self.assertIn("play-money", POKER_GTO_SYSTEM_PROMPT)
        self.assertIn("real-money", POKER_GTO_SYSTEM_PROMPT)
        self.assertIn("never claim that it is", POKER_GTO_SYSTEM_PROMPT)
        self.assertIn("an exact solver output", POKER_GTO_SYSTEM_PROMPT)

    def test_worker_appends_poker_prompt_to_system_prompt(self):
        captured_prompts = []

        class DummyGrounding:
            env = None
            restricted_to_window = True

        class DummyAgent:
            def __init__(self, system_prompt):
                self.system_prompt = system_prompt

        def capture_agent(_worker, system_prompt=None, engine_params=None):
            captured_prompts.append(system_prompt or "")
            return DummyAgent(system_prompt or "")

        with patch.object(Worker, "_create_agent", capture_agent):
            Worker(
                worker_engine_params={"model": "test"},
                grounding_agent=DummyGrounding(),
                platform="windows",
                enable_reflection=False,
                fast_mode=True,
                system_prompt_addendum=POKER_GTO_SYSTEM_PROMPT,
            )

        self.assertTrue(captured_prompts)
        self.assertIn(POKER_GTO_SYSTEM_PROMPT, captured_prompts[0])


if __name__ == "__main__":
    unittest.main()
