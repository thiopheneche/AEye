"""PySide6 desktop interface for the Agent-S target-window prototype."""

import base64
import html
import io
import itertools
import json
import os
import re
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageChops, ImageStat
from PySide6.QtCore import QRectF, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QFont,
    QFontDatabase,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
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
    get_foreground_window_info,
    list_target_windows,
    validate_desktop_action,
    validate_target_window_action,
)

DEFAULT_MAIN_MODEL = ""


def default_model_profiles_path() -> Path:
    """Store user model settings in an explicitly ignored local file."""
    return Path(__file__).resolve().parents[2] / "config" / "model_profiles.local.json"


def legacy_model_profiles_path() -> Path:
    """Return the pre-local-file profile path for one-time migration."""
    config_root = os.getenv("APPDATA")
    if config_root:
        return Path(config_root) / "AEye" / "model_profiles.json"
    return Path.home() / ".config" / "AEye" / "model_profiles.json"


def protect_api_key(value: str) -> str:
    value = str(value or "")
    if not value:
        return ""
    encoded = value.encode("utf-8")
    if os.name == "nt":
        import win32crypt

        protected = win32crypt.CryptProtectData(
            encoded, "AEye model profile", None, None, None, 0
        )
        return "dpapi:" + base64.b64encode(protected).decode("ascii")
    return "local:" + base64.b64encode(encoded).decode("ascii")


def unprotect_api_key(value: str) -> str:
    value = str(value or "")
    if not value:
        return ""
    scheme, separator, payload = value.partition(":")
    if not separator:
        raise ValueError("Stored API key has an invalid format.")
    decoded = base64.b64decode(payload.encode("ascii"))
    if scheme == "dpapi":
        if os.name != "nt":
            raise ValueError("This API key can only be decrypted by its Windows user.")
        import win32crypt

        return win32crypt.CryptUnprotectData(decoded, None, None, None, 0)[1].decode(
            "utf-8"
        )
    if scheme == "local":
        return decoded.decode("utf-8")
    raise ValueError(f"Unsupported API key protection scheme: {scheme}")


def normalize_model_profile(profile: dict) -> dict:
    """Keep only supported model settings in memory."""
    return {
        "main_model": str(profile.get("main_model", "")).strip(),
        "main_url": str(profile.get("main_url", "")).strip(),
        "main_api_key": str(profile.get("main_api_key", "")).strip(),
        "grounding_model": str(profile.get("grounding_model", "")).strip(),
        "grounding_url": str(profile.get("grounding_url", "")).strip(),
        "grounding_api_key": str(profile.get("grounding_api_key", "")).strip(),
    }


def load_model_profiles(path: Path) -> dict:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Model profile file must contain a JSON object.")
    profiles = payload.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError("Model profile file has an invalid profiles object.")
    loaded_profiles = {}
    for name, profile in profiles.items():
        if not str(name).strip() or not isinstance(profile, dict):
            continue
        normalized = normalize_model_profile(profile)
        normalized["main_api_key"] = unprotect_api_key(
            profile.get("main_api_key_protected", "")
        )
        normalized["grounding_api_key"] = unprotect_api_key(
            profile.get("grounding_api_key_protected", "")
        )
        loaded_profiles[str(name)] = normalized
    return loaded_profiles


def save_model_profiles(path: Path, profiles: dict):
    """Atomically persist local model settings with protected API keys."""
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized_profiles = {}
    for name, profile in profiles.items():
        if not str(name).strip():
            continue
        normalized = normalize_model_profile(profile)
        serialized_profiles[str(name)] = {
            "main_model": normalized["main_model"],
            "main_url": normalized["main_url"],
            "main_api_key_protected": protect_api_key(normalized["main_api_key"]),
            "grounding_model": normalized["grounding_model"],
            "grounding_url": normalized["grounding_url"],
            "grounding_api_key_protected": protect_api_key(
                normalized["grounding_api_key"]
            ),
        }
    payload = {
        "version": 1,
        "profiles": serialized_profiles,
    }
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary_path.replace(path)


def scale_dimensions(width: int, height: int, max_dimension: int = 2400):
    scale = min(max_dimension / width, max_dimension / height, 1)
    return max(1, int(width * scale)), max(1, int(height * scale))


def format_decision_html(details: dict, dark: bool = True) -> str:
    """Render model-provided decision metadata without exposing hidden reasoning."""

    def escaped(name: str, fallback: str = "模型未提供") -> str:
        value = details.get(name)
        if value in (None, ""):
            value = fallback
        return html.escape(str(value)).replace("\n", "<br>")

    state = details.get("state", "ready")
    state_labels = {
        "waiting": "等待画面",
        "thinking": "分析中",
        "ready": "决策完成",
    }
    state_colors = {
        "waiting": "#7ce0ff",
        "thinking": "#ffb020",
        "ready": "#34d399",
    }
    state_label = state_labels.get(state, str(state))
    state_color = state_colors.get(state, "#3155a6")
    text_color = "#e8ecf4" if dark else "#182033"
    muted_color = "#6d7789" if dark else "#667085"
    label_color = "#a5aeba" if dark else "#344054"
    code_color = "#7ce0ff" if dark else "#3155a6"
    code_background = "#102731" if dark else "#eef4ff"
    step = html.escape(str(details.get("step", "—")))
    duration = details.get("decision_ms")
    duration_text = f" · {int(duration)} ms" if duration is not None else ""
    grounding = details.get("grounding_info")
    grounding_html = ""
    if grounding:
        grounding_html = (
            f'<div style="margin-top:10px;color:{muted_color};"><b style="color:{label_color};">定位来源</b><br>'
            f"{html.escape(str(grounding)).replace(chr(10), '<br>')}</div>"
        )

    return f"""
    <!-- legacy overlay tokens retained for compatibility: #e5e7eb #fec84b -->
    <div style="font-family:'Noto Sans SC','Microsoft YaHei UI';color:{text_color};line-height:1.55;">
      <div style="margin-bottom:12px;">
        <span style="color:{state_color};font-weight:700;">● {html.escape(state_label)}</span>
        <span style="color:{muted_color};"> · 第 {step} 步{duration_text}</span>
      </div>
      <div style="margin-bottom:10px;"><b style="color:{label_color};">观察</b><br>{escaped('observation')}</div>
      <div style="margin-bottom:10px;"><b style="color:{label_color};">目标</b><br>{escaped('goal')}</div>
      <div style="margin-bottom:10px;"><b style="color:{label_color};">依据</b><br>{escaped('reason')}</div>
      <div style="margin-bottom:10px;"><b style="color:{label_color};">模型计划</b><br>{escaped('plan', '等待模型返回计划…')}</div>
      <div><b style="color:{label_color};">下一步动作</b><br><span style="font-family:'Cascadia Code','Consolas';color:{code_color};background:{code_background};">{escaped('action', '等待模型返回动作…')}</span></div>
      {grounding_html}
    </div>
    """


def format_overlay_title(state: str, step=None, displayed_step=None) -> str:
    """Describe pending work without replacing the last readable decision."""
    if state == "thinking":
        if displayed_step is None:
            return f"AEye · 正在准备第 {step} 步"
        return f"AEye · 正在准备第 {step} 步 · 当前显示第 {displayed_step} 步"
    if state == "executing" and step is not None:
        return f"AEye · 正在执行第 {step} 步"
    if displayed_step is not None:
        return f"AEye · 第 {displayed_step} 步决策"
    return "AEye · 主模型决策"


class AgentWorker(QThread):
    log_message = Signal(str)
    screenshot_ready = Signal(bytes)
    decision_update = Signal(object)
    overlay_decision_update = Signal(object)
    overlay_status_update = Signal(object)
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

            main_key = self.config["main_api_key"]
            grounding_key = self.config["grounding_api_key"]
            control_mode = self.config["control_mode"]
            locked_window_mode = control_mode == "locked_window"
            if locked_window_mode:
                if self.window_info is None:
                    raise TargetWindowError("锁定单窗口模式缺少目标窗口。")
                target = TargetWindowController.from_hwnd(self.window_info.hwnd)
                initial = target.current_info()
                import win32gui

                window_class = win32gui.GetClassName(initial.hwnd)
                self.log_message.emit(f"目标窗口类：{window_class}")
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
            agent = AgentS3(
                main_engine,
                grounding_agent,
                platform="windows",
                max_trajectory_length=self.config["trajectory_length"],
                enable_reflection=self.config["enable_reflection"],
                system_prompt_addendum=system_prompt_addendum,
            )

            if locked_window_mode:
                self.log_message.emit(
                    f'已绑定窗口："{initial.title}" '
                    f"(PID={initial.process_id}, HWND={initial.hwnd})"
                )
            else:
                self.log_message.emit(
                    "控制范围：完整虚拟桌面；允许通过 Alt+Tab、任务栏或应用切换动作"
                    "在多个窗口之间操作"
                )
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
            if locked_window_mode:
                self.log_message.emit("操作模式：锁定单窗口 + 前台鼠标键盘")
            else:
                self.log_message.emit("操作模式：全屏多窗口 + 前台鼠标键盘")
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

                self.status_changed.emit(
                    f"第 {step + 1}/{total_steps_label} 步：截取画面"
                )
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
                    self.log_message.emit(
                        f"界面稳定后至本轮截图的变化率：{change_percent:.3f}%"
                    )
                previous_capture = comparable_capture.copy()
                grounding_agent.set_coordinate_space(
                    current.width,
                    current.height,
                    offset_x=current.left,
                    offset_y=current.top,
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
                obs.pop("preferred_grounding_region", None)
                if locked_window_mode:
                    obs["preferred_grounding_region"] = (
                        0,
                        0,
                        grounding_width,
                        grounding_height,
                    )
                else:
                    foreground_info = get_foreground_window_info()
                    if foreground_info is not None and not foreground_info.minimized:
                        left = round(
                            (foreground_info.left - current.left)
                            * grounding_width
                            / current.width
                        )
                        top = round(
                            (foreground_info.top - current.top)
                            * grounding_height
                            / current.height
                        )
                        right = round(
                            (
                                foreground_info.left
                                + foreground_info.width
                                - current.left
                            )
                            * grounding_width
                            / current.width
                        )
                        bottom = round(
                            (foreground_info.top + foreground_info.height - current.top)
                            * grounding_height
                            / current.height
                        )
                        preferred_region = (
                            max(0, left),
                            max(0, top),
                            min(grounding_width, right),
                            min(grounding_height, bottom),
                        )
                        if (
                            preferred_region[2] > preferred_region[0]
                            and preferred_region[3] > preferred_region[1]
                        ):
                            obs["preferred_grounding_region"] = preferred_region
                            self.log_message.emit(
                                "前台窗口定位范围："
                                f"title={foreground_info.title!r}；"
                                f"hwnd={foreground_info.hwnd}；"
                                f"grounding_region={preferred_region}"
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

                if screenshot.size == (grounding_width, grounding_height):
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

                thinking_details = {
                    "state": "thinking",
                    "step": step + 1,
                    "observation": "主模型正在观察当前截图并生成下一步决策…",
                    "goal": "分析中…",
                    "reason": "分析中…",
                    "plan": "等待模型返回计划…",
                    "action": "等待模型返回动作…",
                }
                self.decision_update.emit(thinking_details)
                self.overlay_status_update.emit({"state": "thinking", "step": step + 1})
                self.status_changed.emit(
                    f"第 {step + 1}/{total_steps_label} 步：模型决策中"
                )
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
                self.log_message.emit(
                    f"行为目标：{info.get('action_goal', '模型未提供')}"
                )
                self.log_message.emit(
                    f"行为原因：{info.get('action_reason', '模型未提供')}"
                )
                self.log_message.emit(f"模型原始计划：\n{plan}")
                self.log_message.emit(f"执行代码：{action_code}")
                if info.get("grounding_info"):
                    self.log_message.emit(f"定位来源：{info['grounding_info']}")
                self.log_message.emit(f"模型决策耗时：{decision_ms} ms")
                decision_details = {
                    "state": "ready",
                    "step": step + 1,
                    "decision_ms": decision_ms,
                    "observation": info.get("observation_summary", "模型未提供"),
                    "goal": info.get("action_goal", "模型未提供"),
                    "reason": info.get("action_reason", "模型未提供"),
                    "plan": plan,
                    "action": action_code,
                    "grounding_info": info.get("grounding_info"),
                }
                self.decision_update.emit(decision_details)
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
                    r"click\(\s*(\d+)\s*,\s*(\d+)", action_code
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
                    self.log_message.emit(
                        f"循环警告：相同动作已连续出现 {repeated_action_count} 次。"
                    )

                lowered = action_code.casefold()
                if "done" in lowered:
                    self.overlay_decision_update.emit(decision_details)
                    self.completed.emit("模型判断任务已完成")
                    return
                if "fail" in lowered:
                    self.overlay_decision_update.emit(decision_details)
                    self.completed.emit("模型判断任务无法完成")
                    return
                if "next" in lowered:
                    continue
                if "wait" in lowered:
                    self.overlay_decision_update.emit(decision_details)
                    self.overlay_status_update.emit(
                        {"state": "executing", "step": step + 1}
                    )
                    time.sleep(self.config["wait_delay"])
                    continue

                if locked_window_mode:
                    validate_target_window_action(plan_code)
                else:
                    validate_desktop_action(plan_code)
                if self._stop_event.is_set():
                    self.completed.emit("任务已停止")
                    return

                self.overlay_decision_update.emit(decision_details)
                self.overlay_status_update.emit(
                    {"state": "executing", "step": step + 1}
                )
                self.status_changed.emit(
                    f"第 {step + 1}/{total_steps_label} 步：执行操作"
                )
                import pyautogui
                import win32gui

                foreground_before = win32gui.GetForegroundWindow()
                pointer_match = re.search(
                    r"pyautogui\.(?:click|moveTo)\(\s*(-?\d+)\s*,\s*(-?\d+)",
                    action_code,
                )
                if pointer_match:
                    point_x = int(pointer_match.group(1))
                    point_y = int(pointer_match.group(2))
                    try:
                        point_description = describe_screen_point(point_x, point_y)
                    except Exception as exc:
                        self.log_message.emit(
                            "坐标落点预检不可用（不影响动作执行）："
                            f"{type(exc).__name__}: {exc}"
                        )
                    else:
                        self.log_message.emit(f"坐标落点预检：{point_description}")
                if locked_window_mode:
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
                self.status_changed.emit(
                    f"第 {step + 1}/{total_steps_label} 步：等待界面稳定"
                )
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
                        "动作已产生可见变化；下一轮应以稳定后的最终画面重新判断，"
                        "不要把过渡状态当成未执行。"
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


class LogoWidget(QWidget):
    """Paint the original AEye eye mark without changing its proportions."""

    def __init__(self, parent=None, show_wordmark=True):
        super().__init__(parent)
        self.show_wordmark = show_wordmark
        self.setMinimumSize(126 if show_wordmark else 44, 42)
        self.setSizePolicy(
            self.sizePolicy().horizontalPolicy(), self.sizePolicy().verticalPolicy()
        )

    def sizeHint(self):
        return QSize(126 if self.show_wordmark else 44, 42)

    def paintEvent(self, event):
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        eye_left = 5.0
        eye_top = (self.height() - 24.0) / 2.0
        painter.save()
        painter.translate(eye_left + 17.0, eye_top + 12.0)
        painter.rotate(-8.0)
        painter.translate(-17.0, -12.0)
        path = QPainterPath()
        # Match the original mark: a lifted upper-right corner, a full lower-left
        # curve, and a narrow right-hand taper instead of a symmetric eye.
        path.moveTo(1.5, 16.8)
        path.cubicTo(0.8, 9.0, 8.0, 2.3, 20.5, 0.9)
        path.cubicTo(26.5, 0.2, 32.5, 0.5, 34.0, 2.6)
        path.cubicTo(36.2, 5.8, 33.7, 11.8, 30.3, 15.9)
        path.cubicTo(25.8, 21.5, 18.5, 24.0, 7.8, 23.5)
        path.cubicTo(3.6, 23.2, 1.0, 21.8, 1.5, 16.8)
        painter.setPen(QPen(QColor("#22d3ee"), 2.0))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)
        painter.setPen(QPen(QColor("#f43f8e"), 3.0))
        painter.drawEllipse(QRectF(14.5, 7.0, 9.5, 9.5))
        painter.restore()
        if self.show_wordmark:
            font = QFont("Segoe UI", 18, QFont.Bold)
            font.setLetterSpacing(QFont.AbsoluteSpacing, 0.8)
            painter.setFont(font)
            painter.setPen(QColor("#e8ecf4"))
            painter.drawText(
                QRectF(51, 0, self.width() - 51, self.height()), Qt.AlignVCenter, "AEye"
            )

    @staticmethod
    def window_icon():
        pixmap = QPixmap(48, 48)
        pixmap.fill(Qt.transparent)
        widget = LogoWidget(show_wordmark=False)
        widget.resize(48, 48)
        widget.render(pixmap)
        return QIcon(pixmap)


class DecisionOverlay(QWidget):
    """Small click-through decision panel shown while the main window is hidden."""

    def __init__(self):
        super().__init__(
            None,
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
            | Qt.WindowTransparentForInput,
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setObjectName("decisionOverlay")
        self.resize(430, 370)
        self.displayed_step = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(7)
        self.title_label = QLabel("AEye · 主模型决策")
        self.title_label.setObjectName("overlayTitle")
        layout.addWidget(self.title_label)
        self.content = QTextEdit()
        self.content.setObjectName("overlayDecisionView")
        self.content.setReadOnly(True)
        self.content.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.content.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        layout.addWidget(self.content, 1)

        self.setStyleSheet(
            """
            QWidget#decisionOverlay {
                background: rgba(13, 17, 23, 245);
                border: 1px solid #22d3ee;
                border-radius: 9px;
            }
            QLabel#overlayTitle {
                color: #e8ecf4; font-size: 14px; font-weight: 700;
                padding-bottom: 5px; border-bottom: 1px solid #232b38;
            }
            QTextEdit#overlayDecisionView {
                background: transparent; color: #e8ecf4; border: none; padding: 2px;
            }
            """
        )

    def show_details(self, details: dict):
        step = details.get("step")
        if step is not None:
            self.displayed_step = step
        self.content.setHtml(format_decision_html(details, dark=True))
        self.content.verticalScrollBar().setValue(0)
        self.title_label.setText(
            format_overlay_title("ready", displayed_step=self.displayed_step)
        )

    def show_status(self, details: dict):
        self.title_label.setText(
            format_overlay_title(
                details.get("state", "ready"),
                step=details.get("step"),
                displayed_step=self.displayed_step,
            )
        )

    def reset_for_run(self, details: dict):
        self.displayed_step = None
        self.show_details(details)
        self.title_label.setText(format_overlay_title("waiting"))

    def show_at_top_left(self):
        screen = QApplication.primaryScreen()
        if screen:
            area = screen.availableGeometry()
            self.move(area.left() + 12, area.top() + 12)
        self.show()
        self.raise_()
        return self._exclude_from_capture()

    def _exclude_from_capture(self) -> bool:
        """Keep the user-visible overlay out of supported Windows captures."""
        if os.name != "nt":
            return False
        try:
            import ctypes

            hwnd = int(self.winId())
            return bool(ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x11))
        except Exception:
            return False


class AgentSWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.worker = None
        self._restore_after_run = False
        self._pinned_target = None
        self.decision_overlay = DecisionOverlay()
        self.model_profiles_path = default_model_profiles_path()
        self.model_profiles = {}
        self.last_pixmap = None
        self.current_log_path = None
        self.latest_log_path = (
            Path(__file__).resolve().parents[2] / "logs" / "gui_runs" / "latest.log"
        )
        self.setWindowTitle("AEye")
        self.resize(1500, 920)
        self.setMinimumSize(1080, 680)
        self._build_ui()
        self._apply_style()
        self._load_model_profiles()
        self.refresh_windows()

    def _build_ui_legacy(self):
        self.setWindowIcon(LogoWidget.window_icon())
        root = QWidget()
        root.setObjectName("appShell")
        self.setCentralWidget(root)
        shell = QHBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        def card(title, subtitle=""):
            frame = QFrame()
            frame.setObjectName("card")
            layout = QVBoxLayout(frame)
            layout.setContentsMargins(18, 14, 18, 18)
            layout.setSpacing(12)
            header = QHBoxLayout()
            heading = QLabel(title)
            heading.setObjectName("cardTitle")
            header.addWidget(heading)
            header.addStretch(1)
            if subtitle:
                hint = QLabel(subtitle)
                hint.setObjectName("cardSubtitle")
                header.addWidget(hint)
            layout.addLayout(header)
            return frame, layout

        def page_header(title, subtitle):
            header = QWidget()
            layout = QVBoxLayout(header)
            layout.setContentsMargins(0, 0, 0, 4)
            layout.setSpacing(3)
            heading = QLabel(title)
            heading.setObjectName("pageTitle")
            detail = QLabel(subtitle)
            detail.setObjectName("subtitle")
            detail.setWordWrap(True)
            layout.addWidget(heading)
            layout.addWidget(detail)
            return header

        navigation = QFrame()
        navigation.setObjectName("navigation")
        navigation.setFixedWidth(204)
        nav_layout = QVBoxLayout(navigation)
        nav_layout.setContentsMargins(10, 12, 10, 12)
        nav_layout.setSpacing(12)
        nav_layout.addWidget(LogoWidget())
        self.navigation_list = QListWidget()
        self.navigation_list.setObjectName("navigationList")
        self.navigation_list.setFocusPolicy(Qt.NoFocus)
        for icon, text in (
            ("▣", "新任务"),
            ("◉", "监督"),
            ("◷", "运行记录"),
            ("≛", "模型配置"),
            ("⚙", "设置"),
        ):
            item = QListWidgetItem(f" {icon}   {text}")
            item.setSizeHint(QSize(0, 42))
            self.navigation_list.addItem(item)
        nav_layout.addWidget(self.navigation_list, 1)
        footer = QLabel("AEye 0.4.0 · Agent-S 派生")
        footer.setObjectName("navFooter")
        nav_layout.addWidget(footer)
        shell.addWidget(navigation)

        self.page_stack = QStackedWidget()
        self.page_stack.setObjectName("pageStack")
        shell.addWidget(self.page_stack, 1)

        # Page 1: task setup.
        setup_scroll = QScrollArea()
        setup_scroll.setObjectName("pageScroll")
        setup_scroll.setWidgetResizable(True)
        setup_scroll.setFrameShape(QFrame.NoFrame)
        setup_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        setup_page = QWidget()
        setup_page.setObjectName("setupPage")
        setup_scroll.setWidget(setup_page)
        setup_layout = QVBoxLayout(setup_page)
        setup_layout.setContentsMargins(28, 22, 28, 24)
        setup_layout.setSpacing(14)
        setup_layout.addWidget(
            page_header(
                "新任务",
                "选择控制范围，设定任务并开始监督。运行期间你可以随时暂停、纠正或立即停止。",
            )
        )
        setup_columns = QHBoxLayout()
        setup_columns.setSpacing(16)
        setup_layout.addLayout(setup_columns)
        setup_left = QVBoxLayout()
        setup_right = QVBoxLayout()
        setup_left.setSpacing(14)
        setup_right.setSpacing(14)
        setup_columns.addLayout(setup_left, 56)
        setup_columns.addLayout(setup_right, 44)

        scope_card, scope_layout = card("控制范围", "模型能操作多大区域")
        scope_row = QHBoxLayout()
        scope_row.setSpacing(10)
        self.locked_scope_button = QPushButton(
            "◉  锁定单窗口\n\n只观察并操作所选窗口，禁止跨应用动作"
        )
        self.desktop_scope_button = QPushButton(
            "○  全屏多窗口\n\n观察整个虚拟桌面，允许在已打开窗口之间切换"
        )
        for button in (self.locked_scope_button, self.desktop_scope_button):
            button.setObjectName("scopeCard")
            button.setCheckable(True)
            button.setMinimumHeight(88)
            button.setMinimumWidth(0)
            button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            scope_row.addWidget(button, 1)
        scope_group = QButtonGroup(self)
        scope_group.setExclusive(True)
        scope_group.addButton(self.locked_scope_button)
        scope_group.addButton(self.desktop_scope_button)
        self.locked_scope_button.setChecked(True)
        scope_layout.addLayout(scope_row)
        scope_layout.addWidget(QLabel("目标窗口"), 0, Qt.AlignLeft)
        window_row = QHBoxLayout()
        self.window_combo = QComboBox()
        self.window_combo.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon
        )
        self.refresh_button = QPushButton("↻")
        self.refresh_button.setObjectName("iconButton")
        self.refresh_button.setToolTip("刷新窗口列表")
        self.preview_button = QPushButton("▣")
        self.preview_button.setObjectName("iconButton")
        self.preview_button.setToolTip("预览目标窗口")
        window_row.addWidget(self.window_combo, 1)
        window_row.addWidget(self.refresh_button)
        window_row.addWidget(self.preview_button)
        scope_layout.addLayout(window_row)
        self.setup_preview = QLabel("选择窗口后可在监督页查看实时截图")
        self.setup_preview.setObjectName("miniPreview")
        self.setup_preview.setAlignment(Qt.AlignCenter)
        self.setup_preview.setMinimumHeight(84)
        self.setup_preview.setWordWrap(True)
        scope_layout.addWidget(self.setup_preview)
        setup_left.addWidget(scope_card)

        task_card, task_layout = card("任务目标")
        self.task_edit = QTextEdit()
        self.task_edit.setPlaceholderText(
            "描述你希望 AEye 在目标窗口或桌面中完成的事情。"
        )
        self.task_edit.setMinimumHeight(130)
        task_layout.addWidget(self.task_edit)
        task_hint = QLabel("示例：在记事本中输入 Hello AEye，然后保存文件。")
        task_hint.setObjectName("fieldHint")
        task_layout.addWidget(task_hint)
        setup_left.addWidget(task_card)
        setup_left.addStretch(1)

        limits_card, limits_layout = card("运行限制")
        limits_form = QFormLayout()
        limits_form.setSpacing(12)
        self.max_steps_spin = QSpinBox()
        self.max_steps_spin.setRange(1, 100)
        self.max_steps_spin.setValue(15)
        self.max_steps_spin.setMinimumHeight(34)
        self.trajectory_spin = QSpinBox()
        self.trajectory_spin.setRange(1, 32)
        self.trajectory_spin.setValue(2)
        self.trajectory_spin.setMinimumHeight(34)
        self.infinite_run_checkbox = QCheckBox("永久循环")
        self.infinite_run_checkbox.setObjectName("switchCheck")
        self.infinite_run_checkbox.setChecked(False)
        self.infinite_run_checkbox.setToolTip(
            "开启后忽略最大步数，持续执行直到点击停止、关闭程序或发生错误。"
        )
        self.reflection_checkbox = QCheckBox("启用动作反思")
        self.reflection_checkbox.setObjectName("switchCheck")
        self.reflection_checkbox.setChecked(False)
        limits_form.addRow("最大步数", self.max_steps_spin)
        limits_form.addRow("保留历史轮数", self.trajectory_spin)
        limits_form.addRow(self.infinite_run_checkbox)
        limits_form.addRow(self.reflection_checkbox)
        limits_layout.addLayout(limits_form)
        setup_right.addWidget(limits_card)

        self.model_config_frame = QFrame()
        self.model_config_frame.setObjectName("expander")
        model_layout = QVBoxLayout(self.model_config_frame)
        model_layout.setContentsMargins(0, 0, 0, 0)
        model_layout.setSpacing(0)
        self.model_config_toggle = QPushButton()
        self.model_config_toggle.setObjectName("expanderButton")
        self.model_config_summary = QLabel()
        self.model_config_summary.setObjectName("expanderSummary")
        self.model_config_summary.setMinimumWidth(0)
        self.model_config_summary.setSizePolicy(
            QSizePolicy.Ignored, QSizePolicy.Preferred
        )
        model_header = QWidget()
        model_header.setObjectName("expanderHeader")
        model_header_layout = QHBoxLayout(model_header)
        model_header_layout.setContentsMargins(16, 12, 12, 12)
        model_header_layout.addWidget(QLabel("模型配置"))
        model_header_layout.addStretch(1)
        model_header_layout.addWidget(self.model_config_summary)
        model_header_layout.addWidget(self.model_config_toggle)
        model_layout.addWidget(model_header)
        self.model_config_body = QWidget()
        self.model_config_body.setObjectName("expanderBody")
        model_form = QFormLayout(self.model_config_body)
        model_form.setContentsMargins(16, 12, 16, 16)
        model_form.setSpacing(9)
        profile_widget = QWidget()
        profile_layout = QHBoxLayout(profile_widget)
        profile_layout.setContentsMargins(0, 0, 0, 0)
        profile_layout.setSpacing(6)
        self.model_profile_combo = QComboBox()
        self.model_profile_combo.addItem("选择已保存方案…", None)
        self.save_model_profile_button = QPushButton("保存")
        self.delete_model_profile_button = QPushButton("删除")
        profile_layout.addWidget(self.model_profile_combo, 1)
        profile_layout.addWidget(self.save_model_profile_button)
        profile_layout.addWidget(self.delete_model_profile_button)
        self.main_model_edit = QLineEdit(DEFAULT_MAIN_MODEL)
        self.main_url_edit = QLineEdit()
        self.main_api_key_edit = QLineEdit()
        self.ground_model_edit = QLineEdit()
        self.ground_url_edit = QLineEdit()
        self.ground_api_key_edit = QLineEdit()
        self.main_model_edit.setPlaceholderText("例如：gpt-5.5")
        self.main_url_edit.setPlaceholderText("例如：https://example.com/v1")
        self.main_api_key_edit.setPlaceholderText("保存在本地加密配置中")
        self.main_api_key_edit.setEchoMode(QLineEdit.Password)
        self.ground_model_edit.setPlaceholderText("例如：bytedance/ui-tars-1.5-7b")
        self.ground_url_edit.setPlaceholderText("例如：https://openrouter.ai/api/v1")
        self.ground_api_key_edit.setPlaceholderText("保存在本地加密配置中")
        self.ground_api_key_edit.setEchoMode(QLineEdit.Password)
        for field in (
            self.main_model_edit,
            self.main_url_edit,
            self.main_api_key_edit,
            self.ground_model_edit,
            self.ground_url_edit,
            self.ground_api_key_edit,
        ):
            field.setMinimumHeight(34)
        model_form.addRow("配置方案", profile_widget)
        model_form.addRow("主模型", self.main_model_edit)
        model_form.addRow("主模型 URL", self.main_url_edit)
        model_form.addRow("主模型 Key", self.main_api_key_edit)
        model_form.addRow("Grounding", self.ground_model_edit)
        model_form.addRow("Grounding URL", self.ground_url_edit)
        model_form.addRow("Grounding Key", self.ground_api_key_edit)
        model_layout.addWidget(self.model_config_body)
        self.model_config_body.setVisible(False)
        setup_right.addWidget(self.model_config_frame)

        boundary = QLabel(
            "✓  边界已确认　锁定模式仅操作所选窗口；跨窗口与系统级动作会被拦截。"
        )
        boundary.setObjectName("infoBarGood")
        boundary.setWordWrap(True)
        setup_right.addWidget(boundary)
        start_row = QHBoxLayout()
        self.start_button = QPushButton("开始任务")
        self.start_button.setObjectName("primaryButton")
        self.start_button.setMinimumHeight(42)
        start_note = QLabel("开始后主窗口最小化，左上角显示决策小窗")
        start_note.setObjectName("fieldHint")
        start_note.setWordWrap(True)
        start_row.addWidget(self.start_button)
        start_row.addWidget(start_note, 1)
        setup_right.addLayout(start_row)
        setup_right.addStretch(1)
        self.page_stack.addWidget(setup_scroll)

        # Page 2: live supervision.
        run_page = QWidget()
        run_page.setObjectName("runPage")
        run_layout = QVBoxLayout(run_page)
        run_layout.setContentsMargins(28, 14, 28, 14)
        run_layout.setSpacing(12)
        run_toolbar = QHBoxLayout()
        run_title = QLabel("监督")
        run_title.setObjectName("runTitle")
        self.run_task_summary = QLabel("· 等待任务开始")
        self.run_task_summary.setObjectName("runSubtitle")
        self.run_status_badge = QLabel("● 就绪")
        self.run_status_badge.setObjectName("statusBadge")
        self.run_model_button = QPushButton("≛  模型配置")
        self.run_back_button = QPushButton("‹  返回任务")
        self.run_top_pause_button = QPushButton("暂停")
        self.run_top_stop_button = QPushButton("停止")
        self.run_top_stop_button.setObjectName("dangerButton")
        run_toolbar.addWidget(run_title)
        run_toolbar.addWidget(self.run_task_summary, 1)
        run_toolbar.addWidget(self.run_model_button)
        run_toolbar.addWidget(self.run_back_button)
        run_toolbar.addWidget(self.run_status_badge)
        run_toolbar.addWidget(self.run_top_pause_button)
        run_toolbar.addWidget(self.run_top_stop_button)
        run_layout.addLayout(run_toolbar)

        run_splitter = QSplitter(Qt.Horizontal)
        run_splitter.setObjectName("runSplitter")
        run_layout.addWidget(run_splitter, 1)
        stage_card = QFrame()
        stage_card.setObjectName("stageCard")
        stage_layout = QVBoxLayout(stage_card)
        stage_layout.setContentsMargins(12, 12, 12, 10)
        stage_layout.setSpacing(8)
        stage_tags = QHBoxLayout()
        self.target_badge = QLabel("目标窗口 · 等待选择")
        self.target_badge.setObjectName("stageBadge")
        self.stability_badge = QLabel("● 界面等待中")
        self.stability_badge.setObjectName("stableBadge")
        stage_tags.addWidget(self.target_badge)
        stage_tags.addStretch(1)
        stage_tags.addWidget(self.stability_badge)
        stage_layout.addLayout(stage_tags)
        self.preview_label = QLabel("选择控制范围后点击“预览”")
        self.preview_label.setObjectName("preview")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumHeight(260)
        stage_layout.addWidget(self.preview_label, 1)
        capture_caption = QLabel("OBS · 每轮截图 → 主模型决策 · Grounding 物理坐标")
        capture_caption.setObjectName("captureCaption")
        stage_layout.addWidget(capture_caption)
        self.run_progress = QProgressBar()
        self.run_progress.setRange(0, 100)
        self.run_progress.setValue(0)
        self.run_progress.setTextVisible(False)
        stage_layout.addWidget(self.run_progress)
        run_splitter.addWidget(stage_card)

        run_side_scroll = QScrollArea()
        run_side_scroll.setObjectName("runSideScroll")
        run_side_scroll.setWidgetResizable(True)
        run_side_scroll.setFrameShape(QFrame.NoFrame)
        run_side_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        run_side = QWidget()
        run_side.setObjectName("runSide")
        run_side_layout = QVBoxLayout(run_side)
        run_side_layout.setContentsMargins(0, 0, 0, 0)
        run_side_layout.setSpacing(12)
        run_side_scroll.setWidget(run_side)
        decision_card, decision_layout = card("当前决策", "结构化输出")
        self.decision_edit = QTextEdit()
        self.decision_edit.setObjectName("decisionView")
        self.decision_edit.setReadOnly(True)
        self.decision_edit.setMinimumHeight(250)
        self.decision_edit.setHtml(
            format_decision_html(
                {
                    "state": "waiting",
                    "observation": "开始任务后，这里会同步显示主模型对当前截图的结构化判断。",
                    "goal": "等待任务开始…",
                    "reason": "仅展示模型明确返回的决策依据，不展示隐藏推理。",
                },
                dark=True,
            )
        )
        decision_layout.addWidget(self.decision_edit)
        run_side_layout.addWidget(decision_card)
        info_card, info_layout = card("运行信息")
        info_grid = QGridLayout()
        self.used_steps_value = QLabel("0 / —")
        self.used_steps_value.setObjectName("metricValue")
        self.change_rate_value = QLabel("等待采样")
        self.change_rate_value.setObjectName("metricValue")
        self.decision_time_value = QLabel("— ms")
        self.decision_time_value.setObjectName("metricValue")
        self.location_source_value = QLabel("等待定位")
        self.location_source_value.setObjectName("metricValue")
        metrics = (
            ("已用 / 剩余步数", self.used_steps_value),
            ("界面变化率", self.change_rate_value),
            ("决策耗时", self.decision_time_value),
            ("定位来源", self.location_source_value),
        )
        for index, (label, value) in enumerate(metrics):
            metric = QWidget()
            metric_layout = QVBoxLayout(metric)
            metric_layout.setContentsMargins(0, 2, 0, 2)
            metric_layout.setSpacing(2)
            key = QLabel(label)
            key.setObjectName("metricLabel")
            metric_layout.addWidget(key)
            metric_layout.addWidget(value)
            info_grid.addWidget(metric, index // 2, index % 2)
        info_layout.addLayout(info_grid)
        run_side_layout.addWidget(info_card)
        event_card, event_layout = card("最近事件", "事件流 · 诊断")
        self.recent_events = QPlainTextEdit()
        self.recent_events.setObjectName("recentEvents")
        self.recent_events.setReadOnly(True)
        self.recent_events.setMaximumBlockCount(80)
        self.recent_events.setMinimumHeight(130)
        event_layout.addWidget(self.recent_events)
        run_side_layout.addWidget(event_card)
        run_side_layout.addStretch(1)
        run_splitter.addWidget(run_side_scroll)
        run_splitter.setSizes([700, 420])
        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("status")
        self.bottom_status_dot = QLabel("●")
        self.bottom_status_dot.setObjectName("statusDot")
        self.pause_button = QPushButton("暂停")
        self.stop_button = QPushButton("停止")
        self.stop_button.setObjectName("dangerButton")
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        run_status_bar = QFrame()
        run_status_bar.setObjectName("runStatusBar")
        run_status_layout = QHBoxLayout(run_status_bar)
        run_status_layout.setContentsMargins(14, 8, 10, 8)
        run_status_layout.addWidget(self.bottom_status_dot)
        run_status_layout.addWidget(self.status_label, 1)
        run_status_layout.addWidget(self.pause_button)
        run_status_layout.addWidget(self.stop_button)
        run_layout.addWidget(run_status_bar)
        self.page_stack.addWidget(run_page)

        # Page 3: latest run record. The existing log view remains the source.
        history_page = QWidget()
        history_page.setObjectName("historyPage")
        history_layout = QVBoxLayout(history_page)
        history_layout.setContentsMargins(28, 22, 28, 24)
        history_layout.setSpacing(14)
        history_header = QHBoxLayout()
        history_heading = page_header(
            "运行记录",
            "最近一次运行保存在 logs/gui_runs/latest.log，可打开原始记录进行完整诊断。",
        )
        history_header.addWidget(history_heading, 1)
        self.open_log_button = QPushButton("▣  打开日志")
        self.history_rerun_button = QPushButton("再运行一次")
        self.history_rerun_button.setObjectName("primaryButton")
        history_header.addWidget(self.open_log_button)
        history_header.addWidget(self.history_rerun_button)
        history_layout.addLayout(history_header)
        history_split = QSplitter(Qt.Horizontal)
        history_split.setObjectName("historySplitter")
        history_layout.addWidget(history_split, 1)
        history_list_card, history_list_layout = card("最近运行")
        self.history_latest_item = QLabel(
            "●  最近一次运行\n\n状态与步骤可在右侧原始日志中确认"
        )
        self.history_latest_item.setObjectName("historyItem")
        self.history_latest_item.setWordWrap(True)
        self.history_latest_item.setMinimumHeight(92)
        history_list_layout.addWidget(self.history_latest_item)
        history_list_layout.addStretch(1)
        history_split.addWidget(history_list_card)
        history_detail = QWidget()
        history_detail_layout = QVBoxLayout(history_detail)
        history_detail_layout.setContentsMargins(0, 0, 0, 0)
        history_detail_layout.setSpacing(12)
        history_summary = QLabel(
            "✓  最近运行摘要　日志持续自动保存；API Key 不会写入运行记录。"
        )
        history_summary.setObjectName("infoBarGood")
        history_summary.setWordWrap(True)
        history_detail_layout.addWidget(history_summary)
        log_card, log_layout = card("步骤回放与诊断", "latest.log")
        self.log_edit = QPlainTextEdit()
        self.log_edit.setObjectName("historyLog")
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumBlockCount(3000)
        log_layout.addWidget(self.log_edit)
        history_detail_layout.addWidget(log_card, 1)
        history_split.addWidget(history_detail)
        history_split.setSizes([340, 780])
        self.page_stack.addWidget(history_page)

        settings_page = QWidget()
        settings_layout = QVBoxLayout(settings_page)
        settings_layout.setContentsMargins(28, 22, 28, 24)
        settings_layout.addWidget(
            page_header("设置", "更多应用级设置将在后续版本中提供。")
        )
        settings_card, settings_card_layout = card("应用设置")
        settings_hint = QLabel(
            "当前版本保留系统原生标题栏与现有焦点策略。模型配置请从左侧导航进入。"
        )
        settings_hint.setObjectName("fieldHint")
        settings_hint.setWordWrap(True)
        settings_card_layout.addWidget(settings_hint)
        settings_layout.addWidget(settings_card)
        settings_layout.addStretch(1)
        self.page_stack.addWidget(settings_page)

        self.control_mode_combo = QComboBox(self)
        self.control_mode_combo.addItem("锁定单窗口", "locked_window")
        self.control_mode_combo.addItem("全屏多窗口", "full_desktop")
        self.control_mode_combo.hide()

        def set_model_expanded(expanded):
            self.model_config_body.setVisible(expanded)
            self.model_config_toggle.setText("⌃" if expanded else "⌄")

        def update_model_summary():
            profile = self.model_profile_combo.currentData() or "未命名方案"
            self.model_config_summary.setText(f"{profile} · API Key 已加密")

        def navigate(row):
            if row == 3:
                self.page_stack.setCurrentIndex(0)
                set_model_expanded(True)
                self.model_config_frame.setFocus()
                return
            if row == 4:
                self.page_stack.setCurrentIndex(3)
                return
            self.page_stack.setCurrentIndex(row)

        def sync_scope_cards():
            locked = self.current_control_mode() == "locked_window"
            self.locked_scope_button.setChecked(locked)
            self.desktop_scope_button.setChecked(not locked)

        def mirror_log():
            lines = self.log_edit.toPlainText().splitlines()[-12:]
            self.recent_events.setPlainText("\n".join(lines))
            log_text = self.log_edit.toPlainText()
            changes = re.findall(r"变化(?:率| )[^\d]*(\d+(?:\.\d+)?)%", log_text)
            if changes:
                self.change_rate_value.setText(f"{changes[-1]}%")
            sources = re.findall(r"定位来源：(.+)", log_text)
            if sources:
                self.location_source_value.setText(sources[-1][:28])

        def update_decision_metrics():
            text = self.decision_edit.toPlainText()
            duration = re.search(r"第\s*(\d+)\s*步\s*·\s*(\d+)\s*ms", text)
            if duration:
                self.decision_time_value.setText(f"{duration.group(2)} ms")
            source = re.search(r"定位来源\s*(.+)", text)
            if source:
                self.location_source_value.setText(source.group(1).strip()[:28])

        def update_selected_window_label(text):
            self.setup_preview.setProperty("pixmapKey", None)
            self.setup_preview.setText(f"▣  {text}" if text else "尚未选择目标窗口")
            self.target_badge.setText(
                f"目标窗口 · {text[:48]}" if text else "目标窗口 · 等待选择"
            )

        def sync_run_controls():
            self.run_top_pause_button.setEnabled(self.pause_button.isEnabled())
            self.run_top_pause_button.setText(self.pause_button.text())
            self.run_top_stop_button.setEnabled(self.stop_button.isEnabled())
            scope_enabled = self.control_mode_combo.isEnabled()
            self.locked_scope_button.setEnabled(scope_enabled)
            self.desktop_scope_button.setEnabled(scope_enabled)
            status = self.status_label.text()
            if self.run_status_badge.property("statusText") != status:
                self.run_status_badge.setProperty("statusText", status)
                self.run_status_badge.setText(f"● {status}")
            if "失败" in status:
                status_color = "#ff4d5e"
            elif "暂停" in status or "停止" in status:
                status_color = "#ffb020"
            elif "完成" in status:
                status_color = "#34d399"
            else:
                status_color = "#22d3ee"
            self.bottom_status_dot.setStyleSheet(f"color: {status_color};")
            self.status_label.setStyleSheet(f"color: {status_color};")
            self.stability_badge.setText(
                "● 界面已稳定" if "稳定" in status else "● 正在监督"
            )
            step_match = re.search(r"第\s*(\d+)/(\d+|∞)\s*步", status)
            if step_match:
                current = int(step_match.group(1))
                total_text = step_match.group(2)
                self.used_steps_value.setText(f"{current} / {total_text}")
                if total_text.isdigit():
                    self.run_progress.setValue(
                        min(100, round(current / max(1, int(total_text)) * 100))
                    )
            if self.worker:
                worker_identity = id(self.worker)
                if self.page_stack.property("activeWorkerId") != worker_identity:
                    self.page_stack.setProperty("activeWorkerId", worker_identity)
                    self.navigation_list.setCurrentRow(1)
            else:
                self.page_stack.setProperty("activeWorkerId", None)
            if self.last_pixmap and not self.last_pixmap.isNull():
                preview_key = (
                    self.last_pixmap.cacheKey(),
                    self.setup_preview.width(),
                    self.setup_preview.height(),
                )
                if self.setup_preview.property("pixmapKey") != preview_key:
                    self.setup_preview.setProperty("pixmapKey", preview_key)
                    self.setup_preview.setPixmap(
                        self.last_pixmap.scaled(
                            self.setup_preview.size(),
                            Qt.KeepAspectRatio,
                            Qt.SmoothTransformation,
                        )
                    )

        self.model_config_toggle.clicked.connect(
            lambda: set_model_expanded(not self.model_config_body.isVisible())
        )
        model_header.mousePressEvent = lambda event: set_model_expanded(
            not self.model_config_body.isVisible()
        )
        self.navigation_list.currentRowChanged.connect(navigate)
        self.navigation_list.setCurrentRow(0)
        self.locked_scope_button.clicked.connect(
            lambda: self.control_mode_combo.setCurrentIndex(0)
        )
        self.desktop_scope_button.clicked.connect(
            lambda: self.control_mode_combo.setCurrentIndex(1)
        )
        self.control_mode_combo.currentIndexChanged.connect(sync_scope_cards)
        self.run_model_button.clicked.connect(
            lambda: self.navigation_list.setCurrentRow(3)
        )
        self.run_back_button.clicked.connect(
            lambda: self.navigation_list.setCurrentRow(0)
        )
        self.run_top_pause_button.clicked.connect(self.pause_button.click)
        self.run_top_stop_button.clicked.connect(self.stop_button.click)
        self.history_rerun_button.clicked.connect(
            lambda: self.navigation_list.setCurrentRow(0)
        )
        self.window_combo.currentTextChanged.connect(update_selected_window_label)
        self.task_edit.textChanged.connect(
            lambda: self.run_task_summary.setText(
                "· " + (self.task_edit.toPlainText().strip()[:46] or "等待任务开始")
            )
        )
        self.model_profile_combo.currentIndexChanged.connect(update_model_summary)
        self.main_api_key_edit.textChanged.connect(update_model_summary)
        self.ground_api_key_edit.textChanged.connect(update_model_summary)
        self.log_edit.textChanged.connect(mirror_log)
        self.decision_edit.textChanged.connect(update_decision_metrics)
        self.ui_sync_timer = QTimer(self)
        self.ui_sync_timer.timeout.connect(sync_run_controls)
        self.ui_sync_timer.start(250)
        self.status_pulse_timer = QTimer(self)
        self.status_pulse_timer.setInterval(700)

        def pulse_status():
            pulsing = not bool(self.run_status_badge.property("pulse"))
            self.run_status_badge.setProperty("pulse", pulsing)
            status = self.run_status_badge.property("statusText") or "就绪"
            self.run_status_badge.setText(f"{'●' if pulsing else '◌'} {status}")

        self.status_pulse_timer.timeout.connect(pulse_status)
        self.status_pulse_timer.start()
        set_model_expanded(False)
        update_model_summary()
        if self.latest_log_path.is_file():
            try:
                self.log_edit.setPlainText(
                    self.latest_log_path.read_text(encoding="utf-8", errors="replace")
                )
                latest_text = self.log_edit.toPlainText()
                task_match = re.search(r"^task=(.+)$", latest_text, re.MULTILINE)
                if task_match:
                    self.history_latest_item.setText(
                        "●  最近一次运行\n\n" + task_match.group(1)[:90]
                    )
            except OSError:
                pass

        self.refresh_button.clicked.connect(self.refresh_windows)
        self.preview_button.clicked.connect(self.preview_selected_window)
        self.window_combo.currentIndexChanged.connect(self.preview_selected_window)
        self.control_mode_combo.currentIndexChanged.connect(self._control_mode_changed)
        self.start_button.clicked.connect(self.start_agent)
        self.pause_button.clicked.connect(self.toggle_pause)
        self.stop_button.clicked.connect(self.stop_agent)
        self.infinite_run_checkbox.toggled.connect(self._infinite_run_changed)
        self.open_log_button.clicked.connect(self.open_latest_log)
        self.model_profile_combo.currentIndexChanged.connect(
            self._model_profile_changed
        )
        self.save_model_profile_button.clicked.connect(self.save_current_model_profile)
        self.delete_model_profile_button.clicked.connect(
            self.delete_current_model_profile
        )
        self._infinite_run_changed(self.infinite_run_checkbox.isChecked())
        self._control_mode_changed()

    def _build_ui(self):
        """Build the single-screen supervision dashboard used by AEye."""
        self.setWindowIcon(LogoWidget.window_icon())
        root = QWidget()
        root.setObjectName("appShell")
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(16, 12, 16, 14)
        root_layout.setSpacing(12)

        def section_title(text, action=None):
            row = QHBoxLayout()
            row.setSpacing(8)
            label = QLabel(text)
            label.setObjectName("sectionTitle")
            row.addWidget(label)
            row.addStretch(1)
            if action is not None:
                row.addWidget(action)
            return row

        def panel(object_name="panel"):
            frame = QFrame()
            frame.setObjectName(object_name)
            return frame

        def decision_tile(icon, title, color):
            frame = panel("decisionTile")
            layout = QVBoxLayout(frame)
            layout.setContentsMargins(14, 12, 14, 12)
            layout.setSpacing(8)
            heading = QLabel(f"{icon}  {title}")
            heading.setObjectName("decisionTileTitle")
            heading.setStyleSheet(f"color: {color};")
            value = QLabel("等待任务开始…")
            value.setObjectName("decisionTileValue")
            value.setWordWrap(True)
            value.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            layout.addWidget(heading)
            layout.addWidget(value, 1)
            return frame, value

        # Top command bar.
        top_bar = panel("topBar")
        top_bar.setFixedHeight(62)
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(14, 7, 12, 7)
        top_layout.setSpacing(12)
        top_layout.addWidget(LogoWidget())
        product_copy = QVBoxLayout()
        product_copy.setSpacing(0)
        product_name = QLabel("Windows 视觉桌面智能体")
        product_name.setObjectName("productSubtitle")
        product_copy.addWidget(product_name)
        product_copy.addStretch(1)
        top_layout.addLayout(product_copy)
        top_layout.addStretch(1)
        self.status_dot = QLabel("●")
        self.status_dot.setObjectName("statusDot")
        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("headerStatus")
        self.settings_button = QPushButton("⚙  设置")
        self.settings_button.setObjectName("toolbarButton")
        top_layout.addWidget(self.status_dot)
        top_layout.addWidget(self.status_label)
        top_layout.addSpacing(18)
        top_layout.addWidget(self.settings_button)
        root_layout.addWidget(top_bar)

        # Three-column workbench.
        workbench = QHBoxLayout()
        workbench.setSpacing(12)
        root_layout.addLayout(workbench, 1)

        # Left: scope, model configuration, run settings and current task.
        left_column = QWidget()
        left_column.setObjectName("leftColumn")
        left_column.setMinimumWidth(290)
        left_column.setMaximumWidth(350)
        left_column_layout = QVBoxLayout(left_column)
        left_column_layout.setContentsMargins(0, 0, 0, 0)
        left_column_layout.setSpacing(9)
        left_scroll = QScrollArea()
        left_scroll.setObjectName("leftScroll")
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        left_panel = panel("leftPanel")
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(14, 14, 14, 14)
        left_layout.setSpacing(13)
        left_scroll.setWidget(left_panel)
        workbench.addWidget(left_column, 0)

        left_layout.addLayout(section_title("控制模式"))
        self.locked_scope_button = QPushButton(
            "▣   锁定单窗口\n      仅控制指定窗口"
        )
        self.desktop_scope_button = QPushButton(
            "▤   全屏多窗口\n      控制整个桌面"
        )
        for button in (self.locked_scope_button, self.desktop_scope_button):
            button.setObjectName("modeButton")
            button.setCheckable(True)
            button.setMinimumHeight(62)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            left_layout.addWidget(button)
        mode_group = QButtonGroup(self)
        mode_group.setExclusive(True)
        mode_group.addButton(self.locked_scope_button)
        mode_group.addButton(self.desktop_scope_button)
        self.locked_scope_button.setChecked(True)

        window_refresh = QPushButton("↻")
        window_refresh.setObjectName("flatIconButton")
        window_refresh.setToolTip("刷新窗口列表")
        left_layout.addLayout(section_title("目标窗口", window_refresh))
        window_row = QHBoxLayout()
        window_row.setSpacing(7)
        self.window_combo = QComboBox()
        self.window_combo.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon
        )
        self.refresh_button = window_refresh
        self.preview_button = QPushButton("预览")
        self.preview_button.setObjectName("compactButton")
        window_row.addWidget(self.window_combo, 1)
        window_row.addWidget(self.preview_button)
        left_layout.addLayout(window_row)
        self.window_meta_label = QLabel("等待选择可监控窗口")
        self.window_meta_label.setObjectName("mutedText")
        self.window_meta_label.setWordWrap(True)
        left_layout.addWidget(self.window_meta_label)

        model_manage = QPushButton("⚙  配置管理")
        model_manage.setObjectName("flatAction")
        left_layout.addLayout(section_title("模型配置", model_manage))
        profile_row = QHBoxLayout()
        profile_row.setSpacing(6)
        self.model_profile_combo = QComboBox()
        self.model_profile_combo.addItem("选择已保存方案…", None)
        self.save_model_profile_button = QPushButton("保存")
        self.save_model_profile_button.setObjectName("compactButton")
        self.delete_model_profile_button = QPushButton("删除")
        self.delete_model_profile_button.setObjectName("compactButton")
        profile_row.addWidget(self.model_profile_combo, 1)
        profile_row.addWidget(self.save_model_profile_button)
        profile_row.addWidget(self.delete_model_profile_button)
        left_layout.addLayout(profile_row)

        self.model_details = QWidget()
        model_form = QFormLayout(self.model_details)
        model_form.setContentsMargins(0, 0, 0, 0)
        model_form.setHorizontalSpacing(8)
        model_form.setVerticalSpacing(7)
        self.main_model_edit = QLineEdit(DEFAULT_MAIN_MODEL)
        self.main_url_edit = QLineEdit()
        self.main_api_key_edit = QLineEdit()
        self.main_api_key_edit.setEchoMode(QLineEdit.Password)
        self.ground_model_edit = QLineEdit()
        self.ground_url_edit = QLineEdit()
        self.ground_api_key_edit = QLineEdit()
        self.ground_api_key_edit.setEchoMode(QLineEdit.Password)
        self.main_model_edit.setPlaceholderText("主模型名称")
        self.main_url_edit.setPlaceholderText("https://…/v1")
        self.main_api_key_edit.setPlaceholderText("本地加密保存")
        self.ground_model_edit.setPlaceholderText("Grounding 模型")
        self.ground_url_edit.setPlaceholderText("https://…/v1")
        self.ground_api_key_edit.setPlaceholderText("本地加密保存")
        model_form.addRow("主模型", self.main_model_edit)
        model_form.addRow("URL", self.main_url_edit)
        model_form.addRow("Key", self.main_api_key_edit)
        model_form.addRow("Grounding", self.ground_model_edit)
        model_form.addRow("URL", self.ground_url_edit)
        model_form.addRow("Key", self.ground_api_key_edit)
        left_layout.addWidget(self.model_details)

        left_layout.addLayout(section_title("任务设置"))
        settings_grid = QGridLayout()
        settings_grid.setHorizontalSpacing(8)
        settings_grid.setVerticalSpacing(8)
        self.max_steps_spin = QSpinBox()
        self.max_steps_spin.setRange(1, 100)
        self.max_steps_spin.setValue(15)
        self.trajectory_spin = QSpinBox()
        self.trajectory_spin.setRange(1, 32)
        self.trajectory_spin.setValue(2)
        self.infinite_run_checkbox = QCheckBox("永久循环")
        self.infinite_run_checkbox.setObjectName("switchCheck")
        self.reflection_checkbox = QCheckBox("动作反思")
        self.reflection_checkbox.setObjectName("switchCheck")
        settings_grid.addWidget(QLabel("最大步数"), 0, 0)
        settings_grid.addWidget(self.max_steps_spin, 0, 1)
        settings_grid.addWidget(QLabel("历史轮数"), 1, 0)
        settings_grid.addWidget(self.trajectory_spin, 1, 1)
        settings_grid.addWidget(self.infinite_run_checkbox, 2, 0, 1, 2)
        settings_grid.addWidget(self.reflection_checkbox, 3, 0, 1, 2)
        left_layout.addLayout(settings_grid)

        left_layout.addLayout(section_title("当前任务"))
        self.task_edit = QTextEdit()
        self.task_edit.setObjectName("taskEdit")
        self.task_edit.setPlaceholderText("描述 AEye 要完成的任务…")
        self.task_edit.setMinimumHeight(84)
        self.task_edit.setMaximumHeight(118)
        left_layout.addWidget(self.task_edit)
        self.start_button = QPushButton("▶  开始任务")
        self.start_button.setObjectName("primaryButton")
        self.start_button.setMinimumHeight(42)
        left_layout.addStretch(1)
        left_column_layout.addWidget(left_scroll, 1)
        left_column_layout.addWidget(self.start_button)

        # Center: live capture and the current structured decision.
        center = QWidget()
        center.setObjectName("centerColumn")
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(12)
        workbench.addWidget(center, 1)

        capture_panel = panel("capturePanel")
        capture_layout = QVBoxLayout(capture_panel)
        capture_layout.setContentsMargins(14, 12, 14, 12)
        capture_layout.setSpacing(9)
        capture_head = QHBoxLayout()
        capture_title = QLabel("当前截图")
        capture_title.setObjectName("sectionTitle")
        self.capture_size_label = QLabel("等待捕获")
        self.capture_size_label.setObjectName("mutedText")
        self.capture_scope_label = QLabel("◉  锁定窗口")
        self.capture_scope_label.setObjectName("scopeBadge")
        capture_head.addWidget(capture_title)
        capture_head.addWidget(self.capture_size_label)
        capture_head.addStretch(1)
        capture_head.addWidget(self.capture_scope_label)
        capture_layout.addLayout(capture_head)
        self.preview_label = QLabel("选择目标窗口后点击“预览”")
        self.preview_label.setObjectName("preview")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumHeight(300)
        capture_layout.addWidget(self.preview_label, 1)
        self.capture_caption = QLabel("坐标空间：等待目标窗口 · 截图仅用于当前任务决策")
        self.capture_caption.setObjectName("captureCaption")
        capture_layout.addWidget(self.capture_caption)
        center_layout.addWidget(capture_panel, 5)

        decision_panel = panel("decisionPanel")
        decision_layout = QVBoxLayout(decision_panel)
        decision_layout.setContentsMargins(14, 11, 14, 11)
        decision_layout.setSpacing(9)
        decision_head = QHBoxLayout()
        decision_title = QLabel("当前决策")
        decision_title.setObjectName("sectionTitle")
        self.decision_step_label = QLabel("STEP —")
        self.decision_step_label.setObjectName("stepBadge")
        decision_head.addWidget(decision_title)
        decision_head.addWidget(self.decision_step_label)
        decision_head.addStretch(1)
        decision_layout.addLayout(decision_head)
        decision_row = QHBoxLayout()
        decision_row.setSpacing(8)
        observation_tile, self.observation_value = decision_tile("◉", "观察摘要", "#6ea8ff")
        goal_tile, self.goal_value = decision_tile("◎", "行为目标", "#42d392")
        reason_tile, self.reason_value = decision_tile("◇", "操作前确认", "#64d6a3")
        action_tile, self.action_value = decision_tile("→", "下一步行动", "#7ce0ff")
        for tile in (observation_tile, goal_tile, reason_tile, action_tile):
            decision_row.addWidget(tile, 1)
        decision_layout.addLayout(decision_row, 1)
        decision_meta = QHBoxLayout()
        self.location_value = QLabel("定位来源：等待定位")
        self.location_value.setObjectName("mutedText")
        self.confidence_value = QLabel("置信度：—")
        self.confidence_value.setObjectName("mutedText")
        decision_meta.addWidget(self.location_value)
        decision_meta.addStretch(1)
        decision_meta.addWidget(self.confidence_value)
        decision_layout.addLayout(decision_meta)
        self.decision_edit = QTextEdit()
        self.decision_edit.setReadOnly(True)
        self.decision_edit.hide()
        center_layout.addWidget(decision_panel, 2)

        run_controls = QHBoxLayout()
        run_controls.setSpacing(10)
        run_controls.addStretch(1)
        self.pause_button = QPushButton("暂停")
        self.stop_button = QPushButton("●  停止")
        self.stop_button.setObjectName("dangerButton")
        self.open_log_button = QPushButton("▱  打开日志")
        self.pause_button.setMinimumWidth(112)
        self.stop_button.setMinimumWidth(112)
        self.open_log_button.setMinimumWidth(130)
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        run_controls.addWidget(self.pause_button)
        run_controls.addWidget(self.stop_button)
        run_controls.addWidget(self.open_log_button)
        run_controls.addStretch(1)
        center_layout.addLayout(run_controls)

        # Right: chronological operational trace.
        timeline_panel = panel("timelinePanel")
        timeline_panel.setMinimumWidth(280)
        timeline_panel.setMaximumWidth(370)
        timeline_layout = QVBoxLayout(timeline_panel)
        timeline_layout.setContentsMargins(14, 14, 14, 14)
        timeline_layout.setSpacing(10)
        timeline_head = QHBoxLayout()
        timeline_title = QLabel("运行步骤")
        timeline_title.setObjectName("sectionTitle")
        self.timeline_open_log_button = QPushButton("查看日志")
        self.timeline_open_log_button.setObjectName("flatAction")
        timeline_head.addWidget(timeline_title)
        timeline_head.addStretch(1)
        timeline_head.addWidget(self.timeline_open_log_button)
        timeline_layout.addLayout(timeline_head)
        self.log_edit = QPlainTextEdit()
        self.log_edit.setObjectName("timelineLog")
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumBlockCount(3000)
        self.log_edit.setPlaceholderText("任务开始后，观察、定位、动作和验证事件会按时间显示在这里。")
        self.log_edit.setPlainText(
            "●  系统就绪\n"
            "   等待设置控制范围和任务目标\n\n"
            "○  目标窗口\n"
            "   等待选择\n\n"
            "○  模型连接\n"
            "   等待任务开始后验证"
        )
        timeline_layout.addWidget(self.log_edit, 1)
        safety_note = QLabel("●  人工控制始终有效\n暂停和停止不会等待下一轮模型决策。")
        safety_note.setObjectName("safetyNote")
        safety_note.setWordWrap(True)
        timeline_layout.addWidget(safety_note)
        workbench.addWidget(timeline_panel, 0)

        self.control_mode_combo = QComboBox(self)
        self.control_mode_combo.addItem("锁定单窗口", "locked_window")
        self.control_mode_combo.addItem("全屏多窗口", "full_desktop")
        self.control_mode_combo.hide()

        def sync_scope_cards():
            locked = self.current_control_mode() == "locked_window"
            self.locked_scope_button.setChecked(locked)
            self.desktop_scope_button.setChecked(not locked)
            self.capture_scope_label.setText(
                "◉  锁定窗口" if locked else "◉  全屏多窗口"
            )

        def update_window_meta(text):
            item = self.selected_window()
            if item is None:
                self.window_meta_label.setText("等待选择可监控窗口")
                return
            self.window_meta_label.setText(
                f"PID {item.process_id}  ·  HWND 0x{item.hwnd:X}  ·  {item.width}×{item.height}"
            )
            self.capture_size_label.setText(f"({item.width} × {item.height})")
            self.capture_caption.setText(
                f"坐标空间：{item.width} × {item.height} · {text[:48]}"
            )

        def toggle_model_details():
            self.model_details.setVisible(not self.model_details.isVisible())

        def update_status_tone(text):
            if "失败" in text or "错误" in text:
                color = "#ef6670"
            elif "暂停" in text or "停止" in text:
                color = "#f3b34f"
            elif "完成" in text or "稳定" in text:
                color = "#45d483"
            else:
                color = "#69a5ff"
            self.status_dot.setStyleSheet(f"color: {color};")
            self.status_label.setStyleSheet(f"color: {color}; font-weight: 700;")

        self.locked_scope_button.clicked.connect(
            lambda: self.control_mode_combo.setCurrentIndex(0)
        )
        self.desktop_scope_button.clicked.connect(
            lambda: self.control_mode_combo.setCurrentIndex(1)
        )
        self.control_mode_combo.currentIndexChanged.connect(sync_scope_cards)
        self.window_combo.currentTextChanged.connect(update_window_meta)
        model_manage.clicked.connect(toggle_model_details)
        self.settings_button.clicked.connect(toggle_model_details)
        self.refresh_button.clicked.connect(self.refresh_windows)
        self.preview_button.clicked.connect(self.preview_selected_window)
        self.window_combo.currentIndexChanged.connect(self.preview_selected_window)
        self.control_mode_combo.currentIndexChanged.connect(self._control_mode_changed)
        self.start_button.clicked.connect(self.start_agent)
        self.pause_button.clicked.connect(self.toggle_pause)
        self.stop_button.clicked.connect(self.stop_agent)
        self.infinite_run_checkbox.toggled.connect(self._infinite_run_changed)
        self.open_log_button.clicked.connect(self.open_latest_log)
        self.timeline_open_log_button.clicked.connect(self.open_latest_log)
        self.model_profile_combo.currentIndexChanged.connect(
            self._model_profile_changed
        )
        self.save_model_profile_button.clicked.connect(self.save_current_model_profile)
        self.delete_model_profile_button.clicked.connect(
            self.delete_current_model_profile
        )
        sync_scope_cards()
        update_status_tone(self.status_label.text())
        self._infinite_run_changed(self.infinite_run_checkbox.isChecked())
        self._control_mode_changed()

    def _infinite_run_changed(self, enabled: bool):
        self.max_steps_spin.setEnabled(not enabled and not bool(self.worker))

    def current_control_mode(self) -> str:
        return self.control_mode_combo.currentData() or "locked_window"

    def _load_model_profiles(self):
        try:
            if (
                not self.model_profiles_path.is_file()
                and legacy_model_profiles_path().is_file()
            ):
                legacy_profiles = load_model_profiles(legacy_model_profiles_path())
                save_model_profiles(self.model_profiles_path, legacy_profiles)
            self.model_profiles = load_model_profiles(self.model_profiles_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.model_profiles = {}
            self.status_label.setText(f"模型配置读取失败：{exc}")
        self._refresh_model_profile_combo()

    def _refresh_model_profile_combo(self, selected_name=None):
        self.model_profile_combo.blockSignals(True)
        self.model_profile_combo.clear()
        self.model_profile_combo.addItem("选择已保存方案…", None)
        for name in sorted(self.model_profiles, key=str.casefold):
            self.model_profile_combo.addItem(name, name)
        if selected_name in self.model_profiles:
            index = self.model_profile_combo.findData(selected_name)
            self.model_profile_combo.setCurrentIndex(index)
        self.model_profile_combo.blockSignals(False)
        self.delete_model_profile_button.setEnabled(
            self.model_profile_combo.currentData() is not None
        )

    def _model_profile_changed(self, *_):
        name = self.model_profile_combo.currentData()
        self.delete_model_profile_button.setEnabled(
            name is not None and not bool(self.worker)
        )
        if name is None:
            return
        profile = self.model_profiles.get(name)
        if not profile:
            return
        self.main_model_edit.setText(profile["main_model"])
        self.main_url_edit.setText(profile["main_url"])
        self.main_api_key_edit.setText(profile["main_api_key"])
        self.ground_model_edit.setText(profile["grounding_model"])
        self.ground_url_edit.setText(profile["grounding_url"])
        self.ground_api_key_edit.setText(profile["grounding_api_key"])
        self.status_label.setText(f"已加载模型配置：{name}")

    def _current_model_profile(self) -> dict:
        return normalize_model_profile(
            {
                "main_model": self.main_model_edit.text(),
                "main_url": self.main_url_edit.text(),
                "main_api_key": self.main_api_key_edit.text(),
                "grounding_model": self.ground_model_edit.text(),
                "grounding_url": self.ground_url_edit.text(),
                "grounding_api_key": self.ground_api_key_edit.text(),
            }
        )

    def save_current_model_profile(self):
        selected_name = self.model_profile_combo.currentData() or ""
        name, accepted = QInputDialog.getText(
            self,
            "保存模型配置",
            "配置名称：",
            text=selected_name,
        )
        name = name.strip()
        if not accepted or not name:
            return
        if name in self.model_profiles:
            answer = QMessageBox.question(
                self,
                "覆盖模型配置",
                f"配置“{name}”已存在，是否覆盖？",
            )
            if answer != QMessageBox.Yes:
                return
        updated_profiles = dict(self.model_profiles)
        updated_profiles[name] = self._current_model_profile()
        try:
            save_model_profiles(self.model_profiles_path, updated_profiles)
        except OSError as exc:
            QMessageBox.critical(self, "模型配置保存失败", str(exc))
            return
        self.model_profiles = updated_profiles
        self._refresh_model_profile_combo(name)
        self.status_label.setText(f"已保存模型配置：{name}（API Key 已在本地加密）")

    def delete_current_model_profile(self):
        name = self.model_profile_combo.currentData()
        if name is None:
            return
        answer = QMessageBox.question(
            self,
            "删除模型配置",
            f"确定删除配置“{name}”吗？",
        )
        if answer != QMessageBox.Yes:
            return
        updated_profiles = dict(self.model_profiles)
        updated_profiles.pop(name, None)
        try:
            save_model_profiles(self.model_profiles_path, updated_profiles)
        except OSError as exc:
            QMessageBox.critical(self, "模型配置删除失败", str(exc))
            return
        self.model_profiles = updated_profiles
        self._refresh_model_profile_combo()
        self.status_label.setText(f"已删除模型配置：{name}")

    def _control_mode_changed(self, *_):
        locked = self.current_control_mode() == "locked_window"
        idle = not bool(self.worker)
        self.window_combo.setEnabled(locked and idle)
        self.refresh_button.setEnabled(locked and idle)
        if not locked:
            self.status_label.setText("全屏多窗口模式：允许 Alt+Tab、任务栏和应用切换")
        self.preview_selected_window()

    def _apply_style_legacy(self):
        families = set(QFontDatabase.families())
        app_family = (
            "Noto Sans SC" if "Noto Sans SC" in families else "Microsoft YaHei UI"
        )
        mono_family = next(
            (
                family
                for family in ("Cascadia Code", "Cascadia Mono", "Consolas")
                if family in families
            ),
            "Consolas",
        )
        QApplication.instance().setFont(QFont(app_family, 9))
        self.log_edit.setFont(QFont(mono_family, 9))
        self.recent_events.setFont(QFont(mono_family, 8))
        self.setStyleSheet(
            f"""
            QMainWindow, QWidget#appShell, QWidget#setupPage, QWidget#runPage,
            QWidget#historyPage, QWidget#runSide, QStackedWidget#pageStack {{
                background: #0b0e13; color: #e8ecf4;
                font-family: "{app_family}";
            }}
            QWidget {{ color: #e8ecf4; font-family: "{app_family}"; }}
            QFrame#navigation {{
                background: #0d1117; border-right: 1px solid #232b38;
            }}
            QListWidget#navigationList {{
                background: transparent; border: none; outline: none; padding: 0;
            }}
            QListWidget#navigationList::item {{
                color: #a5aeba; border: none; border-radius: 5px;
                padding-left: 8px; margin: 2px 0;
            }}
            QListWidget#navigationList::item:hover {{
                background: #151a22; color: #e8ecf4;
            }}
            QListWidget#navigationList::item:selected {{
                background: #10303a; color: #7ce0ff;
                border-left: 2px solid #22d3ee;
            }}
            QLabel#navFooter {{ color: #566174; font-size: 10px; padding: 5px; }}
            QLabel#pageTitle {{ font-size: 24px; font-weight: 700; color: #e8ecf4; }}
            QLabel#subtitle {{ color: #a5aeba; font-size: 12px; }}
            QLabel#runTitle {{ font-size: 18px; font-weight: 700; color: #e8ecf4; }}
            QLabel#runSubtitle {{ color: #a5aeba; }}
            QFrame#card, QFrame#stageCard {{
                background: #151a22; border: 1px solid #232b38; border-radius: 8px;
            }}
            QLabel#cardTitle {{ font-size: 14px; font-weight: 700; color: #e8ecf4; }}
            QLabel#cardSubtitle, QLabel#fieldHint, QLabel#metricLabel,
            QLabel#captureCaption {{ color: #6d7789; font-size: 10px; }}
            QLabel#metricValue {{ color: #7ce0ff; font-size: 13px; font-weight: 700; }}
            QLineEdit, QComboBox, QTextEdit, QPlainTextEdit, QSpinBox {{
                background: #10141b; color: #e8ecf4; border: 1px solid #37424f;
                border-radius: 5px; padding: 7px; selection-background-color: #0891b2;
            }}
            QLineEdit:hover, QComboBox:hover, QTextEdit:hover,
            QPlainTextEdit:hover, QSpinBox:hover {{ border-color: #4a5868; }}
            QLineEdit:focus, QComboBox:focus, QTextEdit:focus,
            QPlainTextEdit:focus, QSpinBox:focus {{ border: 1px solid #22d3ee; }}
            QComboBox::drop-down {{ border: none; width: 24px; }}
            QComboBox QAbstractItemView {{
                background: #151a22; color: #e8ecf4; border: 1px solid #37424f;
                selection-background-color: #10303a;
            }}
            QPushButton {{
                background: #151a22; color: #e8ecf4; border: 1px solid #37424f;
                border-radius: 5px; padding: 7px 13px; font-weight: 600;
            }}
            QPushButton:hover {{ background: #1a212b; border-color: #526170; }}
            QPushButton:pressed {{ background: #232b38; }}
            QPushButton:disabled {{ color: #566174; background: #10141b; border-color: #232b38; }}
            QPushButton#primaryButton {{
                background: #22d3ee; color: #071117; border: 1px solid #22d3ee;
                font-weight: 800; padding-left: 20px; padding-right: 20px;
            }}
            QPushButton#primaryButton:hover {{ background: #0fd0ec; }}
            QPushButton#primaryButton:pressed {{ background: #0891b2; }}
            QPushButton#dangerButton {{
                background: #ff4d5e; color: white; border: 1px solid #ff4d5e;
                font-weight: 800;
            }}
            QPushButton#dangerButton:hover {{ background: #e73f51; }}
            QPushButton#iconButton {{ min-width: 18px; padding: 7px 9px; }}
            QPushButton#scopeCard {{
                text-align: left; padding: 12px; color: #a5aeba;
                background: #10141b; border: 1px solid #37424f;
            }}
            QPushButton#scopeCard:hover {{ background: #1a212b; }}
            QPushButton#scopeCard:checked {{
                color: #e8ecf4; background: #102731; border: 2px solid #22d3ee;
            }}
            QLabel#miniPreview {{
                background: #10141b; color: #a5aeba; border: 1px solid #2b3442;
                border-radius: 5px; padding: 12px;
            }}
            QCheckBox {{ color: #e8ecf4; spacing: 9px; }}
            QCheckBox::indicator {{
                width: 17px; height: 17px; border: 1px solid #526170;
                border-radius: 4px; background: #10141b;
            }}
            QCheckBox::indicator:checked {{ background: #22d3ee; border-color: #22d3ee; }}
            QFrame#expander {{
                background: #151a22; border: 1px solid #232b38; border-radius: 8px;
            }}
            QWidget#expanderHeader {{ background: transparent; border: none; }}
            QWidget#expanderHeader QLabel {{ font-weight: 700; }}
            QLabel#expanderSummary {{ color: #6d7789; font-size: 10px; font-weight: 400; }}
            QPushButton#expanderButton {{
                background: transparent; border: none; color: #a5aeba;
                min-width: 18px; padding: 2px;
            }}
            QWidget#expanderBody {{ background: #11161d; border-top: 1px solid #232b38; }}
            QLabel#infoBarGood {{
                background: #103229; color: #74e8bc; border: 1px solid #187a61;
                border-radius: 6px; padding: 12px;
            }}
            QLabel#statusBadge {{
                background: #10303a; color: #7ce0ff; border: 1px solid #155b69;
                border-radius: 12px; padding: 5px 10px; font-weight: 700;
            }}
            QLabel#stageBadge {{
                background: #102731; color: #7ce0ff; border: 1px solid #155b69;
                border-radius: 4px; padding: 5px 8px;
            }}
            QLabel#stableBadge {{
                background: #0f2d25; color: #34d399; border: 1px solid #17634f;
                border-radius: 4px; padding: 5px 8px;
            }}
            QLabel#preview {{
                background: #090c10; color: #6d7789; border: 1px solid #155b69;
                border-radius: 5px;
            }}
            QTextEdit#decisionView {{
                background: #10141b; border: 1px solid #2b3442;
                border-radius: 5px; padding: 10px;
            }}
            QPlainTextEdit#recentEvents, QPlainTextEdit#historyLog {{
                font-family: "{mono_family}"; background: #10141b;
                color: #a5aeba; border: 1px solid #232b38;
            }}
            QFrame#runStatusBar {{
                background: #11161d; border: 1px solid #232b38; border-radius: 6px;
            }}
            QLabel#status {{
                background: transparent; color: #7ce0ff; border: none;
                padding: 5px; font-weight: 700;
            }}
            QLabel#statusDot {{ color: #22d3ee; font-size: 15px; }}
            QProgressBar {{
                background: #232b38; border: none; border-radius: 2px; height: 4px;
            }}
            QProgressBar::chunk {{ background: #22d3ee; border-radius: 2px; }}
            QLabel#historyItem {{
                background: #102731; color: #e8ecf4; border: 1px solid #22d3ee;
                border-radius: 6px; padding: 14px;
            }}
            QScrollArea#pageScroll, QScrollArea#runSideScroll {{
                background: transparent; border: none;
            }}
            QScrollBar:vertical {{ background: #0b0e13; width: 9px; margin: 0; }}
            QScrollBar::handle:vertical {{
                background: #37424f; min-height: 26px; border-radius: 4px;
            }}
            QScrollBar::handle:vertical:hover {{ background: #526170; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QSplitter::handle {{ background: #0b0e13; }}
            QSplitter::handle:hover {{ background: #155b69; }}
            """
        )

    def _apply_style(self):
        families = set(QFontDatabase.families())
        app_family = (
            "Noto Sans SC" if "Noto Sans SC" in families else "Microsoft YaHei UI"
        )
        mono_family = next(
            (
                family
                for family in ("Cascadia Code", "Cascadia Mono", "Consolas")
                if family in families
            ),
            "Consolas",
        )
        QApplication.instance().setFont(QFont(app_family, 9))
        self.log_edit.setFont(QFont(mono_family, 8))
        self.setStyleSheet(
            f"""
            QMainWindow, QWidget#appShell {{
                background: #0b111c;
                color: #eef3fb;
                font-family: "{app_family}";
            }}
            QWidget {{ color: #eef3fb; font-family: "{app_family}"; }}
            QFrame#topBar {{
                background: #0c1320;
                border: 1px solid #131d2b;
                border-radius: 8px;
            }}
            QLabel#productSubtitle {{ color: #7d899b; font-size: 10px; }}
            QLabel#statusDot {{ color: #45d483; font-size: 15px; }}
            QLabel#headerStatus {{ color: #8ee7ad; font-weight: 700; }}
            QFrame#leftPanel, QFrame#capturePanel, QFrame#decisionPanel,
            QFrame#timelinePanel {{
                background: #101827;
                border: 1px solid #172235;
                border-radius: 8px;
            }}
            QWidget#centerColumn {{ background: transparent; }}
            QLabel#sectionTitle {{
                color: #f3f6fb;
                font-size: 13px;
                font-weight: 800;
            }}
            QLabel#mutedText, QLabel#captureCaption {{
                color: #758196;
                font-size: 10px;
            }}
            QLabel#scopeBadge {{
                color: #7ea8ff;
                background: #121f35;
                border: 1px solid #234474;
                border-radius: 10px;
                padding: 4px 9px;
            }}
            QLabel#stepBadge {{
                color: #a8b8cf;
                background: #131d2d;
                border: 1px solid #25344b;
                border-radius: 4px;
                padding: 3px 7px;
                font-family: "{mono_family}";
            }}
            QLineEdit, QComboBox, QTextEdit, QPlainTextEdit, QSpinBox {{
                background: #0c1320;
                color: #e8edf6;
                border: 1px solid #26364e;
                border-radius: 5px;
                padding: 6px 8px;
                selection-background-color: #2f78ed;
            }}
            QLineEdit:hover, QComboBox:hover, QTextEdit:hover,
            QPlainTextEdit:hover, QSpinBox:hover {{ border-color: #3b5274; }}
            QLineEdit:focus, QComboBox:focus, QTextEdit:focus,
            QPlainTextEdit:focus, QSpinBox:focus {{ border-color: #4285f4; }}
            QComboBox::drop-down {{ border: none; width: 24px; }}
            QComboBox QAbstractItemView {{
                background: #111a29;
                color: #e8edf6;
                border: 1px solid #26364e;
                selection-background-color: #1d3c68;
            }}
            QTextEdit#taskEdit {{
                background: #111a29;
                border-color: #26364e;
                padding: 9px;
            }}
            QPushButton {{
                background: #151f30;
                color: #e8edf6;
                border: 1px solid #27364c;
                border-radius: 5px;
                padding: 7px 12px;
                font-weight: 650;
            }}
            QPushButton:hover {{ background: #1a2940; border-color: #3b5274; }}
            QPushButton:pressed {{ background: #203451; }}
            QPushButton:disabled {{
                color: #536078;
                background: #101724;
                border-color: #1d293b;
            }}
            QPushButton#toolbarButton {{ background: #111a29; padding: 7px 12px; }}
            QPushButton#compactButton {{ padding: 6px 9px; }}
            QPushButton#flatIconButton, QPushButton#flatAction {{
                background: transparent;
                border: none;
                color: #a6b3c7;
                padding: 3px 5px;
            }}
            QPushButton#flatIconButton:hover, QPushButton#flatAction:hover {{
                color: #78a9ff;
                background: #162236;
            }}
            QPushButton#modeButton {{
                text-align: left;
                color: #a4afbf;
                background: #101827;
                border: 1px solid #27364b;
                padding: 9px 13px;
                line-height: 1.35;
            }}
            QPushButton#modeButton:hover {{ background: #142138; }}
            QPushButton#modeButton:checked {{
                color: #f2f6fd;
                background: #172942;
                border: 1px solid #3f7ee8;
            }}
            QPushButton#primaryButton {{
                color: white;
                background: #3478ee;
                border: 1px solid #4a8cff;
                font-weight: 800;
            }}
            QPushButton#primaryButton:hover {{ background: #4388fa; }}
            QPushButton#dangerButton {{
                color: white;
                background: #d9484f;
                border: 1px solid #ed6269;
                font-weight: 800;
            }}
            QPushButton#dangerButton:hover {{ background: #e3545b; }}
            QCheckBox {{ color: #c1cad8; spacing: 8px; }}
            QCheckBox::indicator {{
                width: 28px;
                height: 15px;
                border-radius: 8px;
                border: 1px solid #34445d;
                background: #182337;
            }}
            QCheckBox::indicator:checked {{
                background: #3478ee;
                border-color: #5a96ff;
            }}
            QLabel#preview {{
                background: #080d15;
                color: #657086;
                border: 1px solid #1c2b40;
                border-radius: 6px;
            }}
            QFrame#decisionTile {{
                background: #0d1522;
                border: 1px solid #1d2a3e;
                border-radius: 6px;
            }}
            QLabel#decisionTileTitle {{ font-size: 11px; font-weight: 800; }}
            QLabel#decisionTileValue {{
                color: #cbd4e2;
                font-size: 11px;
                line-height: 1.45;
            }}
            QPlainTextEdit#timelineLog {{
                background: transparent;
                color: #bac5d5;
                border: none;
                border-left: 1px solid #263750;
                border-radius: 0;
                padding: 7px 9px 7px 13px;
                font-family: "{mono_family}";
                selection-background-color: #254a7b;
            }}
            QLabel#safetyNote {{
                color: #74dca2;
                background: #0d251e;
                border: 1px solid #1e604b;
                border-radius: 6px;
                padding: 9px;
                font-size: 10px;
            }}
            QScrollArea#leftScroll {{ background: transparent; border: none; }}
            QScrollBar:vertical {{
                background: #0c1320;
                width: 8px;
                margin: 0;
            }}
            QScrollBar::handle:vertical {{
                background: #2a3b56;
                min-height: 30px;
                border-radius: 4px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
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
                controller = TargetWindowController.from_hwnd(info.hwnd)
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
        if not pixmap.isNull() and hasattr(self, "capture_size_label"):
            self.capture_size_label.setText(f"({pixmap.width()} × {pixmap.height()})")
        self._render_pixmap()

    def show_decision(self, details: dict):
        self.decision_edit.setHtml(format_decision_html(details))
        if hasattr(self, "observation_value"):
            self.observation_value.setText(
                str(details.get("observation") or "等待模型观察当前界面…")
            )
            self.goal_value.setText(
                str(details.get("goal") or "等待模型确定本步目标…")
            )
            self.reason_value.setText(
                str(details.get("reason") or details.get("plan") or "等待操作前确认…")
            )
            self.action_value.setText(
                str(details.get("action") or "等待下一步动作…")
            )
            step = details.get("step", "—")
            duration = details.get("decision_ms")
            duration_text = f" · {int(duration)} ms" if duration is not None else ""
            self.decision_step_label.setText(f"STEP {step}{duration_text}")
            grounding = details.get("grounding_info") or "等待定位"
            self.location_value.setText(f"定位来源：{grounding}")
            confidence = details.get("confidence")
            confidence_text = str(confidence) if confidence is not None else "—"
            self.confidence_value.setText(f"置信度：{confidence_text}")

    def show_overlay_decision(self, details: dict):
        self.decision_overlay.show_details(details)

    def show_overlay_status(self, details: dict):
        self.decision_overlay.show_status(details)

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
            f"reflection={config['enable_reflection']}\n"
            f"infinite_run={config['infinite_run']}\n"
            "api_keys=local-profile; encrypted at rest; not recorded in run log\n"
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
        model_profile = self._current_model_profile()
        if not model_profile["main_model"] or not model_profile["main_url"]:
            QMessageBox.warning(
                self,
                "主模型配置不完整",
                "请填写主模型名称和主模型 URL，或选择一个已保存的模型配置。",
            )
            return
        if not model_profile["main_api_key"]:
            QMessageBox.warning(
                self,
                "缺少主模型 API Key",
                "请输入主模型 API Key，或选择一个已经保存 Key 的模型配置。",
            )
            return
        if not model_profile["grounding_model"] or not model_profile["grounding_url"]:
            QMessageBox.warning(
                self,
                "Grounding 配置不完整",
                "请填写 Grounding 模型名称和 Grounding URL。",
            )
            return
        if not model_profile["grounding_api_key"]:
            QMessageBox.warning(
                self,
                "缺少 Grounding API Key",
                "请输入 Grounding API Key。",
            )
            return

        config = {
            "main_model": model_profile["main_model"],
            "main_url": model_profile["main_url"],
            "main_api_key": model_profile["main_api_key"],
            "grounding_model": model_profile["grounding_model"],
            "grounding_url": model_profile["grounding_url"],
            "grounding_api_key": model_profile["grounding_api_key"],
            "grounding_width": 1920,
            "grounding_height": 1080,
            "enable_reflection": self.reflection_checkbox.isChecked(),
            "max_steps": self.max_steps_spin.value(),
            "infinite_run": self.infinite_run_checkbox.isChecked(),
            "trajectory_length": self.trajectory_spin.value(),
            "control_mode": control_mode,
            "system_prompt_addendum": "",
            "max_image_dimension": 2400,
            "desktop_main_max_dimension": 1280,
            "action_delay": 1.0,
            "wait_delay": 2.0,
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
        waiting_details = {
            "state": "waiting",
            "observation": "正在等待第一张任务截图…",
            "goal": "等待主模型开始分析…",
            "reason": "下一动作即将执行时，小窗会显示该动作的完整决策。",
        }
        self.show_decision(waiting_details)
        self.decision_overlay.reset_for_run(waiting_details)
        self.append_log(f"任务：{task}")
        self.append_log(f"最近运行日志：{self.latest_log_path}")
        if control_mode == "locked_window":
            try:
                pin_controller = TargetWindowController.from_hwnd(info.hwnd)
                was_topmost = pin_controller.is_always_on_top()
                if not was_topmost:
                    pin_controller.set_always_on_top(True)
                self._pinned_target = (pin_controller, was_topmost)
                self.append_log(
                    "锁定窗口置顶：任务期间保持目标窗口始终置顶；结束后恢复原状态。"
                )
            except Exception as exc:
                self._pinned_target = None
                QMessageBox.critical(self, "窗口置顶失败", str(exc))
                return
        self.worker = AgentWorker(info, task, config)
        self.worker.log_message.connect(self.append_log)
        self.worker.screenshot_ready.connect(self.show_screenshot)
        self.worker.decision_update.connect(self.show_decision)
        self.worker.overlay_decision_update.connect(self.show_overlay_decision)
        self.worker.overlay_status_update.connect(self.show_overlay_status)
        self.worker.status_changed.connect(self.status_label.setText)
        self.worker.completed.connect(self._agent_completed)
        self.worker.failed.connect(self._agent_failed)
        self.worker.finished.connect(self._worker_finished)
        self._set_running(True)
        self._restore_after_run = True
        self.append_log(
            "运行界面：AEye 主窗口已最小化；左上角决策窗会实时显示模型判断。"
        )
        overlay_excluded = self.decision_overlay.show_at_top_left()
        if not overlay_excluded:
            self.append_log(
                "决策窗截图排除不可用：小窗仍保持鼠标穿透，但可能出现在全屏截图中。"
            )
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
            self._restore_main_window_after_run()

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
        self.decision_overlay.hide()
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
        self.model_profile_combo.setEnabled(not running)
        self.save_model_profile_button.setEnabled(not running)
        self.delete_model_profile_button.setEnabled(
            not running and self.model_profile_combo.currentData() is not None
        )
        self.main_model_edit.setEnabled(not running)
        self.main_url_edit.setEnabled(not running)
        self.main_api_key_edit.setEnabled(not running)
        self.ground_model_edit.setEnabled(not running)
        self.ground_url_edit.setEnabled(not running)
        self.ground_api_key_edit.setEnabled(not running)
        self.infinite_run_checkbox.setEnabled(not running)
        self.max_steps_spin.setEnabled(
            not running and not self.infinite_run_checkbox.isChecked()
        )
        self.reflection_checkbox.setEnabled(not running)
        self.pause_button.setEnabled(running)
        self.stop_button.setEnabled(running)
        self.pause_button.setText("暂停")

    def closeEvent(self, event: QCloseEvent):
        self.decision_overlay.close()
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
