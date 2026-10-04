"""Psychrometrics shared by the view-model (display) and the controller (automation gates)."""
import math


def dewpoint_c(temp_c, rh_pct):
    """Dew point (°C) from temperature (°C) + relative humidity (%), Magnus-Tetens. None if undefined."""
    if temp_c is None or rh_pct is None or rh_pct <= 0:
        return None
    a, b = 17.625, 243.04
    g = math.log(rh_pct / 100.0) + a * temp_c / (b + temp_c)
    return round(b * g / (a - g), 1)
