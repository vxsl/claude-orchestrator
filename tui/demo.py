"""Manual smoke demo for the engine: python -m tui.demo

Keys: q quits · s suspends to a shell command · p copies to clipboard
(OSC 52) · any other key echoes in the status line · paste echoes too.
The spinner row animates on a 0.3s view interval.
"""

from __future__ import annotations

import subprocess

from .app import App
from .layout import split_rows
from .view import View

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class DemoView(View):
    def __init__(self) -> None:
        super().__init__()
        self.last_key = "(none)"
        self.last_paste = ""
        self.spin = 0

    def on_show(self) -> None:
        super().on_show()
        if not self._timers:
            self.set_interval(0.3, self._tick)

    def _tick(self) -> None:
        self.spin += 1
        self.request_paint()

    def on_key(self, ev) -> bool:
        self.last_key = ev.key
        if ev.key == "q":
            self.app.exit()
            return True
        if ev.key == "s":
            with self.app.suspend():
                subprocess.run(
                    ["sh", "-c", "echo 'suspended — press enter'; read x"]
                )
            return True
        if ev.key == "p":
            self.app.copy_to_clipboard("tui demo clipboard test")
        self.request_paint()
        return True

    def on_paste(self, ev) -> bool:
        self.last_paste = ev.text.replace("\n", "␤")[:60]
        self.request_paint()
        return True

    def render(self, frame, rect) -> None:
        title, body, status = split_rows(rect, 1, 1.0, 1)
        frame.write_markup(
            title.x, title.y, title.w,
            "[bold #c0caf5 on #24283b] tui engine demo — q quit · s suspend "
            "· p clipboard · try pasting [/]",
        )
        for i in range(min(20, body.h)):
            if i == 3:
                spin = SPINNER[self.spin % len(SPINNER)]
                markup = f"  row {i:2d}  [#e0af68]{spin}[/] spinner (0.3s view interval)"
            else:
                markup = f"  row {i:2d}"
            frame.write_markup(body.x, body.y + i, body.w, markup)
        frame.write_markup(
            status.x, status.y, status.w,
            f"[#565f89]key: [#9ece6a]{self.last_key}[/]   "
            f"paste: [#9ece6a]{self.last_paste}[/][/]",
        )


def main() -> None:
    app = App()
    app.push(DemoView())
    app.run()


if __name__ == "__main__":
    main()
