"""Manual smoke tool + P5 spike harness: python -m tui.demo_term [command...]

Hosts a single full-screen TerminalPane running $SHELL (or the given
command). ctrl+q quits (a passthrough key — everything else, including
ctrl+c, is forwarded verbatim to the PTY); the app also exits when the
child does. This doubles as the py-spy profiling harness for the P5
performance gate: attach it to a heavy streamer and record the pid.
"""

from __future__ import annotations

import os
import shlex
import signal
import sys

from .app import App
from .termpane import TerminalPane
from .view import View


class TermView(View):
    """Full-screen view hosting one TerminalPane — the reference wiring
    for the pane contract (see tui/termpane.py's module docstring)."""

    def __init__(self, command: str) -> None:
        super().__init__()
        self.pane = TerminalPane(command, passthrough_keys={"ctrl+q"})
        self.pane.on_finished = self._finished

    def _finished(self) -> None:
        self.app.exit()

    def on_show(self) -> None:
        super().on_show()
        self.pane.request_paint = self.app.request_paint
        self.pane.copy_to_clipboard = self.app.copy_to_clipboard
        self.app.register_pane(self.pane)
        if self.pane._pid is None:
            self.pane.start()

    def on_hide(self) -> None:
        super().on_hide()
        self.app.unregister_pane(self.pane)

    def on_resize(self, rect) -> None:
        self.pane.resize(rect.h, rect.w)

    def on_key(self, ev) -> bool:
        if ev.key == "ctrl+q":
            self._quit()
            return True
        return self.pane.handle_key(ev)

    def _quit(self) -> None:
        # Stop the child while the loop is still running: the PTY read
        # happens in an executor thread, and asyncio.run() waits for
        # executor threads at shutdown — a live child would deadlock the
        # exit. (Owning views must likewise stop()/detach before the app
        # exits.) Interactive shells ignore SIGTERM, so first HUP the
        # child's process group like a closing terminal would.
        pane = self.pane
        if pane._pid is not None:
            try:
                os.killpg(pane._pid, signal.SIGHUP)
            except OSError:
                pass
        pane.stop()
        self.app.exit()

    def on_paste(self, ev) -> bool:
        return self.pane.handle_paste(ev)

    def on_mouse(self, ev) -> bool:
        return self.pane.handle_mouse(ev, ev.x, ev.y)

    def render(self, frame, rect) -> None:
        self.pane.render(frame, rect, focused=True)


def main() -> None:
    argv = sys.argv[1:]
    if argv:
        command = " ".join(shlex.quote(a) for a in argv)
    else:
        command = os.environ.get("SHELL", "bash")
    view = TermView(command)
    app = App()
    app.push(view)
    try:
        app.run()
    finally:
        view.pane.stop()  # kill the child if it outlived the UI


if __name__ == "__main__":
    main()
