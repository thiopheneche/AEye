"""Windows target-window discovery, validation, and screenshot capture.

This module deliberately keeps target selection separate from Agent-S planning.
The selected HWND and process id form a boundary that is revalidated before
every observation and action.
"""

import ast
from dataclasses import dataclass
import platform
import re
import time
from typing import List
import unicodedata

from PIL import Image, ImageGrab


class TargetWindowError(RuntimeError):
    """Raised when a requested target window cannot be used safely."""


WINDOW_SAFE_ACTIONS = {
    "click",
    "click_at",
    "type",
    "type_at",
    "type_text",
    "drag_and_drop",
    "drag_at",
    "highlight_text_span",
    "scroll",
    "scroll_at",
    "hotkey",
    "hold_and_press",
    "press",
    "wait",
    "done",
    "fail",
    "save_to_knowledge",
}
DESKTOP_SAFE_ACTIONS = WINDOW_SAFE_ACTIONS | {"open", "switch_applications"}


def _parse_action_name(plan_code: str, mode_name: str) -> str:
    try:
        expression = ast.parse(plan_code, mode="eval").body
        if not isinstance(expression, ast.Call) or not isinstance(
            expression.func, ast.Attribute
        ):
            raise ValueError
        return expression.func.attr
    except (SyntaxError, ValueError):
        raise TargetWindowError(f"The model returned an invalid {mode_name} action.")


def validate_target_window_action(plan_code: str):
    """Reject actions that intentionally escape a selected-window session."""
    action_name = _parse_action_name(plan_code, "target-window")

    if action_name not in WINDOW_SAFE_ACTIONS:
        raise TargetWindowError(
            f'Action "{action_name}" is not allowed in target-window mode.'
        )

    lowered = plan_code.casefold()
    escaped_shortcuts = (
        "'alt', 'tab'",
        '"alt", "tab"',
        "'win'",
        '"win"',
        "'command', 'tab'",
        '"command", "tab"',
    )
    if any(shortcut in lowered for shortcut in escaped_shortcuts):
        raise TargetWindowError(
            "Application-switching shortcuts are blocked in target-window mode."
        )


def validate_desktop_action(plan_code: str):
    """Validate an action for full-desktop mode while allowing app switching."""
    action_name = _parse_action_name(plan_code, "full-desktop")
    if action_name not in DESKTOP_SAFE_ACTIONS:
        raise TargetWindowError(
            f'Action "{action_name}" is not allowed in full-desktop mode.'
        )


def map_grounding_coordinates(
    coordinates,
    width: int,
    height: int,
    grounding_width: int,
    grounding_height: int,
    offset_x: int = 0,
    offset_y: int = 0,
):
    """Map grounding-model coordinates into a desktop or window client area."""
    if min(width, height, grounding_width, grounding_height) <= 0:
        raise ValueError("Coordinate space dimensions must be positive.")
    return [
        offset_x + round(coordinates[0] * width / grounding_width),
        offset_y + round(coordinates[1] * height / grounding_height),
    ]


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    process_id: int
    title: str
    left: int
    top: int
    width: int
    height: int
    minimized: bool = False


class DesktopController:
    """Capture and control the entire Windows virtual desktop."""

    def current_info(self) -> WindowInfo:
        _require_windows()
        import win32api

        left = win32api.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
        top = win32api.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
        width = win32api.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
        height = win32api.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
        if width <= 1 or height <= 1:
            raise TargetWindowError("The virtual desktop has an invalid capture area.")
        return WindowInfo(
            hwnd=0,
            process_id=0,
            title="完整桌面",
            left=left,
            top=top,
            width=width,
            height=height,
        )

    def capture(self) -> tuple[Image.Image, WindowInfo]:
        info = self.current_info()
        bbox = (
            info.left,
            info.top,
            info.left + info.width,
            info.top + info.height,
        )
        return ImageGrab.grab(bbox=bbox, all_screens=True), info


def _require_windows():
    if platform.system() != "Windows":
        raise TargetWindowError(
            "Target-window mode is currently supported on Windows only."
        )

    import win32con
    import win32gui
    import win32process

    return win32con, win32gui, win32process


def _read_window_info(hwnd: int, allow_minimized: bool = False) -> WindowInfo:
    win32con, win32gui, win32process = _require_windows()

    if not win32gui.IsWindow(hwnd):
        raise TargetWindowError(f"Target window no longer exists (HWND={hwnd}).")
    if not win32gui.IsWindowVisible(hwnd):
        raise TargetWindowError("Target window is no longer visible.")
    minimized = bool(win32gui.IsIconic(hwnd))
    if minimized and not allow_minimized:
        raise TargetWindowError(
            "Target window is minimized; restore it before continuing."
        )

    title = win32gui.GetWindowText(hwnd).strip()
    if not title:
        raise TargetWindowError("Target window no longer has a visible title.")

    client_left, client_top = win32gui.ClientToScreen(hwnd, (0, 0))
    client_rect = win32gui.GetClientRect(hwnd)
    width = client_rect[2] - client_rect[0]
    height = client_rect[3] - client_rect[1]
    if minimized and allow_minimized and (width <= 1 or height <= 1):
        import ctypes
        from ctypes import wintypes

        normal_rect = win32gui.GetWindowPlacement(hwnd)[4]
        outer_width = normal_rect[2] - normal_rect[0]
        outer_height = normal_rect[3] - normal_rect[1]
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
        ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        adjusted = wintypes.RECT()
        if not ctypes.windll.user32.AdjustWindowRectEx(
            ctypes.byref(adjusted), style, False, ex_style
        ):
            raise TargetWindowError("Could not recover the minimized client size.")
        width = outer_width - (adjusted.right - adjusted.left)
        height = outer_height - (adjusted.bottom - adjusted.top)
    if width <= 1 or height <= 1:
        raise TargetWindowError("Target window has an invalid client area.")

    _, process_id = win32process.GetWindowThreadProcessId(hwnd)
    return WindowInfo(
        hwnd=hwnd,
        process_id=process_id,
        title=title,
        left=client_left,
        top=client_top,
        width=width,
        height=height,
        minimized=minimized,
    )


def list_target_windows(include_minimized: bool = False) -> List[WindowInfo]:
    """Return visible, titled, non-minimized top-level Windows windows."""
    _, win32gui, _ = _require_windows()
    windows: List[WindowInfo] = []

    def collect(hwnd, _):
        try:
            windows.append(_read_window_info(hwnd, allow_minimized=include_minimized))
        except TargetWindowError:
            pass
        return True

    win32gui.EnumWindows(collect, None)
    return sorted(windows, key=lambda item: item.title.casefold())


def find_target_window(title_query: str) -> WindowInfo:
    """Resolve a title query, preferring a single case-insensitive exact match."""
    query = title_query.strip().casefold()
    if not query:
        raise TargetWindowError("Window title query cannot be empty.")

    windows = list_target_windows()
    exact = [item for item in windows if item.title.casefold() == query]
    if len(exact) == 1:
        return exact[0]

    partial = [item for item in windows if query in item.title.casefold()]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise TargetWindowError(f'No visible window matched "{title_query}".')

    titles = "\n".join(f"  - {item.title}" for item in partial[:10])
    raise TargetWindowError(
        f'Multiple windows matched "{title_query}". Use a more specific title:\n{titles}'
    )


def _desktop_window_query_terms(title_query: str):
    query = title_query.strip().casefold()
    aliases = (
        {"wechat", "weixin", "微信"},
        {"notepad", "记事本"},
        {"file explorer", "explorer", "文件资源管理器"},
    )
    terms = {query}
    for alias_group in aliases:
        if query in alias_group:
            terms.update(alias_group)
    return {term for term in terms if term}


def find_desktop_window(title_query: str) -> WindowInfo:
    """Resolve an already-open desktop window, including common localized aliases."""
    terms = _desktop_window_query_terms(title_query)
    if not terms:
        raise TargetWindowError("Application window query cannot be empty.")

    candidates = []
    for info in list_target_windows(include_minimized=True):
        title = info.title.casefold()
        exact = any(title == term for term in terms)
        partial = any(term in title or title in term for term in terms)
        if exact or partial:
            candidates.append(
                (
                    0 if exact else 1,
                    1 if info.minimized else 0,
                    -(info.width * info.height),
                    info,
                )
            )
    if not candidates:
        raise TargetWindowError(
            f'No already-open window matched application "{title_query}".'
        )
    candidates.sort(key=lambda item: item[:3])
    return candidates[0][3]


def get_foreground_window_info():
    """Return the current foreground top-level window when it can be inspected."""
    _, win32gui, _ = _require_windows()
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return None
    get_ancestor = getattr(win32gui, "GetAncestor", None)
    if get_ancestor is not None:
        hwnd = get_ancestor(hwnd, 2) or hwnd  # GA_ROOT
    try:
        return _read_window_info(hwnd, allow_minimized=True)
    except TargetWindowError:
        return None


def _request_window_activation(hwnd: int):
    """Ask Windows for foreground focus, with a thread-attachment fallback."""
    win32con, win32gui, win32process = _require_windows()
    import win32api

    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    if win32gui.GetForegroundWindow() == hwnd:
        return

    alt_pressed = False
    try:
        # The Alt gesture is the least invasive way to satisfy Windows' normal
        # foreground-lock policy, so try it before attaching input queues.
        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
        alt_pressed = True
        win32gui.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    finally:
        if alt_pressed:
            win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
    if win32gui.GetForegroundWindow() == hwnd:
        return

    current_thread = win32api.GetCurrentThreadId()
    target_thread, _ = win32process.GetWindowThreadProcessId(hwnd)
    foreground_hwnd = win32gui.GetForegroundWindow()
    foreground_thread = 0
    if foreground_hwnd:
        foreground_thread, _ = win32process.GetWindowThreadProcessId(foreground_hwnd)

    attached_threads = []
    try:
        for source_thread in {current_thread, foreground_thread}:
            if source_thread and source_thread != target_thread:
                try:
                    win32process.AttachThreadInput(source_thread, target_thread, True)
                except Exception:
                    continue
                attached_threads.append(source_thread)
        win32gui.BringWindowToTop(hwnd)
        set_active = getattr(win32gui, "SetActiveWindow", None)
        if set_active is not None:
            set_active(hwnd)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    finally:
        for source_thread in reversed(attached_threads):
            try:
                win32process.AttachThreadInput(source_thread, target_thread, False)
            except Exception:
                pass


def match_desktop_window_description(description: str):
    """Match a shell-icon description against the titles of all open windows."""
    normalized_description = unicodedata.normalize("NFKC", description).casefold()
    compact_description = "".join(
        character for character in normalized_description if character.isalnum()
    )
    candidates = []
    for info in list_target_windows(include_minimized=True):
        normalized_title = unicodedata.normalize("NFKC", info.title).casefold()
        title_parts = [normalized_title]
        title_parts.extend(
            part.strip()
            for part in re.split(r"\s[-–—|·]\s|[-–—|·]", normalized_title)
            if part.strip()
        )
        labels = set(title_parts)
        labels.update(_desktop_window_query_terms(normalized_title))
        best_score = 0
        for label in labels:
            compact_label = "".join(
                character for character in label if character.isalnum()
            )
            minimum_length = 2 if re.search(r"[\u4e00-\u9fff]", label) else 3
            if (
                len(compact_label) >= minimum_length
                and compact_label in compact_description
            ):
                best_score = max(best_score, len(compact_label))
        if best_score:
            candidates.append(
                (
                    -best_score,
                    1 if info.minimized else 0,
                    -(info.width * info.height),
                    info,
                )
            )
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[:3])
    return candidates[0][3]


def activate_desktop_window(title_query: str) -> WindowInfo:
    """Restore and focus an existing window without launching another instance."""
    _, win32gui, _ = _require_windows()

    selected = find_desktop_window(title_query)
    _request_window_activation(selected.hwnd)

    for _ in range(20):
        if win32gui.GetForegroundWindow() == selected.hwnd:
            return _read_window_info(selected.hwnd, allow_minimized=True)
        time.sleep(0.05)
    raise TargetWindowError(
        f'Windows did not activate the existing window "{selected.title}".'
    )


def open_desktop_application(title_query: str):
    """Activate an existing app, otherwise launch it through Unicode-safe search."""
    try:
        find_desktop_window(title_query)
    except TargetWindowError:
        import pyautogui
        import pyperclip

        previous_clipboard = None
        try:
            previous_clipboard = pyperclip.paste()
        except Exception:
            pass
        pyautogui.hotkey("win")
        time.sleep(0.5)
        pyperclip.copy(title_query)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(1.0)
        pyautogui.press("enter")
        time.sleep(0.5)
        if previous_clipboard is not None:
            try:
                pyperclip.copy(previous_clipboard)
            except Exception:
                pass
        return None
    return activate_desktop_window(title_query)


def describe_screen_point(x: int, y: int) -> dict:
    """Return the root window currently occupying a physical screen coordinate."""
    _, win32gui, win32process = _require_windows()
    child_hwnd = win32gui.WindowFromPoint((int(x), int(y)))
    root_hwnd = 0
    if child_hwnd:
        get_ancestor = getattr(win32gui, "GetAncestor", None)
        if get_ancestor is not None:
            root_hwnd = get_ancestor(child_hwnd, 2)  # GA_ROOT
        else:
            # Some pywin32 builds do not expose GetAncestor. Walking GetParent
            # provides the same top-level target needed by this diagnostic.
            root_hwnd = child_hwnd
            visited = {int(child_hwnd)}
            while True:
                parent_hwnd = win32gui.GetParent(root_hwnd)
                if not parent_hwnd or int(parent_hwnd) in visited:
                    break
                root_hwnd = parent_hwnd
                visited.add(int(root_hwnd))
    root_hwnd = root_hwnd or child_hwnd
    process_id = 0
    if root_hwnd:
        _, process_id = win32process.GetWindowThreadProcessId(root_hwnd)
    return {
        "x": int(x),
        "y": int(y),
        "child_hwnd": int(child_hwnd or 0),
        "root_hwnd": int(root_hwnd or 0),
        "pid": int(process_id),
        "title": win32gui.GetWindowText(root_hwnd).strip() if root_hwnd else "",
    }


class TargetWindowController:
    """Keep a selected HWND/PID stable while capturing and executing actions."""

    def __init__(self, title_query: str):
        selected = find_target_window(title_query)
        self.hwnd = selected.hwnd
        self.process_id = selected.process_id
        self.initial_title = selected.title

    @classmethod
    def from_hwnd(cls, hwnd: int):
        """Bind to an exact window selected by a graphical window picker."""
        selected = _read_window_info(hwnd)
        controller = cls.__new__(cls)
        controller.hwnd = selected.hwnd
        controller.process_id = selected.process_id
        controller.initial_title = selected.title
        return controller

    def current_info(self) -> WindowInfo:
        info = _read_window_info(self.hwnd)
        if info.process_id != self.process_id:
            raise TargetWindowError(
                "Target HWND was reused by another process; stopping for safety."
            )
        return info

    def is_always_on_top(self) -> bool:
        """Return whether the selected top-level window already has TOPMOST state."""
        win32con, win32gui, _ = _require_windows()
        self.current_info()
        ex_style = win32gui.GetWindowLong(self.hwnd, win32con.GWL_EXSTYLE)
        return bool(ex_style & win32con.WS_EX_TOPMOST)

    def set_always_on_top(self, enabled: bool) -> WindowInfo:
        """Set or clear TOPMOST without moving, resizing, or activating the window."""
        win32con, win32gui, _ = _require_windows()
        self.current_info()
        insert_after = win32con.HWND_TOPMOST if enabled else win32con.HWND_NOTOPMOST
        flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE
        win32gui.SetWindowPos(self.hwnd, insert_after, 0, 0, 0, 0, flags)
        return self.current_info()

    def capture(self) -> tuple[Image.Image, WindowInfo]:
        """Capture the selected window's visible client area."""
        info = self.current_info()
        bbox = (
            info.left,
            info.top,
            info.left + info.width,
            info.top + info.height,
        )
        image = ImageGrab.grab(bbox=bbox, all_screens=True)
        return image, info

    def ensure_foreground(self) -> WindowInfo:
        """Validate and focus the target immediately before global input."""
        _, win32gui, _ = _require_windows()
        info = self.current_info()
        if win32gui.GetForegroundWindow() != self.hwnd:
            _request_window_activation(self.hwnd)
        for _ in range(10):
            if win32gui.GetForegroundWindow() == self.hwnd:
                break
            time.sleep(0.05)
        if win32gui.GetForegroundWindow() != self.hwnd:
            raise TargetWindowError(
                "The target window did not acquire foreground focus; no keyboard or "
                "mouse input was executed."
            )
        return self.current_info()
