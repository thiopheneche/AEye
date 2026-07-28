import argparse
import datetime
import io
import logging
import os
import platform
import signal
import sys
import time

from gui_agents.s3.utils.window_target import (
    TargetWindowController,
    TargetWindowError,
    list_target_windows,
    validate_target_window_action,
)

current_platform = platform.system().lower()

# Global flag to track pause state for debugging
paused = False


def get_char():
    """Get a single character from stdin without pressing Enter"""
    try:
        # Import termios and tty on Unix-like systems
        if platform.system() in ["Darwin", "Linux"]:
            import termios
            import tty

            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setraw(sys.stdin.fileno())
                ch = sys.stdin.read(1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            return ch
        else:
            # Windows fallback
            import msvcrt

            return msvcrt.getch().decode("utf-8", errors="ignore")
    except:
        return input()  # Fallback for non-terminal environments


def signal_handler(signum, frame):
    """Handle Ctrl+C signal for debugging during agent execution"""
    global paused

    if not paused:
        print("\n\n🔸 Agent-S Workflow Paused 🔸")
        print("=" * 50)
        print("Options:")
        print("  • Press Ctrl+C again to quit")
        print("  • Press Esc to resume workflow")
        print("=" * 50)

        paused = True

        while paused:
            try:
                print("\n[PAUSED] Waiting for input... ", end="", flush=True)
                char = get_char()

                if ord(char) == 3:  # Ctrl+C
                    print("\n\n🛑 Exiting Agent-S...")
                    sys.exit(0)
                elif ord(char) == 27:  # Esc
                    print("\n\n▶️  Resuming Agent-S workflow...")
                    paused = False
                    break
                else:
                    print(f"\n   Unknown command: '{char}' (ord: {ord(char)})")

            except KeyboardInterrupt:
                print("\n\n🛑 Exiting Agent-S...")
                sys.exit(0)
    else:
        # Already paused, second Ctrl+C means quit
        print("\n\n🛑 Exiting Agent-S...")
        sys.exit(0)


# Set up signal handler for Ctrl+C
signal.signal(signal.SIGINT, signal_handler)

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

datetime_str: str = datetime.datetime.now().strftime("%Y%m%d@%H%M%S")

log_dir = "logs"
os.makedirs(log_dir, exist_ok=True)

file_handler = logging.FileHandler(
    os.path.join("logs", "normal-{:}.log".format(datetime_str)), encoding="utf-8"
)
debug_handler = logging.FileHandler(
    os.path.join("logs", "debug-{:}.log".format(datetime_str)), encoding="utf-8"
)
stdout_handler = logging.StreamHandler(sys.stdout)
sdebug_handler = logging.FileHandler(
    os.path.join("logs", "sdebug-{:}.log".format(datetime_str)), encoding="utf-8"
)

file_handler.setLevel(logging.INFO)
debug_handler.setLevel(logging.DEBUG)
stdout_handler.setLevel(logging.INFO)
sdebug_handler.setLevel(logging.DEBUG)

formatter = logging.Formatter(
    fmt="\x1b[1;33m[%(asctime)s \x1b[31m%(levelname)s \x1b[32m%(module)s/%(lineno)d-%(processName)s\x1b[1;33m] \x1b[0m%(message)s"
)
file_handler.setFormatter(formatter)
debug_handler.setFormatter(formatter)
stdout_handler.setFormatter(formatter)
sdebug_handler.setFormatter(formatter)

stdout_handler.addFilter(logging.Filter("desktopenv"))
sdebug_handler.addFilter(logging.Filter("desktopenv"))

logger.addHandler(file_handler)
logger.addHandler(debug_handler)
logger.addHandler(stdout_handler)
logger.addHandler(sdebug_handler)

platform_os = platform.system()


def show_permission_dialog(code: str, action_description: str):
    """Show a platform-specific permission dialog and return True if approved."""
    if platform.system() == "Darwin":
        result = os.system(
            f'osascript -e \'display dialog "Do you want to execute this action?\n\n{code} which will try to {action_description}" with title "Action Permission" buttons {{"Cancel", "OK"}} default button "OK" cancel button "Cancel"\''
        )
        return result == 0
    elif platform.system() == "Linux":
        result = os.system(
            f'zenity --question --title="Action Permission" --text="Do you want to execute this action?\n\n{code}" --width=400 --height=200'
        )
        return result == 0
    return False


def scale_screen_dimensions(width: int, height: int, max_dim_size: int):
    scale_factor = min(max_dim_size / width, max_dim_size / height, 1)
    safe_width = int(width * scale_factor)
    safe_height = int(height * scale_factor)
    return safe_width, safe_height


def run_agent(agent, instruction: str, target_window=None):
    import pyautogui
    from PIL import Image

    global paused
    obs = {}
    traj = "Task:\n" + instruction
    subtask_traj = ""
    for step in range(15):
        # Check if we're in paused state and wait
        while paused:
            time.sleep(0.1)
        # Capture either the whole desktop or only the user-authorized window.
        if target_window is not None:
            screenshot, window_info = target_window.capture()
            capture_width, capture_height = window_info.width, window_info.height
            agent.grounding_agent.set_coordinate_space(
                capture_width,
                capture_height,
                offset_x=window_info.left,
                offset_y=window_info.top,
            )
        else:
            screenshot = pyautogui.screenshot()
            capture_width, capture_height = screenshot.size
            agent.grounding_agent.set_coordinate_space(capture_width, capture_height)

        scaled_width, scaled_height = scale_screen_dimensions(
            capture_width, capture_height, max_dim_size=2400
        )
        screenshot = screenshot.resize((scaled_width, scaled_height), Image.LANCZOS)
        agent.grounding_agent.set_grounding_image_size(scaled_width, scaled_height)

        # Save the screenshot to a BytesIO object
        buffered = io.BytesIO()
        screenshot.save(buffered, format="PNG")

        # Get the byte value of the screenshot
        screenshot_bytes = buffered.getvalue()
        # Convert to base64 string.
        obs["screenshot"] = screenshot_bytes

        # Check again for pause state before prediction
        while paused:
            time.sleep(0.1)

        print(f"\n🔄 Step {step + 1}/15: Getting next action from agent...")

        # Get next action code from the agent
        info, code = agent.predict(instruction=instruction, observation=obs)

        if "done" in code[0].lower() or "fail" in code[0].lower():
            if platform.system() == "Darwin":
                os.system(
                    f'osascript -e \'display dialog "Task Completed" with title "OpenACI Agent" buttons "OK" default button "OK"\''
                )
            elif platform.system() == "Linux":
                os.system(
                    f'zenity --info --title="OpenACI Agent" --text="Task Completed" --width=200 --height=100'
                )

            break

        if "next" in code[0].lower():
            continue

        if "wait" in code[0].lower():
            print("⏳ Agent requested wait...")
            time.sleep(5)
            continue

        else:
            time.sleep(1.0)
            print("EXECUTING CODE:", code[0])

            # Check for pause state before execution
            while paused:
                time.sleep(0.1)

            if target_window is not None:
                validate_target_window_action(info.get("plan_code", ""))
                target_window.ensure_foreground()

            exec(code[0])
            time.sleep(1.0)

            # Update task and subtask trajectories
            if "reflection" in info and "executor_plan" in info:
                traj += (
                    "\n\nReflection:\n"
                    + str(info["reflection"])
                    + "\n\n----------------------\n\nPlan:\n"
                    + info["executor_plan"]
                )


def main():
    parser = argparse.ArgumentParser(description="Run AgentS3 with specified model.")
    parser.add_argument(
        "--provider",
        type=str,
        default="openai",
        help="Specify the provider to use (e.g., openai, anthropic, etc.)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="gpt-5.5",
        help="Specify the model to use (e.g., gpt-5.5)",
    )
    parser.add_argument(
        "--model_url",
        type=str,
        default="",
        help="The URL of the main generation model API.",
    )
    parser.add_argument(
        "--model_api_key",
        type=str,
        default="",
        help="The API key of the main generation model.",
    )
    parser.add_argument(
        "--model_temperature",
        type=float,
        default=None,
        help="Temperature to fix the generation model at (e.g. o3 can only be run with 1.0)",
    )

    # Grounding model config: Self-hosted endpoint based (required)
    parser.add_argument(
        "--ground_provider",
        type=str,
        default=None,
        help="The provider for the grounding model",
    )
    parser.add_argument(
        "--ground_url",
        type=str,
        default=None,
        help="The URL of the grounding model",
    )
    parser.add_argument(
        "--ground_api_key",
        type=str,
        default="",
        help="The API key of the grounding model.",
    )
    parser.add_argument(
        "--ground_model",
        type=str,
        default=None,
        help="The model name for the grounding model",
    )
    parser.add_argument(
        "--grounding_width",
        type=int,
        default=None,
        help="Width of screenshot image after processor rescaling",
    )
    parser.add_argument(
        "--grounding_height",
        type=int,
        default=None,
        help="Height of screenshot image after processor rescaling",
    )

    # AgentS3 specific arguments
    parser.add_argument(
        "--max_trajectory_length",
        type=int,
        default=8,
        help="Maximum number of image turns to keep in trajectory",
    )
    parser.add_argument(
        "--enable_reflection",
        action="store_true",
        default=True,
        help="Enable reflection agent to assist the worker agent",
    )
    parser.add_argument(
        "--enable_local_env",
        action="store_true",
        default=False,
        help="Enable local coding environment for code execution (WARNING: Executes arbitrary code locally)",
    )
    parser.add_argument(
        "--task",
        type=str,
        help="The task instruction for Agent-S3 to perform.",
    )
    parser.add_argument(
        "--list_windows",
        action="store_true",
        help="List visible Windows windows and exit without loading any models.",
    )
    parser.add_argument(
        "--window_title",
        type=str,
        default=None,
        help="Restrict screenshots and actions to the visible Windows window matching this title.",
    )

    args = parser.parse_args()

    if args.list_windows:
        try:
            windows = list_target_windows()
        except TargetWindowError as exc:
            parser.error(str(exc))
        if not windows:
            print("No capturable windows found.")
        for index, item in enumerate(windows, start=1):
            print(
                f"{index:>3}. {item.title} "
                f"[PID={item.process_id}, HWND={item.hwnd}, {item.width}x{item.height}]"
            )
        return

    required_grounding_args = {
        "--ground_provider": args.ground_provider,
        "--ground_url": args.ground_url,
        "--ground_model": args.ground_model,
        "--grounding_width": args.grounding_width,
        "--grounding_height": args.grounding_height,
    }
    missing = [name for name, value in required_grounding_args.items() if value is None]
    if missing:
        parser.error("the following arguments are required: " + ", ".join(missing))

    import pyautogui

    from gui_agents.s3.agents.agent_s import AgentS3
    from gui_agents.s3.agents.grounding import OSWorldACI
    from gui_agents.s3.utils.local_env import LocalEnv

    target_window = None
    if args.window_title:
        try:
            target_window = TargetWindowController(args.window_title)
            initial_window_info = target_window.current_info()
        except TargetWindowError as exc:
            parser.error(str(exc))
        screen_width, screen_height = (
            initial_window_info.width,
            initial_window_info.height,
        )
        print(
            f'Target window selected: "{initial_window_info.title}" '
            f"[PID={initial_window_info.process_id}, HWND={initial_window_info.hwnd}]"
        )
    else:
        screen_width, screen_height = pyautogui.size()

    # Load the general engine params
    engine_params = {
        "engine_type": args.provider,
        "model": args.model,
        "base_url": args.model_url,
        "api_key": args.model_api_key,
        "temperature": getattr(args, "model_temperature", None),
    }

    # Load the grounding engine from a custom endpoint
    engine_params_for_grounding = {
        "engine_type": args.ground_provider,
        "model": args.ground_model,
        "base_url": args.ground_url,
        "api_key": args.ground_api_key,
        "grounding_width": args.grounding_width,
        "grounding_height": args.grounding_height,
    }

    # Initialize environment based on user preference
    local_env = None
    if args.enable_local_env:
        print(
            "⚠️  WARNING: Local coding environment enabled. This will execute arbitrary code locally!"
        )
        local_env = LocalEnv()

    grounding_agent = OSWorldACI(
        env=local_env,
        platform=current_platform,
        engine_params_for_generation=engine_params,
        engine_params_for_grounding=engine_params_for_grounding,
        width=screen_width,
        height=screen_height,
    )
    grounding_agent.restricted_to_window = target_window is not None

    agent = AgentS3(
        engine_params,
        grounding_agent,
        platform=current_platform,
        max_trajectory_length=args.max_trajectory_length,
        enable_reflection=args.enable_reflection,
    )

    task = args.task

    # handle query from command line
    if isinstance(task, str) and task.strip():
        agent.reset()
        run_agent(agent, task, target_window=target_window)
        return

    while True:
        query = input("Query: ")

        agent.reset()

        # Run the agent on your own device
        run_agent(agent, query, target_window=target_window)

        response = input("Would you like to provide another query? (y/n): ")
        if response.lower() != "y":
            break


if __name__ == "__main__":
    main()
