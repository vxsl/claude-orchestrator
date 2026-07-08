"""BrainPreviewView — port of screens.BrainPreviewScreen (P4-A).

Static preview of parsed brain-dump tasks. Dismisses with:
  - "add"    (enter / y) — add all as workstreams
  - "launch" (l)         — add all and launch a session
  - ""       (escape / n / ctrl+h / backspace) — cancel
"""

from __future__ import annotations

from rendering import C_DIM, C_PURPLE, _category_markup

from .modals import ModalView

_HINT = (
    f"[{C_DIM}]Enter/y[/{C_DIM}] add  "
    f"[{C_DIM}]l[/{C_DIM}] add & launch  "
    f"[{C_DIM}]Esc[/{C_DIM}] cancel  "
    f"[{C_DIM}]^H[/{C_DIM}] back"
)


class BrainPreviewView(ModalView):
    cancel_result = ""
    border_color = C_PURPLE

    def __init__(self, tasks: list) -> None:
        super().__init__(title=f"Parsed {len(tasks)} tasks", hint=_HINT)
        self.tasks = tasks
        self._lines = self._build_lines()
        self.box_size = (80, len(self._lines) + 8)  # center() clamps to screen

    def _build_lines(self) -> list[str]:
        lines: list[str] = []
        for i, task in enumerate(self.tasks, 1):
            lines.append(f"  [bold]{i}.[/bold] {task.name}")
            lines.append(f"     {_category_markup(task.category)}")
            if task.raw_text != task.name:
                lines.append(f"     [{C_DIM}]{task.raw_text[:80]}[/{C_DIM}]")
            lines.append("")
        return lines

    def _dispatch_key(self, ev) -> bool:
        if ev.key in ("enter", "y"):
            self.dismiss("add")
            return True
        if ev.key == "l":
            self.dismiss("launch")
            return True
        if ev.key == "n":
            self.dismiss("")
            return True
        return False

    def render_body(self, frame, body) -> None:
        for i, line in enumerate(self._lines[: body.h]):
            self._write_line(frame, body.x, body.y + i, body.w, line)
