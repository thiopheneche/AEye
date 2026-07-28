import unittest
import threading
from itertools import islice
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from gui_agents.s3.gui_app import AgentSWindow, AgentWorker
from gui_agents.s3.utils.window_target import (
    TargetWindowError,
    TargetWindowController,
    WindowInfo,
    map_grounding_coordinates,
    validate_desktop_action,
    validate_target_window_action,
    describe_screen_point,
    open_desktop_application,
    match_desktop_window_description,
)
from gui_agents.s3.agents.grounding import OSWorldACI
from gui_agents.s3.agents.worker import Worker
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

    def test_point_description_falls_back_when_get_ancestor_is_unavailable(self):
        fake_gui = SimpleNamespace(
            WindowFromPoint=lambda point: 30,
            GetParent=lambda hwnd: {30: 20, 20: 10, 10: 0}[hwnd],
            GetWindowText=lambda hwnd: "Target window" if hwnd == 10 else "",
        )
        fake_process = SimpleNamespace(GetWindowThreadProcessId=lambda hwnd: (1, 4321))
        with patch(
            "gui_agents.s3.utils.window_target._require_windows",
            return_value=(SimpleNamespace(), fake_gui, fake_process),
        ):
            result = describe_screen_point(100, 200)

        self.assertEqual(result["child_hwnd"], 30)
        self.assertEqual(result["root_hwnd"], 10)
        self.assertEqual(result["pid"], 4321)
        self.assertEqual(result["title"], "Target window")


class TopmostWindowTests(unittest.TestCase):
    def setUp(self):
        self.controller = TargetWindowController.__new__(TargetWindowController)
        self.controller.hwnd = 123
        self.controller.process_id = 456
        self.controller.current_info = lambda: WindowInfo(
            123, 456, "Target", 0, 0, 800, 600
        )

    def test_reads_existing_topmost_state(self):
        fake_con = SimpleNamespace(GWL_EXSTYLE=-20, WS_EX_TOPMOST=8)
        fake_gui = SimpleNamespace(GetWindowLong=lambda hwnd, index: 8)
        with patch(
            "gui_agents.s3.utils.window_target._require_windows",
            return_value=(fake_con, fake_gui, SimpleNamespace()),
        ):
            self.assertTrue(self.controller.is_always_on_top())

    def test_sets_and_clears_topmost_without_moving_window(self):
        calls = []
        fake_con = SimpleNamespace(
            HWND_TOPMOST=-1,
            HWND_NOTOPMOST=-2,
            SWP_NOMOVE=1,
            SWP_NOSIZE=2,
            SWP_NOACTIVATE=4,
        )
        fake_gui = SimpleNamespace(
            SetWindowPos=lambda *args: calls.append(args) or True
        )
        with patch(
            "gui_agents.s3.utils.window_target._require_windows",
            return_value=(fake_con, fake_gui, SimpleNamespace()),
        ):
            self.controller.set_always_on_top(True)
            self.controller.set_always_on_top(False)

        self.assertEqual(calls[0][1], fake_con.HWND_TOPMOST)
        self.assertEqual(calls[1][1], fake_con.HWND_NOTOPMOST)
        self.assertEqual(calls[0][-1], 7)


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

    def test_windows_switch_action_activates_existing_window(self):
        agent = OSWorldACI.__new__(OSWorldACI)
        agent.platform = "windows"
        code = agent.switch_applications("WeChat")
        self.assertIn("activate_desktop_window('WeChat')", code)
        self.assertNotIn("hotkey('win', 'd'", code)

    def test_windows_open_uses_unicode_safe_desktop_helper(self):
        agent = OSWorldACI.__new__(OSWorldACI)
        agent.platform = "windows"
        code = agent.open("微信")
        self.assertIn("open_desktop_application", code)
        self.assertNotIn("pyautogui.write", code)

    def test_wechat_taskbar_click_uses_window_activation_instead_of_coordinates(self):
        agent = OSWorldACI.__new__(OSWorldACI)
        agent.restricted_to_window = False
        code = agent.click("The green WeChat icon on the Windows taskbar")
        self.assertIn("open_desktop_application", code)
        self.assertIn("微信", code)
        self.assertEqual(agent.last_grounding_info, "桌面应用快捷激活：微信")

    def test_shell_icon_dynamically_matches_arbitrary_open_window_title(self):
        open_window = WindowInfo(
            hwnd=42,
            process_id=99,
            title="notes.txt - Visual Studio Code",
            left=0,
            top=0,
            width=1200,
            height=800,
        )
        with patch(
            "gui_agents.s3.utils.window_target.list_target_windows",
            return_value=[open_window],
        ):
            matched = match_desktop_window_description(
                "The Visual Studio Code icon on the Windows taskbar"
            )

        self.assertEqual(matched, open_window)

    def test_open_desktop_application_prefers_existing_window(self):
        expected = SimpleNamespace(hwnd=123)
        with patch(
            "gui_agents.s3.utils.window_target.find_desktop_window",
            return_value=expected,
        ), patch(
            "gui_agents.s3.utils.window_target.activate_desktop_window",
            return_value=expected,
        ) as activate:
            result = open_desktop_application("微信")

        self.assertIs(result, expected)
        activate.assert_called_once_with("微信")

    def test_open_desktop_application_pastes_unicode_when_not_running(self):
        actions = []
        clipboard = {"value": "original"}
        fake_pyautogui = SimpleNamespace(
            hotkey=lambda *keys: actions.append(("hotkey", keys)),
            press=lambda key: actions.append(("press", key)),
        )
        fake_pyperclip = SimpleNamespace(
            paste=lambda: clipboard["value"],
            copy=lambda value: (
                clipboard.update(value=value),
                actions.append(("copy", value)),
            ),
        )
        with patch(
            "gui_agents.s3.utils.window_target.find_desktop_window",
            side_effect=TargetWindowError("not running"),
        ), patch(
            "gui_agents.s3.utils.window_target.time.sleep", return_value=None
        ), patch.dict(
            "sys.modules",
            {"pyautogui": fake_pyautogui, "pyperclip": fake_pyperclip},
        ):
            result = open_desktop_application("微信")

        self.assertIsNone(result)
        self.assertIn(("copy", "微信"), actions)
        self.assertIn(("hotkey", ("ctrl", "v")), actions)
        self.assertEqual(clipboard["value"], "original")


class CaptureDimensionTests(unittest.TestCase):
    def setUp(self):
        self.info = WindowInfo(
            hwnd=0,
            process_id=0,
            title="test",
            left=0,
            top=0,
            width=2560,
            height=1440,
        )

    def test_full_desktop_main_model_uses_compact_planning_image(self):
        dimensions = AgentWorker._main_model_image_dimensions(
            self.info,
            {
                "control_mode": "full_desktop",
                "desktop_main_max_dimension": 1600,
            },
        )
        self.assertEqual(dimensions, (1600, 900))

    def test_full_desktop_grounding_keeps_native_pixel_dimensions(self):
        dimensions = AgentWorker._grounding_image_dimensions(
            self.info,
            {
                "control_mode": "full_desktop",
                "desktop_main_max_dimension": 1600,
            },
        )
        self.assertEqual(dimensions, (2560, 1440))

    def test_locked_window_uses_same_scaled_image_for_both_models(self):
        config = {"control_mode": "locked_window", "max_image_dimension": 2400}
        self.assertEqual(
            AgentWorker._main_model_image_dimensions(self.info, config),
            (2400, 1350),
        )
        self.assertEqual(
            AgentWorker._grounding_image_dimensions(self.info, config),
            (2400, 1350),
        )

    def test_desktop_window_inventory_contains_all_unique_open_windows(self):
        windows = [
            WindowInfo(1, 10, "微信", 0, 0, 800, 600, False),
            WindowInfo(2, 20, "Notes - Visual Studio Code", 0, 0, 800, 600, True),
        ]
        prompt = AgentWorker._desktop_window_inventory_prompt(windows)
        self.assertIn("title='微信'; pid=10; state=visible", prompt)
        self.assertIn(
            "title='Notes - Visual Studio Code'; pid=20; state=minimized", prompt
        )
        self.assertIn("switch_applications", prompt)


class GroundingScreenshotTests(unittest.TestCase):
    def test_prefers_coordinate_accurate_grounding_screenshot(self):
        obs = {"screenshot": b"main", "grounding_screenshot": b"native"}
        self.assertEqual(OSWorldACI._grounding_screenshot(obs), b"native")

    def test_falls_back_to_main_screenshot(self):
        self.assertEqual(
            OSWorldACI._grounding_screenshot({"screenshot": b"main"}), b"main"
        )

    def test_extracts_unquoted_chinese_and_known_application_labels(self):
        candidates = OSWorldACI._local_text_candidates(
            "The top contact result 范宇欣 below the WeChat search box"
        )
        self.assertIn("范宇欣", candidates)
        self.assertNotIn("WeChat", candidates)

    def test_extracts_application_label_for_shell_icon(self):
        candidates = OSWorldACI._local_text_candidates(
            "The green WeChat icon on the Windows taskbar"
        )
        self.assertIn("WeChat", candidates)


class FastCoordinateActionTests(unittest.TestCase):
    def setUp(self):
        self.agent = OSWorldACI.__new__(OSWorldACI)
        self.agent.width = 800
        self.agent.height = 600
        self.agent.coordinate_offset_x = 100
        self.agent.coordinate_offset_y = 50

    def test_click_at_maps_normalized_coordinates_without_grounding(self):
        command = self.agent.click_at(500, 500)
        self.assertIn("pyautogui.click(500, 350", command)

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

    def test_transport_failure_skips_format_retry(self):
        class FakeAgent:
            def __init__(self):
                self.calls = 0
                self.messages = []
                self.engine = SimpleNamespace(model="test-model")

            def get_response(self, **kwargs):
                self.calls += 1
                raise TimeoutError("upstream timed out")

        agent = FakeAgent()
        with patch("gui_agents.s3.utils.common_utils.time.sleep", return_value=None):
            response = call_llm_formatted(
                agent,
                [lambda value: (bool(value), "response required")],
                format_max_retries=3,
                call_max_retries=1,
            )

        self.assertEqual(response, "")
        self.assertEqual(agent.calls, 1)


class ImageHistoryTests(unittest.TestCase):
    def test_removes_old_images_but_keeps_text_history(self):
        agent = SimpleNamespace(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "previous observation"},
                        {"type": "image_url", "image_url": {"url": "old"}},
                    ],
                },
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "previous action"}],
                },
            ]
        )

        Worker._remove_historical_images(agent)

        self.assertEqual(
            agent.messages[0]["content"],
            [{"type": "text", "text": "previous observation"}],
        )
        self.assertEqual(
            agent.messages[1]["content"],
            [{"type": "text", "text": "previous action"}],
        )

    def test_flush_keeps_only_configured_recent_text_turns(self):
        worker = Worker.__new__(Worker)
        worker.engine_params = {"engine_type": "openai"}
        worker.max_trajectory_length = 2
        worker.generator_agent = SimpleNamespace(
            messages=[
                {"role": "system", "content": [{"type": "text", "text": "system"}]},
                *[
                    {
                        "role": "user" if index % 2 == 0 else "assistant",
                        "content": [{"type": "text", "text": str(index)}],
                    }
                    for index in range(8)
                ],
            ]
        )
        worker.reflection_agent = None

        worker.flush_messages()

        self.assertEqual(len(worker.generator_agent.messages), 5)
        self.assertEqual(
            [
                message["content"][0]["text"]
                for message in worker.generator_agent.messages
            ],
            ["system", "4", "5", "6", "7"],
        )


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


class ImmediateStopTests(unittest.TestCase):
    def test_force_stop_terminates_running_worker_with_short_wait(self):
        calls = []
        worker = SimpleNamespace(
            request_stop=lambda: calls.append("request_stop"),
            isRunning=lambda: True,
            terminate=lambda: calls.append("terminate"),
            wait=lambda timeout: calls.append(("wait", timeout)) or True,
        )

        stopped = AgentSWindow._terminate_worker_immediately(worker)

        self.assertTrue(stopped)
        self.assertEqual(calls, ["request_stop", "terminate", ("wait", 500)])


if __name__ == "__main__":
    unittest.main()
