"""Todo views — ports of screens.TodoScreen / _TodoContextScreen (P4-B).

TodoView is the fullscreen todo list for a workstream: header stats,
todo blocks, a context preview for the highlighted item, and the
original's action keys (a add, e edit, E context, d delete, space
toggle, enter/c spawn, J/K reorder, ? help). Dismisses None.

TodoContextView edits a todo's context notes. Save-on-close like the
original (^H/escape *saves*): every exit path dismisses with the
stripped editor text; the caller persists it via state.edit_todo.
"""

from __future__ import annotations

from rendering import (
    C_BLUE, C_DIM, C_FAINT, C_GOLD, C_GREEN, C_LIGHT, C_YELLOW,
    _render_todo_option, _rich_escape,
)

from ..keys import KeyEvent
from ..layout import Rect
from ..widgets import BlockList, TextEdit
from .help import HelpView
from .modals import FormModalView, ListModalView
from .quick_note import QuickNoteView
from .todo_edit import TodoEditView

_CTX_HINT = f"[{C_DIM}]^H[/{C_DIM}] save & back"

_TODO_HELP = "  ".join(
    f"[{C_YELLOW}]{k}[/{C_YELLOW}] {v}" for k, v in [
        ("a", "add"), ("Enter", "spawn"), ("Space", "done"), ("e", "edit"),
        ("d", "del"), ("E", "ctx"), ("J/K", "reorder"), ("q", "back"),
    ]
)


class TodoView(ListModalView):
    fullscreen = True
    list_cls = BlockList

    def __init__(self, ws, store) -> None:
        super().__init__(title="", hint=_TODO_HELP)
        self.ws = ws
        self.store = store
        self._items: list = []
        self.list.on_highlight = lambda _id: self.request_paint()
        self._rebuild()

    @property
    def _state(self):
        return self.app.state

    # ── data ──────────────────────────────────────────────────────

    def _rebuild(self) -> None:
        from state import AppState

        self.ws = self.store.get(self.ws.id) or self.ws
        self._items = AppState.active_todos(self.ws)
        self.title = (
            f"◆ Todos  [{C_DIM}]{_rich_escape(self.ws.name)}[/{C_DIM}]"
        )
        rows = []
        for item in self._items:
            lines = _render_todo_option(item).split("\n")
            rows.append((item.id, lines[0], False))
            rows.extend(((item.id, j), line, True)
                        for j, line in enumerate(lines[1:], 1))
        self.list.set_rows(rows)
        self.request_paint()

    def _highlighted_item(self):
        hid = self.list.highlighted_id
        if hid is None:
            return None
        key = BlockList.block_key(hid)
        return next((t for t in self._items if t.id == key), None)

    # ── keys ──────────────────────────────────────────────────────

    def _dispatch_key(self, ev) -> bool:
        key = ev.key
        if key == "a":
            self._add()
            return True
        if key == "e":
            self._edit()
            return True
        if key == "E":
            self._edit_context()
            return True
        if key == "d":
            self._delete()
            return True
        if key == "space":
            self._toggle_done()
            return True
        if key == "c":
            self._spawn()
            return True
        if key == "J":
            self._move(1)
            return True
        if key == "K":
            self._move(-1)
            return True
        if key == "question_mark":
            self.app.push(HelpView(context="todo"))
            return True
        if key == "q":  # the original's footer promise ("q back")
            self._cancel()
            return True
        if key == "l":  # not bound in the original — never selects
            return True
        return self.list.handle_key(ev)

    def _on_selected(self, item_id) -> None:  # enter
        self._spawn()

    # ── actions (ports of TodoScreen's action_* methods) ─────────

    def _add(self) -> None:
        def on_text(text) -> None:
            if text and text.strip():
                self._state.add_todo(self.ws.id, text.strip())
                self._rebuild()
                self.list.handle_key(KeyEvent("G", None))  # jump to newest
                self.app.notify("Todo added", timeout=1)

        self.app.push(QuickNoteView(self.ws), on_result=on_text)

    def _toggle_done(self) -> None:
        item = self._highlighted_item()
        if not item:
            return
        self._state.toggle_todo(self.ws.id, item.id)
        self._rebuild()

    def _edit(self) -> None:
        item = self._highlighted_item()
        if not item:
            return

        def on_text(text) -> None:
            if text and text.strip():
                self._state.edit_todo(self.ws.id, item.id, text=text.strip())
                self._rebuild()

        self.app.push(TodoEditView(item.text), on_result=on_text)

    def _edit_context(self) -> None:
        item = self._highlighted_item()
        if not item:
            return

        def on_ctx(new_ctx) -> None:
            if new_ctx is not None:
                self._state.edit_todo(self.ws.id, item.id, context=new_ctx)
            self._rebuild()

        self.app.push(TodoContextView(item), on_result=on_ctx)

    def _delete(self) -> None:
        item = self._highlighted_item()
        if not item:
            return
        self._state.delete_todo(self.ws.id, item.id)
        self._rebuild()
        self.app.notify("Todo deleted", timeout=1)

    def _spawn(self) -> None:
        item = self._highlighted_item()
        if not item:
            return
        prompt = item.text
        if item.context:
            prompt = f"{item.text}\n\n{item.context}"
        self._state.toggle_todo(self.ws.id, item.id)  # mark done
        self._rebuild()
        self.app.launch_claude_session(self.ws, prompt=prompt, reuse_pending=False)

    def _move(self, direction: int) -> None:
        item = self._highlighted_item()
        if item:
            self._state.reorder_todo(self.ws.id, item.id, direction)
            self._rebuild()

    # ── rendering ─────────────────────────────────────────────────

    def _stats_markup(self) -> str:
        done = sum(1 for t in self._items if t.done)
        crystal = sum(
            1 for t in self._items
            if getattr(t, "origin", "manual") == "crystallized"
        )
        pending = len(self._items) - done
        parts = []
        if pending:
            parts.append(f"[{C_LIGHT}]{pending}[/{C_LIGHT}] pending")
        if done:
            parts.append(f"[{C_GREEN}]{done}[/{C_GREEN}] done")
        if crystal:
            parts.append(f"[{C_GOLD}]{crystal}[/{C_GOLD}] crystallized")
        sep = f" [{C_FAINT}]·[/{C_FAINT}] "
        return sep.join(parts) if parts else f"[{C_DIM}]empty[/{C_DIM}]"

    def _context_lines(self) -> list[str]:
        item = self._highlighted_item()
        if item and item.context:
            is_crystal = getattr(item, "origin", "manual") == "crystallized"
            all_lines = item.context.strip().split("\n")
            lines = [_rich_escape(line) for line in all_lines[:4]]
            label_color = C_GOLD if is_crystal else C_BLUE
            lines[0] = f"[{label_color}]Context:[/{label_color}] {lines[0]}"
            if len(all_lines) > 4:
                lines.append(f"[{C_DIM}]...[/{C_DIM}]")
            return lines
        if item:
            return [f"[{C_DIM}]No context — E to add[/{C_DIM}]"]
        return []

    def render_body(self, frame, body: Rect) -> None:
        if body.h < 3:
            return
        self._write_line(frame, body.x, body.y, body.w, self._stats_markup())
        ctx = self._context_lines()
        ctx_h = len(ctx) + 1 if ctx else 0
        list_h = max(1, body.h - 2 - ctx_h)
        if self._items:
            self.list.page_size = list_h
            for i, line in enumerate(self.list.render(body.w, list_h)):
                self._write_line(frame, body.x, body.y + 2 + i, body.w, line)
        else:
            self._write_line(
                frame, body.x, body.y + 2, body.w,
                f"[{C_DIM}]No todos — press [bold]a[/bold] to add[/{C_DIM}]",
            )
        for i, line in enumerate(ctx):
            self._write_line(frame, body.x, body.bottom - len(ctx) + i,
                             body.w, line)


class TodoContextView(FormModalView):
    box_size = (80, 24)
    textedit_height = 15

    def __init__(self, item) -> None:
        label = item.text[:40] if item else "?"
        super().__init__(title=f"Context: {_rich_escape(label)}", hint=_CTX_HINT)
        self.item = item
        self.area = self.add_field("", TextEdit(item.context if item else ""))

    def _text(self) -> str:
        return "\n".join(self.area.lines).strip()

    def _submit(self) -> None:
        self.dismiss(self._text())

    def _cancel(self) -> None:  # escape/^H save too — original semantics
        self.dismiss(self._text())
