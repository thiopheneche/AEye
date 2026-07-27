"""Windows target-window discovery, validation, and screenshot capture.

This module deliberately keeps target selection separate from Agent-S planning.
The selected HWND and process id form a boundary that is revalidated before
every observation and action.
"""

import ast
from dataclasses import dataclass
import platform
import time
from typing import List

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


def validate_target_window_action(plan_code: str, keyboard_only: bool = False):
    """Reject actions that intentionally escape a selected-window session."""
    try:
        expression = ast.parse(plan_code, mode="eval").body
        if not isinstance(expression, ast.Call) or not isinstance(
            expression.func, ast.Attribute
        ):
            raise ValueError
        action_name = expression.func.attr
    except (SyntaxError, ValueError):
        raise TargetWindowError("The model returned an invalid target-window action.")

    if action_name not in WINDOW_SAFE_ACTIONS:
        raise TargetWindowError(
            f'Action "{action_name}" is not allowed in target-window mode.'
        )

    if keyboard_only and action_name in {
        "click",
        "click_at",
        "type",
        "type_at",
        "drag_and_drop",
        "drag_at",
        "highlight_text_span",
        "scroll",
        "scroll_at",
    }:
        raise TargetWindowError(
            f'Action "{action_name}" is blocked because keyboard-only mode is enabled.'
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


class TargetWindowController:
    """Keep a selected HWND/PID stable while capturing and executing actions."""

    def __init__(self, title_query: str, background: bool = False):
        selected = find_target_window(title_query)
        self.hwnd = selected.hwnd
        self.process_id = selected.process_id
        self.initial_title = selected.title
        self.background = background
        self._last_input_hwnd = self.hwnd

    @classmethod
    def from_hwnd(cls, hwnd: int, background: bool = False):
        """Bind to an exact window selected by a graphical window picker."""
        selected = _read_window_info(hwnd)
        controller = cls.__new__(cls)
        controller.hwnd = selected.hwnd
        controller.process_id = selected.process_id
        controller.initial_title = selected.title
        controller.background = background
        controller._last_input_hwnd = controller.hwnd
        return controller

    def current_info(self) -> WindowInfo:
        info = _read_window_info(self.hwnd)
        if info.process_id != self.process_id:
            raise TargetWindowError(
                "Target HWND was reused by another process; stopping for safety."
            )
        return info

    def capture(self) -> tuple[Image.Image, WindowInfo]:
        """Capture the client area, optionally without reading desktop pixels."""
        info = self.current_info()
        if self.background:
            return self._capture_with_print_window(info), info
        bbox = (
            info.left,
            info.top,
            info.left + info.width,
            info.top + info.height,
        )
        image = ImageGrab.grab(bbox=bbox, all_screens=True)
        return image, info

    def _capture_with_print_window(self, info: WindowInfo) -> Image.Image:
        """Ask the application to paint its client area into an off-screen bitmap."""
        import ctypes

        import win32gui
        import win32ui

        window_dc_handle = win32gui.GetWindowDC(self.hwnd)
        window_dc = win32ui.CreateDCFromHandle(window_dc_handle)
        memory_dc = window_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        try:
            bitmap.CreateCompatibleBitmap(window_dc, info.width, info.height)
            memory_dc.SelectObject(bitmap)
            # PW_CLIENTONLY | PW_RENDERFULLCONTENT. The latter improves Chromium and
            # other compositor-backed applications on supported Windows versions.
            rendered = ctypes.windll.user32.PrintWindow(
                self.hwnd, memory_dc.GetSafeHdc(), 0x3
            )
            if rendered != 1:
                raise TargetWindowError(
                    "The target application does not support background capture."
                )
            bits = bitmap.GetBitmapBits(True)
            return Image.frombuffer(
                "RGB",
                (info.width, info.height),
                bits,
                "raw",
                "BGRX",
                0,
                1,
            ).copy()
        finally:
            memory_dc.DeleteDC()
            window_dc.DeleteDC()
            win32gui.ReleaseDC(self.hwnd, window_dc_handle)
            win32gui.DeleteObject(bitmap.GetHandle())

    def _input_target(self, x: int, y: int):
        """Find the deepest child window at a client-relative point."""
        _, win32gui, _ = _require_windows()
        target = self.hwnd
        point = (int(x), int(y))
        flags = 0x0001 | 0x0002 | 0x0004
        while True:
            try:
                child = win32gui.ChildWindowFromPointEx(target, point, flags)
            except Exception:
                break
            if not child or child == target:
                break
            screen_point = win32gui.ClientToScreen(target, point)
            point = win32gui.ScreenToClient(child, screen_point)
            target = child
        self._last_input_hwnd = target
        return target, point

    @staticmethod
    def _mouse_message(button: str):
        import win32con

        messages = {
            "left": (
                win32con.WM_LBUTTONDOWN,
                win32con.WM_LBUTTONUP,
                win32con.MK_LBUTTON,
            ),
            "right": (
                win32con.WM_RBUTTONDOWN,
                win32con.WM_RBUTTONUP,
                win32con.MK_RBUTTON,
            ),
            "middle": (
                win32con.WM_MBUTTONDOWN,
                win32con.WM_MBUTTONUP,
                win32con.MK_MBUTTON,
            ),
        }
        if button not in messages:
            raise TargetWindowError(f"Unsupported mouse button: {button}")
        return messages[button]

    def click(
        self, x: int, y: int, clicks: int = 1, button: str = "left", hold_keys=None
    ):
        """Send a client-relative click without moving the physical pointer."""
        import win32api
        import win32gui

        self.current_info()
        target, point = self._input_target(x, y)
        down, up, mask = self._mouse_message(button)
        hold_keys = hold_keys or []
        for key in hold_keys:
            self.keyDown(key)
        try:
            packed = win32api.MAKELONG(point[0], point[1])
            for _ in range(max(1, int(clicks))):
                win32gui.PostMessage(target, down, mask, packed)
                win32gui.PostMessage(target, up, 0, packed)
                time.sleep(0.08)
        finally:
            for key in reversed(hold_keys):
                self.keyUp(key)

    def drag(self, x1: int, y1: int, x2: int, y2: int, hold_keys=None):
        import win32api
        import win32con
        import win32gui

        target, start = self._input_target(x1, y1)
        end_screen = win32gui.ClientToScreen(self.hwnd, (int(x2), int(y2)))
        end = win32gui.ScreenToClient(target, end_screen)
        hold_keys = hold_keys or []
        for key in hold_keys:
            self.keyDown(key)
        try:
            win32gui.PostMessage(
                target,
                win32con.WM_LBUTTONDOWN,
                win32con.MK_LBUTTON,
                win32api.MAKELONG(*start),
            )
            for index in range(1, 11):
                x = round(start[0] + (end[0] - start[0]) * index / 10)
                y = round(start[1] + (end[1] - start[1]) * index / 10)
                win32gui.PostMessage(
                    target,
                    win32con.WM_MOUSEMOVE,
                    win32con.MK_LBUTTON,
                    win32api.MAKELONG(x, y),
                )
            win32gui.PostMessage(
                target, win32con.WM_LBUTTONUP, 0, win32api.MAKELONG(*end)
            )
        finally:
            for key in reversed(hold_keys):
                self.keyUp(key)

    def scroll(self, x: int, y: int, clicks: int, horizontal: bool = False):
        import win32api
        import win32con
        import win32gui

        target, _ = self._input_target(x, y)
        screen = win32gui.ClientToScreen(self.hwnd, (int(x), int(y)))
        message = win32con.WM_MOUSEHWHEEL if horizontal else win32con.WM_MOUSEWHEEL
        delta = int(clicks) * win32con.WHEEL_DELTA
        win32gui.PostMessage(
            target, message, (delta & 0xFFFF) << 16, win32api.MAKELONG(*screen)
        )

    @staticmethod
    def _vk(key: str) -> int:
        import win32api

        aliases = {
            "ctrl": "CONTROL",
            "return": "ENTER",
            "esc": "ESCAPE",
            "del": "DELETE",
        }
        name = aliases.get(str(key).casefold(), str(key).upper())
        named = {
            "CONTROL": 0x11,
            "SHIFT": 0x10,
            "ALT": 0x12,
            "ENTER": 0x0D,
            "BACKSPACE": 0x08,
            "TAB": 0x09,
            "ESCAPE": 0x1B,
            "DELETE": 0x2E,
            "SPACE": 0x20,
            "LEFT": 0x25,
            "UP": 0x26,
            "RIGHT": 0x27,
            "DOWN": 0x28,
            "HOME": 0x24,
            "END": 0x23,
            "PGUP": 0x21,
            "PGDN": 0x22,
        }
        if name in named:
            return named[name]
        if len(name) == 1:
            return win32api.VkKeyScan(name) & 0xFF
        if name.startswith("F") and name[1:].isdigit():
            return 0x70 + int(name[1:]) - 1
        raise TargetWindowError(f"Unsupported background key: {key}")

    def keyDown(self, key: str):
        import win32con
        import win32gui

        win32gui.PostMessage(
            self._last_input_hwnd, win32con.WM_KEYDOWN, self._vk(key), 0
        )

    def keyUp(self, key: str):
        import win32con
        import win32gui

        win32gui.PostMessage(self._last_input_hwnd, win32con.WM_KEYUP, self._vk(key), 0)

    def press(self, keys):
        if isinstance(keys, str):
            keys = [keys]
        for key in keys:
            self.keyDown(key)
            self.keyUp(key)

    def hotkey(self, *keys):
        for key in keys:
            self.keyDown(key)
        for key in reversed(keys):
            self.keyUp(key)

    def write(self, text: str):
        import win32con
        import win32gui

        for character in str(text):
            win32gui.PostMessage(
                self._last_input_hwnd, win32con.WM_CHAR, ord(character), 0
            )

    def ensure_foreground(self) -> WindowInfo:
        """Validate and focus the target immediately before global input."""
        win32con, win32gui, _ = _require_windows()
        info = self.current_info()
        if win32gui.GetForegroundWindow() != self.hwnd:
            win32gui.ShowWindow(self.hwnd, win32con.SW_RESTORE)
            try:
                win32gui.SetForegroundWindow(self.hwnd)
            except Exception as exc:
                raise TargetWindowError(
                    "Windows refused to focus the target window; no action was executed."
                ) from exc
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
