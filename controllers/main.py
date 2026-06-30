# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from werkzeug import exceptions

from odoo import _, http
from odoo.exceptions import UserError
from odoo.http import request


# ---------------------------------------------------------------------------
# Capability-token limits.
# ---------------------------------------------------------------------------
# These public endpoints are gated by the per-slot ``access_token``. Possession
# of the token is the only authorisation check, so we throttle per-token to
# limit the blast radius if a token leaks. Buckets are process-local, so the
# effective limit under N workers is N * _MAX_REQ_PER_TOKEN_PER_MIN; that is
# acceptable for a kiosk workload.
_MAX_REQ_PER_TOKEN_PER_MIN = 30
_NAME_MAX_LEN = 200
_BOOKING_MAX_HOURS = 24
_BOOKING_MAX_AHEAD_DAYS = 90

_throttle_lock = threading.Lock()
_throttle_buckets = {}


def _check_rate_limit(token, limit=_MAX_REQ_PER_TOKEN_PER_MIN, window=60):
    now = time.time()
    with _throttle_lock:
        bucket = _throttle_buckets.get(token)
        if bucket is None:
            bucket = deque()
            _throttle_buckets[token] = bucket
        while bucket and bucket[0] < now - window:
            bucket.popleft()
        if len(bucket) >= limit:
            raise exceptions.TooManyRequests()
        bucket.append(now)


def _parse_dt(raw, field_name):
    if not isinstance(raw, str):
        raise UserError(_("%s must be an ISO-8601 datetime string.", field_name))
    try:
        dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError:
        raise UserError(_("%s is not a valid ISO-8601 datetime.", field_name))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _validate_window(start_dt, stop_dt):
    if stop_dt <= start_dt:
        raise UserError(_("The booking must end after it starts."))
    if (stop_dt - start_dt) > timedelta(hours=_BOOKING_MAX_HOURS):
        raise UserError(_("Bookings cannot exceed %s hours.", _BOOKING_MAX_HOURS))
    if start_dt > datetime.utcnow() + timedelta(days=_BOOKING_MAX_AHEAD_DAYS):
        raise UserError(_("Bookings cannot be scheduled more than %s days ahead.", _BOOKING_MAX_AHEAD_DAYS))


def _sanitize_name(name):
    if not isinstance(name, str):
        raise UserError(_("The booking name must be a string."))
    name = name.strip()
    if not name:
        raise UserError(_("The booking name cannot be empty."))
    if len(name) > _NAME_MAX_LEN:
        raise UserError(_("The booking name must be under %s characters.", _NAME_MAX_LEN))
    return name


class RoomController(http.Controller):

    # ------
    # ROUTES
    # ------

    @http.route("/room/<string:short_code>/book", type="http", auth="public", website=True)
    def room_book(self, short_code):
        room_sudo = request.env["room.room"].sudo().search([("short_code", "=", short_code)])
        if not room_sudo:
            raise exceptions.NotFound()
        return request.render("room.room_booking", {"room": room_sudo})

    @http.route("/room/<string:access_token>/get_existing_bookings", type="jsonrpc", auth="public")
    def get_existing_bookings(self, access_token):
        _check_rate_limit(access_token)
        room_sudo = self._fetch_room_from_access_token(access_token)
        return request.env["room.booking"].sudo().search_read(
            [("room_id", "=", room_sudo.id), ("stop_datetime", ">", datetime.now())],
            ["name", "organizer_id", "start_datetime", "stop_datetime"],
            order="start_datetime asc",
        )

    @http.route("/room/<string:access_token>/background", type="http", auth="public")
    def room_background_image(self, access_token):
        room_sudo = self._fetch_room_from_access_token(access_token)
        if not room_sudo.room_background_image:
            return ""
        return request.env['ir.binary']._get_image_stream_from(room_sudo, "room_background_image").get_response()

    @http.route("/room/<string:access_token>/booking/create", type="jsonrpc", auth="public")
    def room_booking_create(self, access_token, name, start_datetime, stop_datetime):
        _check_rate_limit(access_token)
        room_sudo = self._fetch_room_from_access_token(access_token)
        clean_name = _sanitize_name(name)
        start_dt = _parse_dt(start_datetime, _("Start time"))
        stop_dt = _parse_dt(stop_datetime, _("End time"))
        _validate_window(start_dt, stop_dt)
        return request.env["room.booking"].sudo().create({
            "name": clean_name,
            "room_id": room_sudo.id,
            "start_datetime": start_dt,
            "stop_datetime": stop_dt,
        }).id

    @http.route("/room/<string:access_token>/booking/<int:booking_id>/delete", type="jsonrpc", auth="public")
    def room_booking_delete(self, access_token, booking_id):
        _check_rate_limit(access_token)
        return self._fetch_booking(booking_id, access_token).unlink()

    @http.route("/room/<string:access_token>/booking/<int:booking_id>/update", type="jsonrpc", auth="public")
    def room_booking_update(self, access_token, booking_id, **kwargs):
        _check_rate_limit(access_token)
        booking_sudo = self._fetch_booking(booking_id, access_token)

        vals = {}
        if kwargs.get('name'):
            vals['name'] = _sanitize_name(kwargs['name'])
        start_dt = _parse_dt(kwargs['start_datetime'], _("Start time")) if kwargs.get('start_datetime') else None
        stop_dt = _parse_dt(kwargs['stop_datetime'], _("End time")) if kwargs.get('stop_datetime') else None
        if start_dt or stop_dt:
            effective_start = start_dt or booking_sudo.start_datetime
            effective_stop = stop_dt or booking_sudo.stop_datetime
            _validate_window(effective_start, effective_stop)
            if start_dt:
                vals['start_datetime'] = start_dt
            if stop_dt:
                vals['stop_datetime'] = stop_dt

        if not vals:
            return False
        return booking_sudo.write(vals)

    # ------
    # TOOLS
    # ------

    def _fetch_booking(self, booking_id, access_token):
        """Return the sudo-ed booking if it takes place in the room corresponding
        to the given access token
        """
        room_sudo = self._fetch_room_from_access_token(access_token)
        booking_sudo = room_sudo.room_booking_ids.filtered_domain([('id', '=', booking_id)])
        if not booking_sudo:
            raise exceptions.NotFound()
        return booking_sudo

    def _fetch_room_from_access_token(self, access_token):
        """Return the sudo-ed record of the room corresponding to the given
        access token
        """
        room_sudo = request.env["room.room"].sudo().search([("access_token", "=", access_token)])
        if not room_sudo:
            raise exceptions.NotFound()
        return room_sudo
