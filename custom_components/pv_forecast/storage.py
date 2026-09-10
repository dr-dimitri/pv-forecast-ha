"""Native Speicherabschlüsse beobachten, ohne persistierte Verträge zu verändern."""

from collections.abc import Callable
from typing import Any

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store


class ConfirmedStore(Store[dict[str, Any]]):
    """Erst erfolgreiche Dateischreibungen bestätigen; HA behält Lock und Atomizität."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.write_error: str | None = None
        self._generation = 0
        self._saved_generation = 0
        self._retry_factory: Callable[[], dict[str, Any]] | None = None
        self._retry_delay: float | None = None
        self._write_listener: Callable[[], None] | None = None

    @property
    def write_pending(self) -> bool:
        """Auch während des Schreibens hinzugekommene Änderungen bleiben offen."""
        return self._saved_generation != self._generation

    @callback
    def async_track_writes(
        self, listener: Callable[[], None] | None, retry_delay: float
    ) -> None:
        """Nur ein aktiver Manager darf den bestehenden Speichertakt fortsetzen."""
        self._write_listener = listener
        self._retry_delay = retry_delay

    @callback
    def async_stop_retries(self) -> None:
        """Entladen beendet Wiederholungen; der abschließende Flush bleibt möglich."""
        self._retry_delay = None
        self._async_cleanup_delay_listener()

    async def async_save(self, data: dict[str, Any]) -> None:
        self._generation += 1
        self._retry_factory = lambda: data
        await super().async_save(data)

    async def async_save_checked(self, data: dict[str, Any]) -> None:
        """Bewusste Änderungen nur nach tatsächlicher Persistenz bestätigen."""
        generation = self._generation + 1
        try:
            await self.async_save(data)
        except OSError as err:
            raise HomeAssistantError(
                "Die lokalen PV-Daten wurden nicht gespeichert"
            ) from err
        if self._saved_generation < generation:
            raise HomeAssistantError("Die lokalen PV-Daten wurden nicht gespeichert")

    @callback
    def async_delay_save(
        self, data_func: Callable[[], dict[str, Any]], delay: float = 0
    ) -> None:
        self._generation += 1
        self._retry_factory = data_func
        if self._delay_handle is not None:
            # Auch neue Daten während einer Wiederholung halten den früheren Termin.
            delay = min(
                delay, max(0, self._delay_handle.when() - self.hass.loop.time())
            )
        super().async_delay_save(data_func, delay)

    async def _async_write_data(self, data: dict) -> None:
        # Der native Store ruft diese Stufe unter seinem Schreiblock auf.
        # Der Fehler muss vor dessen loggender Fehlerbehandlung sichtbar werden.
        generation = self._generation
        try:
            await super()._async_write_data(data)
        except Exception:
            self.write_error = "storage_unavailable"
            if self._write_listener is not None:
                self._write_listener()
            if (
                self._retry_delay is not None
                and self._retry_factory is not None
                and self._data is None
            ):
                # Kein Sofortversuch; neue Meldungen verschieben diesen Termin nicht.
                super().async_delay_save(self._retry_factory, self._retry_delay)
            raise
        else:
            self._saved_generation = generation
            self.write_error = None
            if self._write_listener is not None:
                self._write_listener()

    async def async_remove(self) -> None:
        # Ein laufender alter Schreibvorgang muss vor der Löschung enden.
        self._generation += 1
        self._retry_factory = None
        self._data = None
        async with self._write_lock:
            self._generation += 1
            self._retry_factory = None
            self._data = None
            await super().async_remove()
            self._saved_generation = self._generation
            self.write_error = None
