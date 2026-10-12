"""server/gas_index_algorithm.py — Sensirion Gas Index Algorithm (VOC mode), pure-Python port.

A line-for-line port of the vendored C in firmware/components/sensirion_gas_index (Sensirion
gas-index-algorithm v3.2.0, BSD-3-Clause — see that component's LICENSE). Only the VOC path is ported; NOx is
computed on the nodes. Python floats are doubles where the C uses float, so outputs can differ from the C by a
rounding step; tests/test_aq_v2_shadow.py pins it against a C-generated trace.

Why it is here (ADR-0035 amendment, Phase 1): the server runs the SAME adaptive baseline the SGP4x nodes run
on-chip, over a BME680's humidity-corrected ln R, so both sensor families land on one "VOC Index" scale (100 =
this room's recent normal). The node-side algorithm runs at 1 Hz; here it runs on the 1-min rung
(`sampling_interval=60`), which reproduces the SGP41 node's own index on its own voc_raw (Study 2 §9).
Do not tune the constants — re-port from a tagged upstream release instead.
"""
import math

SRAW_MINIMUM = 20000
INITIAL_BLACKOUT = 45.0
INDEX_GAIN = 230.0
SRAW_STD_INITIAL = 50.0
SRAW_STD_BONUS = 220.0
TAU_MEAN_HOURS = 12.0
TAU_VARIANCE_HOURS = 12.0
TAU_INITIAL_MEAN = 20.0
INIT_DURATION_MEAN = 3600.0 * 0.75
INIT_TRANSITION_MEAN = 0.01
TAU_INITIAL_VARIANCE = 2500.0
INIT_DURATION_VARIANCE = 3600.0 * 1.45
INIT_TRANSITION_VARIANCE = 0.01
GATING_THRESHOLD = 340.0
GATING_THRESHOLD_INITIAL = 510.0
GATING_THRESHOLD_TRANSITION = 0.09
GATING_MAX_DURATION_MINUTES = 60.0 * 3.0
GATING_MAX_RATIO = 0.3
SIGMOID_L = 500.0
SIGMOID_K = -0.0065
SIGMOID_X0 = 213.0
INDEX_OFFSET = 100.0
LP_TAU_FAST = 20.0
LP_TAU_SLOW = 500.0
LP_ALPHA = -0.2
GAMMA_SCALING = 64.0
ADDITIONAL_GAMMA_MEAN_SCALING = 8.0
FIX16_MAX = 32767.0


def _sigmoid(x0, k, sample):
    x = k * (sample - x0)
    if x < -50.0:
        return 1.0
    if x > 50.0:
        return 0.0
    return 1.0 / (1.0 + math.exp(x))


class VocIndex:
    """One VOC Index stream. `process(sraw)` once per sample, in time order; returns 0 during the blackout,
    then 1..500 (100 = the stream's learned normal)."""

    def __init__(self, sampling_interval: float = 1.0):
        si = self.si = float(sampling_interval)
        self.uptime = 0.0
        self.sraw = 0.0
        self.gas_index = 0.0
        # mean/variance estimator
        self.mv_init = False
        self.mv_mean = 0.0
        self.mv_offset = 0.0
        self.mv_std = SRAW_STD_INITIAL
        self.g_mean_base = (ADDITIONAL_GAMMA_MEAN_SCALING * GAMMA_SCALING * (si / 3600.0)) / (TAU_MEAN_HOURS + si / 3600.0)
        self.g_var_base = (GAMMA_SCALING * (si / 3600.0)) / (TAU_VARIANCE_HOURS + si / 3600.0)
        self.g_init_mean = (ADDITIONAL_GAMMA_MEAN_SCALING * GAMMA_SCALING * si) / (TAU_INITIAL_MEAN + si)
        self.g_init_var = (GAMMA_SCALING * si) / (TAU_INITIAL_VARIANCE + si)
        self.g_mean = 0.0
        self.g_var = 0.0
        self.uptime_gamma = 0.0
        self.uptime_gating = 0.0
        self.gating_minutes = 0.0
        # mox model
        self.mox_std = self.mv_std
        self.mox_mean = 0.0
        # adaptive lowpass
        self.a1 = si / (LP_TAU_FAST + si)
        self.a2 = si / (LP_TAU_SLOW + si)
        self.lp_init = False
        self.x1 = self.x2 = self.x3 = 0.0

    def process(self, sraw: int) -> int:
        if self.uptime <= INITIAL_BLACKOUT:
            self.uptime += self.si
        else:
            if 0 < sraw < 65000:
                sraw = min(max(sraw, SRAW_MINIMUM + 1), SRAW_MINIMUM + 32767)
                self.sraw = float(sraw - SRAW_MINIMUM)
            gi = (self.sraw - self.mox_mean) / (-1.0 * (self.mox_std + SRAW_STD_BONUS)) * INDEX_GAIN
            gi = self._sigmoid_scaled(gi)
            gi = self._lowpass(gi)
            self.gas_index = max(gi, 0.5)
            if self.sraw > 0.0:
                self._mv_process(self.sraw)
                self.mox_std, self.mox_mean = self.mv_std, self.mv_mean + self.mv_offset
        return int(self.gas_index + 0.5)

    def _sigmoid_scaled(self, sample):
        x = SIGMOID_K * (sample - SIGMOID_X0)
        if x < -50.0:
            return SIGMOID_L
        if x > 50.0:
            return 0.0
        if sample >= 0.0:
            shift = (SIGMOID_L - 5.0 * INDEX_OFFSET) / 4.0
            return (SIGMOID_L + shift) / (1.0 + math.exp(x)) - shift
        return (INDEX_OFFSET / INDEX_OFFSET) * (SIGMOID_L / (1.0 + math.exp(x)))

    def _lowpass(self, sample):
        if not self.lp_init:
            self.x1 = self.x2 = self.x3 = sample
            self.lp_init = True
        self.x1 = (1.0 - self.a1) * self.x1 + self.a1 * sample
        self.x2 = (1.0 - self.a2) * self.x2 + self.a2 * sample
        f1 = math.exp(LP_ALPHA * abs(self.x1 - self.x2))
        tau_a = (LP_TAU_SLOW - LP_TAU_FAST) * f1 + LP_TAU_FAST
        a3 = self.si / (self.si + tau_a)
        self.x3 = (1.0 - a3) * self.x3 + a3 * sample
        return self.x3

    def _calculate_gamma(self):
        limit = FIX16_MAX - self.si
        if self.uptime_gamma < limit:
            self.uptime_gamma += self.si
        if self.uptime_gating < limit:
            self.uptime_gating += self.si
        s_gamma_mean = _sigmoid(INIT_DURATION_MEAN, INIT_TRANSITION_MEAN, self.uptime_gamma)
        gamma_mean = self.g_mean_base + (self.g_init_mean - self.g_mean_base) * s_gamma_mean
        thr = GATING_THRESHOLD + (GATING_THRESHOLD_INITIAL - GATING_THRESHOLD) * _sigmoid(
            INIT_DURATION_MEAN, INIT_TRANSITION_MEAN, self.uptime_gating)
        s_gating_mean = _sigmoid(thr, GATING_THRESHOLD_TRANSITION, self.gas_index)
        self.g_mean = s_gating_mean * gamma_mean
        s_gamma_var = _sigmoid(INIT_DURATION_VARIANCE, INIT_TRANSITION_VARIANCE, self.uptime_gamma)
        gamma_var = self.g_var_base + (self.g_init_var - self.g_var_base) * (s_gamma_var - s_gamma_mean)
        thr = GATING_THRESHOLD + (GATING_THRESHOLD_INITIAL - GATING_THRESHOLD) * _sigmoid(
            INIT_DURATION_VARIANCE, INIT_TRANSITION_VARIANCE, self.uptime_gating)
        s_gating_var = _sigmoid(thr, GATING_THRESHOLD_TRANSITION, self.gas_index)
        self.g_var = s_gating_var * gamma_var
        self.gating_minutes += (self.si / 60.0) * ((1.0 - s_gating_mean) * (1.0 + GATING_MAX_RATIO) - GATING_MAX_RATIO)
        self.gating_minutes = max(self.gating_minutes, 0.0)
        if self.gating_minutes > GATING_MAX_DURATION_MINUTES:
            self.uptime_gating = 0.0

    def _mv_process(self, sraw):
        if not self.mv_init:
            self.mv_init = True
            self.mv_offset = sraw
            self.mv_mean = 0.0
            return
        if self.mv_mean >= 100.0 or self.mv_mean <= -100.0:
            self.mv_offset += self.mv_mean
            self.mv_mean = 0.0
        sraw -= self.mv_offset
        self._calculate_gamma()
        delta = (sraw - self.mv_mean) / GAMMA_SCALING
        c = self.mv_std - delta if delta < 0.0 else self.mv_std + delta
        add = (c / 1440.0) ** 2 if c > 1440.0 else 1.0
        self.mv_std = (math.sqrt(add * (GAMMA_SCALING - self.g_var))
                       * math.sqrt(self.mv_std * (self.mv_std / (GAMMA_SCALING * add))
                                   + (self.g_var * delta / add) * delta))
        self.mv_mean += self.g_mean * delta / ADDITIONAL_GAMMA_MEAN_SCALING
