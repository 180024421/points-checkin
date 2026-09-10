from __future__ import annotations

import sys


def main() -> None:
    if "--native" in sys.argv:
        from .gui import main as native_main

        native_main()
        return
    from .webview_app import main as web_main

    web_main()


if __name__ == "__main__":
    main()
