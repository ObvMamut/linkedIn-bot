"""What every view shares: access to the services and a `reload()` the app calls."""

from typing import TYPE_CHECKING, cast

from textual.containers import Vertical

from linkedin_bot.runtime.services import Services

if TYPE_CHECKING:
    from linkedin_bot.tui.app import CockpitApp


class View(Vertical):
    """A tab's content. `reload()` refreshes what it shows from storage (never the network)."""

    # Views holding half-typed input opt out of the periodic refresh.
    AUTO_REFRESH = True
    # What gets focus when the view opens (None: its first table).
    FOCUS: str | None = None

    @property
    def cockpit(self) -> CockpitApp:
        from linkedin_bot.tui.app import CockpitApp

        return cast(CockpitApp, self.app)

    @property
    def services(self) -> Services:
        return self.cockpit.services

    async def reload(self) -> None:
        """Override to refresh the view's data."""

    def report(self, message: str, *, error: bool = False) -> None:
        self.app.notify(message, severity="error" if error else "information", markup=False)
