from __future__ import annotations

import sys

from checkin_tool import __version__


def main() -> None:
    if "--native" in sys.argv:
        from checkin_tool.gui import main as native_main

        native_main()
        return
    from checkin_tool.webview_app import main as web_main

    web_main()


if __name__ == "__main__":
    main()
