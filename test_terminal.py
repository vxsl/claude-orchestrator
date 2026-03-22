"""Quick sanity check: embed claude CLI inside Textual."""

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer
from terminal import TerminalWidget


class TerminalTest(App):
    CSS = """
    TerminalWidget {
        height: 1fr;
    }
    """

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield TerminalWidget(command="claude", id="term")
        yield Footer()

    def on_mount(self) -> None:
        term = self.query_one("#term", TerminalWidget)
        term.start()


if __name__ == "__main__":
    app = TerminalTest()
    app.run()
