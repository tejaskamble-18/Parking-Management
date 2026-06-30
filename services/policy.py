# -*- coding: utf-8 -*-
"""Typed reader for parking.policy (ir.config_parameter) keys.

One place to declare defaults and types so callers can write
``ParkingPolicy(env).no_show_grace_minutes`` instead of scattering
``ir.config_parameter.get_param`` calls with silent string-to-int coercions.
"""

from dataclasses import dataclass


# Sentinel so callers can tell "policy never read" from "policy explicitly 0".
_UNSET = object()


def _int(env, key, default):
    raw = env['ir.config_parameter'].sudo().get_param(f'room.{key}', _UNSET)
    if raw is _UNSET or raw in (None, '', False):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _float(env, key, default):
    raw = env['ir.config_parameter'].sudo().get_param(f'room.{key}', _UNSET)
    if raw is _UNSET or raw in (None, '', False):
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _bool(env, key, default):
    raw = env['ir.config_parameter'].sudo().get_param(f'room.{key}', _UNSET)
    if raw is _UNSET or raw in (None, '', False):
        return default
    return str(raw).strip().lower() in ('1', 'true', 't', 'yes', 'y', 'on')


def _str(env, key, default):
    raw = env['ir.config_parameter'].sudo().get_param(f'room.{key}', _UNSET)
    if raw is _UNSET:
        return default
    return raw or default


@dataclass(frozen=True)
class ParkingPolicy:
    """Snapshot of tunables. Cheap to build, so callers instantiate per-operation
    rather than caching. Values are derived from ``ir.config_parameter`` rows
    prefixed ``room.parking_*``; see res_config_settings.py for the surface UI.
    """

    no_show_grace_minutes: int
    no_show_penalty_points: int
    ban_threshold: int
    ban_duration_days: int

    ev_max_hours_per_booking: float
    ev_daily_cap_minutes: int
    ev_cooldown_minutes: int

    score_weight_leadership: int
    score_weight_ev: int
    score_weight_fairness: int
    fairness_window_days: int

    outlook_sync_enabled: bool
    teams_webhook_url: str

    @classmethod
    def load(cls, env):
        return cls(
            no_show_grace_minutes=_int(env, 'parking_no_show_grace_minutes', 15),
            no_show_penalty_points=_int(env, 'parking_no_show_penalty_points', 10),
            ban_threshold=_int(env, 'parking_ban_threshold', 3),
            ban_duration_days=_int(env, 'parking_ban_duration_days', 7),
            ev_max_hours_per_booking=_float(env, 'parking_ev_max_hours_per_booking', 4.0),
            ev_daily_cap_minutes=_int(env, 'parking_ev_daily_cap_minutes', 480),
            ev_cooldown_minutes=_int(env, 'parking_ev_cooldown_minutes', 60),
            score_weight_leadership=_int(env, 'parking_score_weight_leadership', 20),
            score_weight_ev=_int(env, 'parking_score_weight_ev', 10),
            score_weight_fairness=_int(env, 'parking_score_weight_fairness', 25),
            fairness_window_days=_int(env, 'parking_fairness_window_days', 14),
            outlook_sync_enabled=_bool(env, 'parking_outlook_sync_enabled', False),
            teams_webhook_url=_str(env, 'parking_teams_webhook_url', ''),
        )
