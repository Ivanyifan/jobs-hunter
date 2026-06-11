import os
import sys


def main() -> None:
    port = os.getenv("PORT") or "8501"
    argv = [
        "streamlit",
        "run",
        "frontend/app_frontend.py",
        "--server.port",
        port,
        "--server.address",
        "0.0.0.0",
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    try:
        main()
    except Exception as err:
        print(f"failed to start Streamlit: {err}", file=sys.stderr)
        raise
