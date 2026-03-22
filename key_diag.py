"""Tiny diagnostic: shows what key name Textual sees for each keypress."""
from textual.app import App, ComposeResult
from textual.widgets import Static


class KeyDiag(App):
    CSS = "Screen { align: center middle; } #log { width: 60; height: 20; }"

    def compose(self) -> ComposeResult:
        yield Static("Press keys to see their names. Press 'q' to quit.", id="log")

    def on_key(self, event) -> None:
        if event.key == "q":
            self.exit()
        self.notify(
            f"key={event.key!r}  char={event.character!r}",
            title="Key Event",
            timeout=10,
        )


if __name__ == "__main__":
    KeyDiag().run()
