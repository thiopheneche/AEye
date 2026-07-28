"""PySide6 desktop interface for the Agent-S target-window prototype."""

import io
import itertools
import os
import re
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageChops, ImageStat
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QCloseEvent, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from gui_agents.s3.utils.window_target import (
    DesktopController,
    TargetWindowController,
    TargetWindowError,
    WindowInfo,
    describe_screen_point,
    list_target_windows,
    validate_desktop_action,
    validate_target_window_action,
)


def get_configured_secret(name: str) -> str:
    """Read a secret without copying it into a project configuration file."""
    value = os.getenv(name)
    if value:
        return value

    if os.name == "nt":
        import winreg

        locations = (
            (winreg.HKEY_CURRENT_USER, r"Environment"),
            (
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
            ),
        )
        for hive, path in locations:
            try:
                with winreg.OpenKey(hive, path) as key:
                    value, _ = winreg.QueryValueEx(key, name)
                    if value:
                        return str(value)
            except OSError:
                continue

    raise RuntimeError(f"Required environment variable '{name}' was not found.")


def scale_dimensions(width: int, height: int, max_dimension: int = 2400):
    scale = min(max_dimension / width, max_dimension / height, 1)
    return max(1, int(width * scale)), max(1, int(height * scale))


class AgentWorker(QThread):
    log_message = Signal(str)
    screenshot_ready = Signal(bytes)
    status_changed = Signal(str)
    completed = Signal(str)
    failed = Signal(str)

    def __init__(self, window_info, task: str, config: dict):
        super().__init__()
        self.window_info = window_info
        self.task = task
        self.config = config
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()

    def request_stop(self):
        self._stop_event.set()
        self._pause_event.clear()

    def set_paused(self, paused: bool):
        if paused:
            self._pause_event.set()
        else:
            self._pause_event.clear()

    def _wait_if_paused(self) -> bool:
        while self._pause_event.is_set() and not self._stop_event.is_set():
            self.status_changed.emit("已暂停")
            time.sleep(0.1)
        return self._stop_event.is_set()

    @staticmethod
    def _visual_change_percent(first: Image.Image, second: Image.Image) -> float:
        if first.size != second.size:
            return 100.0
        difference = ImageChops.difference(first.convert("RGB"), second.convert("RGB"))
        mean_difference = sum(ImageStat.Stat(difference).mean) / 3
        return mean_difference / 255 * 100

    @staticmethod
    def _step_numbers(config):
        if config["infinite_run"]:
            return itertools.count()
        return range(config["max_steps"])

    @staticmethod
    def _desktop_window_inventory_prompt(windows):
        """Build compact startup context for deterministic application switching."""
        entries = []
        seen = set()
        for info in windows:
            title = " ".join(info.title.split())
            key = (title.casefold(), info.process_id)
            if not title or key in seen:
                continue
            seen.add(key)
            state = "minimized" if info.minimized else "visible"
            entries.append(f"- title={title!r}; pid={info.process_id}; state={state}")
        if not entries:
            entries.append("- none")
        return (
            "OPEN WINDOWS AT TASK START:\n"
            + "\n".join(entries)
            + "\nWhen the requested application matches this inventory, call "
            "switch_applications using its exact title or a clear application name. "
            "Do not visually click its taskbar or Start-menu icon. The runtime will "
            "re-check the live window list before activation."
        )

    @staticmethod
    def _main_model_image_dimensions(info: WindowInfo, config: dict):
        """Use a compact planning image, especially for full-desktop tasks."""
        if config["control_mode"] == "full_desktop":
            return scale_dimensions(
                info.width,
                info.height,
                max_dimension=config["desktop_main_max_dimension"],
            )
        return scale_dimensions(
            info.width,
            info.height,
            max_dimension=config["max_image_dimension"],
        )

    @classmethod
    def _grounding_image_dimensions(cls, info: WindowInfo, config: dict):
        """Use native desktop pixels, but retain scaled locked-window grounding."""
        if config["control_mode"] == "full_desktop":
            return info.width, info.height
        return cls._main_model_image_dimensions(info, config)

    def _wait_for_visual_settle(self, target, before_image: Image.Image):
        """Wait locally for an action animation/state transition to finish."""
        started = time.perf_counter()
        previous = before_image.convert("RGB")
        latest = previous
        stable_frames = 0
        maximum_change = 0.0
        saw_meaningful_change = False

        while time.perf_counter() - started < self.config["settle_timeout"]:
            if self._stop_event.is_set():
                break
            time.sleep(self.config["settle_poll_interval"])
            latest, _ = target.capture()
            latest = latest.convert("RGB")
            frame_change = self._visual_change_percent(previous, latest)
            total_change = self._visual_change_percent(before_image, latest)
            maximum_change = max(maximum_change, total_change)
            if total_change >= self.config["settle_meaningful_change"]:
                saw_meaningful_change = True

            if frame_change <= self.config["settle_stable_threshold"]:
                stable_frames += 1
            else:
                stable_frames = 0

            elapsed = time.perf_counter() - started
            no_change_grace_complete = elapsed >= self.config["settle_no_change_grace"]
            if stable_frames >= self.config["settle_stable_frames"] and (
                saw_meaningful_change or no_change_grace_complete
            ):
                return latest, elapsed, maximum_change, True
            previous = latest

        elapsed = time.perf_counter() - started
        return latest, elapsed, maximum_change, False

    def run(self):
        try:
            # Delay heavy imports until the worker starts so the GUI appears quickly.
            from gui_agents.s3.agents.agent_s import AgentS3
            from gui_agents.s3.agents.grounding import OSWorldACI

            main_key = get_configured_secret("fyx_api_key")
            grounding_key = (
                get_configured_secret("OPENROUTER_API_KEY")
                if not self.config["fast_mode"]
                else os.getenv("OPENROUTER_API_KEY", "unused-fast-mode")
            )
            control_mode = self.config["control_mode"]
            locked_window_mode = control_mode == "locked_window"
            background_mode = (
                self.config["background_mode"] if locked_window_mode else False
            )
            if locked_window_mode:
                if self.window_info is None:
                    raise TargetWindowError("锁定单窗口模式缺少目标窗口。")
                target = TargetWindowController.from_hwnd(
                    self.window_info.hwnd, background=background_mode
                )
                initial = target.current_info()
                import win32gui

                window_class = win32gui.GetClassName(initial.hwnd)
                self.log_message.emit(f"目标窗口类：{window_class}")
                if background_mode and window_class.startswith("Chrome_WidgetWin"):
                    raise TargetWindowError(
                        "Edge/Chrome 不可靠地接收后台鼠标消息；本次任务已停止，" "请取消勾选“实验性后台模式”后重试。"
                    )
            else:
                target = DesktopController()
                initial = target.current_info()

            system_prompt_addendum = self.config["system_prompt_addendum"]
            desktop_windows = []
            if not locked_window_mode:
                desktop_windows = list_target_windows(include_minimized=True)
                inventory_prompt = self._desktop_window_inventory_prompt(
                    desktop_windows
                )
                system_prompt_addendum = "\n\n".join(
                    item for item in (system_prompt_addendum, inventory_prompt) if item
                )

            main_engine = {
                "engine_type": "openai",
                "model": self.config["main_model"],
                "base_url": self.config["main_url"],
                "api_key": main_key,
                "temperature": 0.0,
                "timeout": 30.0,
                "max_retries": 0,
            }
            grounding_engine = {
                "engine_type": "open_router",
                "model": self.config["grounding_model"],
                "base_url": self.config["grounding_url"],
                "api_key": grounding_key,
                "grounding_width": self.config["grounding_width"],
                "grounding_height": self.config["grounding_height"],
            }

            grounding_agent = OSWorldACI(
                env=None,
                platform="windows",
                engine_params_for_generation=main_engine,
                engine_params_for_grounding=grounding_engine,
                width=initial.width,
                height=initial.height,
            )
            grounding_agent.restricted_to_window = locked_window_mode
            grounding_agent.set_background_input(background_mode)
            agent = AgentS3(
                main_engine,
                grounding_agent,
                platform="windows",
                max_trajectory_length=self.config["trajectory_length"],
                enable_reflection=self.config["enable_reflection"],
                fast_mode=self.config["fast_mode"],
                keyboard_only=self.config["keyboard_only"],
                system_prompt_addendum=system_prompt_addendum,
            )

            if locked_window_mode:
                self.log_message.emit(
                    f'已绑定窗口："{initial.title}" '
                    f"(PID={initial.process_id}, HWND={initial.hwnd})"
                )
            else:
                self.log_message.emit("控制范围：完整虚拟桌面；允许通过 Alt+Tab、任务栏或应用切换动作" "在多个窗口之间操作")
                inventory_titles = [info.title for info in desktop_windows]
                self.log_message.emit(
                    f"启动窗口清单：共 {len(inventory_titles)} 个；"
                    + " | ".join(inventory_titles)
                )
            self.log_message.emit(
                f'主模型：{self.config["main_model"]}；Grounding：{self.config["grounding_model"]}'
            )
            self.log_message.emit(
                "主模型上下文：仅发送当前规划截图；"
                f"保留最近 {self.config['trajectory_length']} 轮文字；请求超时 30 秒"
            )
            if background_mode:
                self.log_message.emit("操作模式：锁定单窗口 + 后台窗口消息（实验性）")
            elif locked_window_mode:
                self.log_message.emit("操作模式：锁定单窗口 + 前台鼠标键盘")
            else:
                self.log_message.emit("操作模式：全屏多窗口 + 前台鼠标键盘")
            if self.config["fast_mode"]:
                self.log_message.emit("快速模式：单次主模型决策，跳过独立 Grounding 请求")
            if self.config["keyboard_only"]:
                self.log_message.emit("输入限制：仅键盘；鼠标、拖动和滚轮动作已禁用")
            if self.config["infinite_run"]:
                self.log_message.emit("运行限制：永久循环，直到手动停止或发生错误")

            obs = {}
            previous_capture = None
            previous_action_code = None
            repeated_action_count = 0
            total_steps_label = (
                "∞" if self.config["infinite_run"] else str(self.config["max_steps"])
            )
            for step in self._step_numbers(self.config):
                if self._stop_event.is_set() or self._wait_if_paused():
                    self.completed.emit("任务已停止")
                    return

                self.status_changed.emit(f"第 {step + 1}/{total_steps_label} 步：截取画面")
                step_started = time.perf_counter()
                screenshot, current = target.capture()
                self.log_message.emit(f"\n========== 第 {step + 1} 步 ==========")
                comparable_capture = screenshot.convert("RGB")
                if (
                    previous_capture is not None
                    and previous_capture.size == comparable_capture.size
                ):
                    difference = ImageChops.difference(
                        previous_capture, comparable_capture
                    )
                    mean_difference = sum(ImageStat.Stat(difference).mean) / 3
                    change_percent = mean_difference / 255 * 100
                    self.log_message.emit(f"界面稳定后至本轮截图的变化率：{change_percent:.3f}%")
                previous_capture = comparable_capture.copy()
                grounding_agent.set_coordinate_space(
                    current.width,
                    current.height,
                    offset_x=0 if background_mode else current.left,
                    offset_y=0 if background_mode else current.top,
                )

                main_width, main_height = self._main_model_image_dimensions(
                    current, self.config
                )
                grounding_width, grounding_height = self._grounding_image_dimensions(
                    current, self.config
                )
                grounding_agent.set_grounding_image_size(
                    grounding_width, grounding_height
                )
                geometry_name = "窗口" if locked_window_mode else "桌面"
                coordinate_mode = (
                    "window-scaled" if locked_window_mode else "native-full-desktop"
                )
                self.log_message.emit(
                    f"第 {step + 1} 步{geometry_name}几何："
                    f"origin=({current.left}, {current.top})；"
                    f"area={current.width}×{current.height}；"
                    f"main_image={main_width}×{main_height}；"
                    f"grounding_image={grounding_width}×{grounding_height}；"
                    f"coordinate_mode={coordinate_mode}；"
                    f"grounding_scale=({current.width / grounding_width:.6f}, "
                    f"{current.height / grounding_height:.6f})"
                )
                main_screenshot = screenshot
                if main_screenshot.size != (main_width, main_height):
                    main_screenshot = main_screenshot.resize(
                        (main_width, main_height), Image.Resampling.LANCZOS
                    )
                main_buffer = io.BytesIO()
                main_screenshot.save(main_buffer, format="PNG")
                main_screenshot_bytes = main_buffer.getvalue()
                obs["screenshot"] = main_screenshot_bytes

                if self.config["fast_mode"]:
                    obs.pop("grounding_screenshot", None)
                elif screenshot.size == (grounding_width, grounding_height):
                    grounding_buffer = io.BytesIO()
                    screenshot.save(grounding_buffer, format="PNG")
                    obs["grounding_screenshot"] = grounding_buffer.getvalue()
                elif (grounding_width, grounding_height) == (
                    main_width,
                    main_height,
                ):
                    obs["grounding_screenshot"] = main_screenshot_bytes
                else:
                    grounding_screenshot = screenshot.resize(
                        (grounding_width, grounding_height), Image.Resampling.LANCZOS
                    )
                    grounding_buffer = io.BytesIO()
                    grounding_screenshot.save(grounding_buffer, format="PNG")
                    obs["grounding_screenshot"] = grounding_buffer.getvalue()

                self.screenshot_ready.emit(main_screenshot_bytes)

                if self._stop_event.is_set() or self._wait_if_paused():
                    self.completed.emit("任务已停止")
                    return

                self.status_changed.emit(f"第 {step + 1}/{total_steps_label} 步：模型决策中")
                decision_started = time.perf_counter()
                info, actions = agent.predict(instruction=self.task, observation=obs)
                decision_ms = round((time.perf_counter() - decision_started) * 1000)
                if not actions:
                    raise RuntimeError("Agent did not return an action.")

                action_code = actions[0]
                plan_code = info.get("plan_code", "")
                plan = info.get("plan", "")
                self.log_message.emit(
                    f"观察摘要：{info.get('observation_summary', '模型未提供')}"
                )
                self.log_message.emit(f"行为目标：{info.get('action_goal', '模型未提供')}")
                self.log_message.emit(f"行为原因：{info.get('action_reason', '模型未提供')}")
                self.log_message.emit(f"模型原始计划：\n{plan}")
                self.log_message.emit(f"执行代码：{action_code}")
                if info.get("grounding_info"):
                    self.log_message.emit(f"定位来源：{info['grounding_info']}")
                self.log_message.emit(f"模型决策耗时：{decision_ms} ms")
                format_diagnostics = info.get("format_diagnostics") or {}
                call_diagnostics = format_diagnostics.get("call") or {}
                feedback = format_diagnostics.get("feedback") or []
                api_errors = call_diagnostics.get("errors") or []
                engine_response = call_diagnostics.get("engine_response") or {}
                engine_summary = engine_response or {"available": False}
                format_history = format_diagnostics.get("history") or []
                history_summary = []
                for attempt_details in format_history:
                    attempt_call = attempt_details.get("call") or {}
                    attempt_engine = attempt_call.get("engine_response") or {}
                    history_summary.append(
                        {
                            "format_attempt": attempt_details.get("attempt"),
                            "valid": attempt_details.get("valid"),
                            "response_length": attempt_details.get("response_length"),
                            "api_attempts": attempt_call.get("attempts"),
                            "api_success": attempt_call.get("succeeded"),
                            "finish_reason": attempt_engine.get("finish_reason"),
                            "content_type": attempt_engine.get("content_type"),
                            "completion_tokens": attempt_engine.get(
                                "completion_tokens"
                            ),
                            "reasoning_tokens": attempt_engine.get("reasoning_tokens"),
                            "errors": [
                                str(item)[:200]
                                for item in (attempt_call.get("errors") or [])
                            ],
                        }
                    )
                self.log_message.emit(
                    "模型格式诊断："
                    f"format_attempts={format_diagnostics.get('attempts', 0)}；"
                    f"valid={format_diagnostics.get('valid', False)}；"
                    f"response_length={format_diagnostics.get('response_length', 0)}；"
                    f"api_attempts={call_diagnostics.get('attempts', 0)}；"
                    f"api_success={call_diagnostics.get('succeeded', False)}；"
                    f"feedback={feedback or ['none']}；"
                    f"api_errors={[str(item)[:300] for item in api_errors] or ['none']}；"
                    f"engine_response={engine_summary}；"
                    f"history={history_summary}"
                )
                if info.get("action_fallback_reason"):
                    self.log_message.emit(
                        "安全兜底：模型计划不可执行，已转换为 agent.wait(1.333)；"
                        f"原因={info['action_fallback_reason']}"
                    )

                coordinate_match = re.search(
                    r"(?:click|click_at)\(\s*(\d+)\s*,\s*(\d+)", action_code
                )
                action_signature = action_code
                if coordinate_match:
                    action_signature = (
                        "click-region:"
                        f"{int(coordinate_match.group(1)) // 50}:"
                        f"{int(coordinate_match.group(2)) // 50}"
                    )
                if action_signature == previous_action_code:
                    repeated_action_count += 1
                else:
                    repeated_action_count = 1
                    previous_action_code = action_signature
                if repeated_action_count >= 3:
                    self.log_message.emit(f"循环警告：相同动作已连续出现 {repeated_action_count} 次。")

                lowered = action_code.casefold()
                if "done" in lowered:
                    self.completed.emit("模型判断任务已完成")
                    return
                if "fail" in lowered:
                    self.completed.emit("模型判断任务无法完成")
                    return
                if "next" in lowered:
                    continue
                if "wait" in lowered:
                    time.sleep(self.config["wait_delay"])
                    continue

                if locked_window_mode:
                    validate_target_window_action(
                        plan_code, keyboard_only=self.config["keyboard_only"]
                    )
                else:
                    validate_desktop_action(
                        plan_code, keyboard_only=self.config["keyboard_only"]
                    )
                if self._stop_event.is_set():
                    self.completed.emit("任务已停止")
                    return

                self.status_changed.emit(f"第 {step + 1}/{total_steps_label} 步：执行操作")
                import pyautogui
                import win32gui

                foreground_before = win32gui.GetForegroundWindow()
                pointer_match = re.search(
                    r"pyautogui\.(?:click|moveTo)\(\s*(-?\d+)\s*,\s*(-?\d+)",
                    action_code,
                )
                if pointer_match and not background_mode:
                    point_x = int(pointer_match.group(1))
                    point_y = int(pointer_match.group(2))
                    try:
                        point_description = describe_screen_point(point_x, point_y)
                    except Exception as exc:
                        self.log_message.emit(
                            "坐标落点预检不可用（不影响动作执行）：" f"{type(exc).__name__}: {exc}"
                        )
                    else:
                        self.log_message.emit(f"坐标落点预检：{point_description}")
                if background_mode:
                    exec(action_code, {"background": target})
                    self.log_message.emit(
                        "动作提交：后台消息；"
                        f"foreground_before={foreground_before}；"
                        f"input_hwnd={target._last_input_hwnd}；"
                        "delivery=仅表示消息已投递，是否被应用消费需由下一步截图验证"
                    )
                elif locked_window_mode:
                    target.ensure_foreground()
                    foreground_after_focus = win32gui.GetForegroundWindow()
                    exec(action_code, {})
                    cursor_x, cursor_y = pyautogui.position()
                    self.log_message.emit(
                        "动作提交：前台输入；"
                        f"foreground_before={foreground_before}；"
                        f"foreground_after_focus={foreground_after_focus}；"
                        f"foreground_after_action={win32gui.GetForegroundWindow()}；"
                        f"cursor=({cursor_x}, {cursor_y})；"
                        "delivery=真实系统输入已发送，是否生效需由下一步截图验证"
                    )
                else:
                    exec(action_code, {})
                    cursor_x, cursor_y = pyautogui.position()
                    self.log_message.emit(
                        "动作提交：全屏多窗口输入；"
                        f"foreground_before={foreground_before}；"
                        f"foreground_after_action={win32gui.GetForegroundWindow()}；"
                        f"cursor=({cursor_x}, {cursor_y})；"
                        "delivery=系统输入已发送，允许前台窗口发生切换"
                    )
                self.status_changed.emit(f"第 {step + 1}/{total_steps_label} 步：等待界面稳定")
                (
                    settled_image,
                    settle_seconds,
                    settle_change,
                    settled,
                ) = self._wait_for_visual_settle(target, comparable_capture)
                previous_capture = settled_image.copy()
                settle_status = "已稳定" if settled else "等待超时"
                interaction_feedback = (
                    f"上一动作执行后本地等待 {settle_seconds:.2f}s；{settle_status}；"
                    f"期间最大画面变化 {settle_change:.3f}%。"
                )
                if settle_change < self.config["settle_meaningful_change"]:
                    interaction_feedback += "动作未产生明显视觉变化；不要原样重复，应重新检查焦点或改用其他操作。"
                else:
                    interaction_feedback += (
                        "动作已产生可见变化；下一轮应以稳定后的最终画面重新判断，" "不要把过渡状态当成未执行。"
                    )
                obs["interaction_feedback"] = interaction_feedback
                self.log_message.emit(f"动作稳定检测：{interaction_feedback}")
                total_ms = round((time.perf_counter() - step_started) * 1000)
                self.log_message.emit(f"本步总耗时：{total_ms} ms")

            if not self.config["infinite_run"]:
                self.completed.emit("已达到最大操作步数")
        except Exception as exc:
            details = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
            self.log_message.emit(details)
            self.failed.emit(str(exc))


class AgentSWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.worker = None
        self._restore_after_run = False
        self._pinned_target = None
        self.last_pixmap = None
        self.current_log_path = None
        self.latest_log_path = (
            Path(__file__).resolve().parents[2] / "logs" / "gui_runs" / "latest.log"
        )
        self.setWindowTitle("AEye")
        self.resize(1280, 820)
        self.setMinimumSize(900, 520)
        self._build_ui()
        self._apply_style()
        self.refresh_windows()

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(18, 18, 18, 18)
        root_layout.setSpacing(12)

        title = QLabel("AEye 屏幕与窗口代理")
        title.setObjectName("pageTitle")
        subtitle = QLabel("锁定单窗口，或观察完整桌面并在多个窗口之间切换操作。")
        subtitle.setObjectName("subtitle")
        root_layout.addWidget(title)
        root_layout.addWidget(subtitle)

        splitter = QSplitter(Qt.Horizontal)
        root_layout.addWidget(splitter, 1)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 8, 0)
        left_layout.setSpacing(12)

        target_group = QGroupBox("控制范围")
        target_layout = QGridLayout(target_group)
        self.control_mode_combo = QComboBox()
        self.control_mode_combo.addItem("锁定单窗口", "locked_window")
        self.control_mode_combo.addItem("全屏多窗口", "full_desktop")
        self.control_mode_combo.setToolTip("锁定模式只观察并操作一个窗口；全屏模式观察整个桌面并允许切换应用。")
        self.window_combo = QComboBox()
        self.window_combo.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon
        )
        self.refresh_button = QPushButton("刷新")
        self.preview_button = QPushButton("预览")
        target_layout.addWidget(QLabel("控制范围"), 0, 0)
        target_layout.addWidget(self.control_mode_combo, 0, 1)
        target_layout.addWidget(self.window_combo, 1, 0, 1, 2)
        target_layout.addWidget(self.refresh_button, 2, 0)
        target_layout.addWidget(self.preview_button, 2, 1)
        left_layout.addWidget(target_group)

        task_group = QGroupBox("任务")
        task_layout = QVBoxLayout(task_group)
        self.task_edit = QTextEdit()
        self.task_edit.setPlaceholderText("例如：在记事本中输入 Hello Agent-S，然后保存文件。")
        self.task_edit.setMinimumHeight(120)
        task_layout.addWidget(self.task_edit)
        left_layout.addWidget(task_group)

        model_group = QGroupBox("模型配置")
        model_form = QFormLayout(model_group)
        self.main_model_edit = QLineEdit("gpt-5.4-mini")
        self.main_url_edit = QLineEdit("https://ai.markfan.dpdns.org/v1")
        self.ground_model_edit = QLineEdit("bytedance/ui-tars-1.5-7b")
        self.ground_url_edit = QLineEdit("https://openrouter.ai/api/v1")
        for field in (
            self.main_model_edit,
            self.main_url_edit,
            self.ground_model_edit,
            self.ground_url_edit,
        ):
            field.setMinimumHeight(34)
        model_form.addRow("主模型", self.main_model_edit)
        model_form.addRow("主模型 URL", self.main_url_edit)
        model_form.addRow("Grounding", self.ground_model_edit)
        model_form.addRow("Grounding URL", self.ground_url_edit)
        left_layout.addWidget(model_group)

        options_group = QGroupBox("运行设置")
        options_form = QFormLayout(options_group)
        self.reflection_checkbox = QCheckBox("启用动作反思")
        self.reflection_checkbox.setChecked(False)
        self.fast_checkbox = QCheckBox("快速模式（主模型直接定位）")
        self.fast_checkbox.setChecked(True)
        self.fast_checkbox.setToolTip("跳过独立 Grounding 请求，主模型直接输出 0-1000 归一化坐标。")
        self.keyboard_only_checkbox = QCheckBox("仅键盘模式（禁止鼠标输入）")
        self.keyboard_only_checkbox.setChecked(True)
        self.keyboard_only_checkbox.setToolTip(
            "模型只能使用 Tab、Shift+Tab、方向键、空格、Enter、Esc、快捷键和文本输入。"
        )
        self.background_checkbox = QCheckBox("实验性后台模式（可遮挡，不可最小化）")
        self.background_checkbox.setToolTip(
            "不抢鼠标和前台。适合经典 Win32 程序；部分浏览器、Electron、UWP " "和游戏可能不响应后台消息。"
        )
        self.max_steps_spin = QSpinBox()
        self.max_steps_spin.setRange(1, 100)
        self.max_steps_spin.setValue(15)
        self.infinite_run_checkbox = QCheckBox("永久循环（直到手动停止）")
        self.infinite_run_checkbox.setChecked(False)
        self.infinite_run_checkbox.setToolTip("开启后忽略最大步数，持续执行直到点击停止、关闭程序或发生错误。")
        self.trajectory_spin = QSpinBox()
        self.trajectory_spin.setRange(1, 32)
        self.trajectory_spin.setValue(2)
        self.max_steps_spin.setMinimumHeight(34)
        self.trajectory_spin.setMinimumHeight(34)
        options_form.addRow(self.reflection_checkbox)
        options_form.addRow(self.fast_checkbox)
        options_form.addRow(self.keyboard_only_checkbox)
        options_form.addRow(self.background_checkbox)
        options_form.addRow(self.infinite_run_checkbox)
        options_form.addRow("最大步数", self.max_steps_spin)
        options_form.addRow("保留历史轮数", self.trajectory_spin)
        left_layout.addWidget(options_group)

        button_row = QHBoxLayout()
        self.start_button = QPushButton("开始")
        self.start_button.setObjectName("primaryButton")
        self.pause_button = QPushButton("暂停")
        self.stop_button = QPushButton("停止")
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        button_row.addWidget(self.start_button)
        button_row.addWidget(self.pause_button)
        button_row.addWidget(self.stop_button)
        left_layout.addLayout(button_row)
        left_layout.addStretch(1)

        # Preserve the settings panel's natural height. When the main window is
        # shorter, the scroll area should scroll instead of crushing form rows.
        left_layout.activate()
        left_panel.setMinimumHeight(left_layout.sizeHint().height())
        self.left_scroll = QScrollArea()
        self.left_scroll.setObjectName("settingsScroll")
        self.left_scroll.setWidgetResizable(True)
        self.left_scroll.setFrameShape(QFrame.NoFrame)
        self.left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.left_scroll.setWidget(left_panel)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(8, 0, 0, 0)
        right_layout.setSpacing(12)

        preview_group = QGroupBox("窗口预览")
        preview_layout = QVBoxLayout(preview_group)
        self.preview_label = QLabel("选择控制范围后点击“预览”")
        self.preview_label.setObjectName("preview")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumHeight(360)
        preview_layout.addWidget(self.preview_label)
        right_layout.addWidget(preview_group, 3)

        log_group = QGroupBox("运行日志")
        log_layout = QVBoxLayout(log_group)
        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumBlockCount(3000)
        log_layout.addWidget(self.log_edit)
        self.open_log_button = QPushButton("打开最近日志")
        log_layout.addWidget(self.open_log_button)
        right_layout.addWidget(log_group, 2)

        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("status")
        right_layout.addWidget(self.status_label)

        splitter.addWidget(self.left_scroll)
        splitter.addWidget(right_panel)
        splitter.setSizes([430, 820])

        self.refresh_button.clicked.connect(self.refresh_windows)
        self.preview_button.clicked.connect(self.preview_selected_window)
        self.window_combo.currentIndexChanged.connect(self.preview_selected_window)
        self.control_mode_combo.currentIndexChanged.connect(self._control_mode_changed)
        self.background_checkbox.toggled.connect(self.preview_selected_window)
        self.start_button.clicked.connect(self.start_agent)
        self.pause_button.clicked.connect(self.toggle_pause)
        self.stop_button.clicked.connect(self.stop_agent)
        self.fast_checkbox.toggled.connect(self._fast_mode_changed)
        self.infinite_run_checkbox.toggled.connect(self._infinite_run_changed)
        self.open_log_button.clicked.connect(self.open_latest_log)
        self._fast_mode_changed(self.fast_checkbox.isChecked())
        self._infinite_run_changed(self.infinite_run_checkbox.isChecked())
        self._control_mode_changed()

    def _fast_mode_changed(self, enabled: bool):
        if enabled:
            self.reflection_checkbox.setChecked(False)
        self.reflection_checkbox.setEnabled(not enabled and not bool(self.worker))

    def _infinite_run_changed(self, enabled: bool):
        self.max_steps_spin.setEnabled(not enabled and not bool(self.worker))

    def current_control_mode(self) -> str:
        return self.control_mode_combo.currentData() or "locked_window"

    def _control_mode_changed(self, *_):
        locked = self.current_control_mode() == "locked_window"
        idle = not bool(self.worker)
        self.window_combo.setEnabled(locked and idle)
        self.refresh_button.setEnabled(locked and idle)
        self.background_checkbox.setEnabled(locked and idle)
        if not locked:
            self.status_label.setText("全屏多窗口模式：允许 Alt+Tab、任务栏和应用切换")
        self.preview_selected_window()

    def _apply_style(self):
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #f5f7fb; color: #182033; }
            QLabel#pageTitle { font-size: 25px; font-weight: 700; }
            QLabel#subtitle { color: #667085; font-size: 13px; margin-bottom: 5px; }
            QGroupBox {
                background: white; border: 1px solid #dfe4ee; border-radius: 10px;
                margin-top: 10px; padding: 12px;
            }
            QGroupBox::title {
                subcontrol-origin: margin; left: 12px; padding: 0 5px;
                font-weight: 600; color: #344054;
            }
            QLineEdit, QComboBox, QTextEdit, QPlainTextEdit, QSpinBox {
                background: #ffffff; border: 1px solid #cfd6e4; border-radius: 6px;
                padding: 7px; selection-background-color: #4f6bed;
            }
            QLineEdit:focus, QComboBox:focus, QTextEdit:focus, QPlainTextEdit:focus {
                border: 1px solid #4f6bed;
            }
            QPushButton {
                background: #eef1f7; border: 1px solid #d6dce8; border-radius: 7px;
                padding: 8px 14px; font-weight: 600;
            }
            QPushButton:hover { background: #e3e8f2; }
            QPushButton:disabled { color: #98a2b3; background: #f2f4f7; }
            QPushButton#primaryButton { background: #4f6bed; color: white; border: none; }
            QPushButton#primaryButton:hover { background: #4058cc; }
            QLabel#preview {
                background: #111827; color: #98a2b3; border-radius: 8px;
                border: 1px solid #273244;
            }
            QLabel#status {
                background: #eef4ff; color: #3155a6; border-radius: 7px;
                padding: 8px 12px; font-weight: 600;
            }
            """
        )

    def selected_window(self):
        return self.window_combo.currentData()

    def refresh_windows(self):
        previous = self.selected_window()
        previous_hwnd = previous.hwnd if previous else None
        self.window_combo.blockSignals(True)
        self.window_combo.clear()
        try:
            windows = list_target_windows()
            for info in windows:
                state = "最小化" if info.minimized else "可见"
                label = (
                    f"{info.title}  ·  {info.width}×{info.height}  ·  "
                    f"{state}  ·  PID {info.process_id}"
                )
                self.window_combo.addItem(label, info)
                if info.hwnd == previous_hwnd:
                    self.window_combo.setCurrentIndex(self.window_combo.count() - 1)
            self.status_label.setText(f"找到 {len(windows)} 个可监控窗口")
        except Exception as exc:
            QMessageBox.critical(self, "窗口枚举失败", str(exc))
        finally:
            self.window_combo.blockSignals(False)
        if self.window_combo.count():
            self.preview_selected_window()

    def preview_selected_window(self):
        try:
            if self.current_control_mode() == "full_desktop":
                controller = DesktopController()
            else:
                info = self.selected_window()
                if not info:
                    return
                controller = TargetWindowController.from_hwnd(
                    info.hwnd, background=self.background_checkbox.isChecked()
                )
            image, _ = controller.capture()
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            self.show_screenshot(buffer.getvalue())
        except TargetWindowError as exc:
            self.status_label.setText(str(exc))

    def show_screenshot(self, data: bytes):
        pixmap = QPixmap()
        pixmap.loadFromData(data, "PNG")
        self.last_pixmap = pixmap
        self._render_pixmap()

    def _render_pixmap(self):
        if not self.last_pixmap or self.last_pixmap.isNull():
            return
        size = self.preview_label.size()
        scaled = self.last_pixmap.scaled(
            size, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        self.preview_label.setPixmap(scaled)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render_pixmap()

    def append_log(self, message: str):
        self.log_edit.appendPlainText(message)
        if self.current_log_path:
            try:
                timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                with self.current_log_path.open("a", encoding="utf-8") as log_file:
                    log_file.write(f"[{timestamp}] {message}\n")
                    log_file.flush()
            except OSError as exc:
                self.current_log_path = None
                self.status_label.setText(f"日志保存失败：{exc}")
        scrollbar = self.log_edit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _begin_run_log(self, info: WindowInfo, task: str, config: dict):
        self.latest_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.latest_log_path.write_text(
            "AEye latest run\n"
            f"started_at={datetime.now().astimezone().isoformat()}\n"
            f"control_mode={config['control_mode']}\n"
            f"window_title={info.title}\n"
            f"pid={info.process_id}\n"
            f"hwnd={info.hwnd}\n"
            f"initial_geometry=({info.left}, {info.top}, {info.width}, {info.height})\n"
            f"task={task}\n"
            f"main_model={config['main_model']}\n"
            f"grounding_model={config['grounding_model']}\n"
            f"fast_mode={config['fast_mode']}\n"
            f"background_mode={config['background_mode']}\n"
            f"keyboard_only={config['keyboard_only']}\n"
            f"reflection={config['enable_reflection']}\n"
            f"infinite_run={config['infinite_run']}\n"
            "api_keys=environment-only; not recorded\n"
            "---\n",
            encoding="utf-8",
        )
        self.current_log_path = self.latest_log_path

    def open_latest_log(self):
        if not self.latest_log_path.is_file():
            QMessageBox.information(self, "暂无日志", "还没有保存过运行日志。")
            return
        os.startfile(str(self.latest_log_path))

    def start_agent(self):
        control_mode = self.current_control_mode()
        try:
            info = (
                self.selected_window()
                if control_mode == "locked_window"
                else DesktopController().current_info()
            )
        except TargetWindowError as exc:
            QMessageBox.critical(self, "控制范围不可用", str(exc))
            return
        task = self.task_edit.toPlainText().strip()
        if not info:
            QMessageBox.warning(self, "缺少窗口", "请先选择目标窗口。")
            return
        if not task:
            QMessageBox.warning(self, "缺少任务", "请输入要执行的任务。")
            return
        try:
            get_configured_secret("fyx_api_key")
            if not self.fast_checkbox.isChecked():
                get_configured_secret("OPENROUTER_API_KEY")
        except RuntimeError as exc:
            QMessageBox.critical(self, "缺少 API Key", str(exc))
            return

        config = {
            "main_model": self.main_model_edit.text().strip(),
            "main_url": self.main_url_edit.text().strip(),
            "grounding_model": self.ground_model_edit.text().strip(),
            "grounding_url": self.ground_url_edit.text().strip(),
            "grounding_width": 1920,
            "grounding_height": 1080,
            "enable_reflection": self.reflection_checkbox.isChecked(),
            "max_steps": self.max_steps_spin.value(),
            "infinite_run": self.infinite_run_checkbox.isChecked(),
            "trajectory_length": self.trajectory_spin.value(),
            "control_mode": control_mode,
            "background_mode": (
                self.background_checkbox.isChecked() and control_mode == "locked_window"
            ),
            "fast_mode": self.fast_checkbox.isChecked(),
            "keyboard_only": self.keyboard_only_checkbox.isChecked(),
            "system_prompt_addendum": "",
            "max_image_dimension": 1280 if self.fast_checkbox.isChecked() else 2400,
            "desktop_main_max_dimension": 1280,
            "action_delay": 0.2 if self.fast_checkbox.isChecked() else 1.0,
            "wait_delay": 0.5 if self.fast_checkbox.isChecked() else 2.0,
            "settle_timeout": 2.0,
            "settle_poll_interval": 0.15,
            "settle_stable_frames": 3,
            "settle_stable_threshold": 0.04,
            "settle_meaningful_change": 0.05,
            "settle_no_change_grace": 0.8,
        }
        try:
            self._begin_run_log(info, task, config)
        except OSError as exc:
            self.current_log_path = None
            QMessageBox.warning(self, "日志保存失败", str(exc))
        self.log_edit.clear()
        self.append_log(f"任务：{task}")
        self.append_log(f"最近运行日志：{self.latest_log_path}")
        if control_mode == "locked_window":
            try:
                pin_controller = TargetWindowController.from_hwnd(info.hwnd)
                was_topmost = pin_controller.is_always_on_top()
                if not was_topmost:
                    pin_controller.set_always_on_top(True)
                self._pinned_target = (pin_controller, was_topmost)
                self.append_log("锁定窗口置顶：任务期间保持目标窗口始终置顶；结束后恢复原状态。")
            except Exception as exc:
                self._pinned_target = None
                QMessageBox.critical(self, "窗口置顶失败", str(exc))
                return
        self.worker = AgentWorker(info, task, config)
        self.worker.log_message.connect(self.append_log)
        self.worker.screenshot_ready.connect(self.show_screenshot)
        self.worker.status_changed.connect(self.status_label.setText)
        self.worker.completed.connect(self._agent_completed)
        self.worker.failed.connect(self._agent_failed)
        self.worker.finished.connect(self._worker_finished)
        self._set_running(True)
        self._restore_after_run = control_mode == "full_desktop"
        if self._restore_after_run:
            self.append_log("全屏运行保护：已最小化 AEye，避免自身窗口遮挡或接收模型点击。")
            self.showMinimized()
            QApplication.processEvents()
        self.worker.start()

    def toggle_pause(self):
        if not self.worker:
            return
        pause = self.pause_button.text() == "暂停"
        self.worker.set_paused(pause)
        self.pause_button.setText("继续" if pause else "暂停")
        self.status_label.setText("已暂停" if pause else "继续运行")

    def stop_agent(self):
        if self.worker:
            worker = self.worker
            self.status_label.setText("正在立即停止…")
            self.stop_button.setEnabled(False)
            self._restore_pinned_target()
            self._terminate_worker_immediately(worker)
            self.append_log("任务已由用户立即停止；未等待当前模型请求返回。")
            self.status_label.setText("任务已停止")

    @staticmethod
    def _terminate_worker_immediately(worker):
        """Stop a worker without waiting for a blocking model request to return."""
        worker.request_stop()
        if not worker.isRunning():
            return True
        worker.terminate()
        return worker.wait(500)

    def _agent_completed(self, message: str):
        self.append_log(message)
        self.status_label.setText(message)

    def _agent_failed(self, message: str):
        self.append_log(f"运行失败：{message}")
        self.status_label.setText("运行失败")
        self._restore_pinned_target()
        self._restore_main_window_after_run()
        QMessageBox.critical(self, "Agent-S 运行失败", message)

    def _worker_finished(self):
        self._set_running(False)
        self._restore_pinned_target()
        self.worker = None
        self._restore_main_window_after_run()

    def _restore_pinned_target(self):
        pinned = self._pinned_target
        self._pinned_target = None
        if not pinned:
            return
        controller, was_topmost = pinned
        if was_topmost:
            return
        try:
            controller.set_always_on_top(False)
            self.append_log("锁定窗口置顶：已恢复目标窗口原来的非置顶状态。")
        except Exception as exc:
            self.append_log(f"锁定窗口置顶恢复失败：{type(exc).__name__}: {exc}")

    def _restore_main_window_after_run(self):
        if not self._restore_after_run:
            return
        self._restore_after_run = False
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _set_running(self, running: bool):
        self.start_button.setEnabled(not running)
        self.preview_button.setEnabled(not running)
        self.control_mode_combo.setEnabled(not running)
        locked = self.current_control_mode() == "locked_window"
        self.refresh_button.setEnabled(not running and locked)
        self.window_combo.setEnabled(not running and locked)
        self.background_checkbox.setEnabled(not running and locked)
        self.fast_checkbox.setEnabled(not running)
        self.keyboard_only_checkbox.setEnabled(not running)
        self.infinite_run_checkbox.setEnabled(not running)
        self.max_steps_spin.setEnabled(
            not running and not self.infinite_run_checkbox.isChecked()
        )
        self.reflection_checkbox.setEnabled(
            not running and not self.fast_checkbox.isChecked()
        )
        self.pause_button.setEnabled(running)
        self.stop_button.setEnabled(running)
        self.pause_button.setText("暂停")

    def closeEvent(self, event: QCloseEvent):
        if self.worker and self.worker.isRunning():
            worker = self.worker
            self._restore_pinned_target()
            self._terminate_worker_immediately(worker)
        else:
            self._restore_pinned_target()
        event.accept()


def main():
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("AEye")
    app.setStyle("Fusion")
    window = AgentSWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
