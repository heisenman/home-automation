"""
Internet weather lane — standalone, modular.

A separate data lane from the BLE sensors: its own source, its own store, its own DB
(default `instance/db/weather.db`), recording outdoor temperature / humidity / pressure
for comparison against locally-measured values.

Designed for the eventual air-gap: the SOURCE is abstracted (`WeatherSource`), so the
internet fetcher (`OpenMeteoSource`) can later be swapped for a transfer-based source that
reads weather data synced in during the offline servers' backup window — without touching
the store or the runner. See ADR-0001 (offline-first) and the §6 "walled lane" idea.
"""
