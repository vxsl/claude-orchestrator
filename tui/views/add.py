"""AddView — port of screens.AddScreen (P4-B).

Name + description LineEdits and a category Cycler. enter / ctrl+s →
dismiss(Workstream); an empty name keeps the form open with a toast.
escape / ctrl+h → dismiss(None).
"""

from __future__ import annotations

from models import Category, Workstream
from rendering import C_DIM

from ..widgets import Cycler, LineEdit
from .modals import FormModalView

_HINT = (
    f"[{C_DIM}]Enter[/{C_DIM}] create  "
    f"[{C_DIM}]Tab[/{C_DIM}] next field  "
    f"[{C_DIM}]^H[/{C_DIM}] back"
)


class AddView(FormModalView):
    box_size = (70, 18)

    def __init__(self) -> None:
        super().__init__(title="New Workstream", hint=_HINT)
        self.name = self.add_field("Name", LineEdit())
        self.desc = self.add_field("Description (optional)", LineEdit())
        self.category = self.add_field(
            "Category",
            Cycler([(c, c.value) for c in Category], value=Category.PERSONAL),
        )

    def _on_submit(self):
        name = self.name.text.strip()
        if not name:
            self.app.notify("Name cannot be empty", severity="error", timeout=2)
            return None
        return Workstream(name=name, description=self.desc.text.strip(),
                          category=self.category.value)
