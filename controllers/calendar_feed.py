# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""ParkSmart iCalendar (ics) feed controller.

Exposes a read-only, token-authenticated calendar feed that Outlook,
Google Calendar, Apple Calendar, etc. can subscribe to via
"Add Calendar from Internet / From URL". Output conforms to RFC 5545.

Pure stdlib only — no third-party ics packages.
"""

from datetime import timedelta

from odoo import fields
from odoo.http import Controller, request, route


# --- RFC 5545 helpers --------------------------------------------------------

# Maximum octets per content line per RFC 5545 section 3.1.
_ICS_LINE_MAX_OCTETS = 75


def _escape_text(value):
    """Escape a TEXT value per RFC 5545 section 3.3.11.

    Order matters: backslash must be escaped first, then the structural
    separators, and finally any newlines are replaced with the literal
    two-character sequence ``\\n``.
    """
    if value is None:
        return ''
    text = str(value)
    text = text.replace('\\', '\\\\')
    text = text.replace(';', '\\;')
    text = text.replace(',', '\\,')
    # Normalise all newline variants to the ics literal "\n".
    text = text.replace('\r\n', '\\n').replace('\r', '\\n').replace('\n', '\\n')
    return text


def _fold_line(line):
    """Fold a single logical ics line to <=75 octets per physical line.

    Continuation lines are prefixed with a single space (per RFC 5545).
    Folding is byte-aware so multibyte UTF-8 characters are never split.
    """
    encoded = line.encode('utf-8')
    if len(encoded) <= _ICS_LINE_MAX_OCTETS:
        return line

    chunks = []
    # First chunk gets up to 75 octets; continuation chunks get up to 74
    # because the leading space itself occupies one octet.
    first = True
    idx = 0
    total = len(encoded)
    while idx < total:
        limit = _ICS_LINE_MAX_OCTETS if first else (_ICS_LINE_MAX_OCTETS - 1)
        end = min(idx + limit, total)
        # Back off if we would split a multibyte UTF-8 sequence.
        while end < total and (encoded[end] & 0xC0) == 0x80:
            end -= 1
        chunk = encoded[idx:end].decode('utf-8')
        chunks.append(chunk if first else ' ' + chunk)
        first = False
        idx = end
    return '\r\n'.join(chunks)


def _format_utc(dt):
    """Format a naive-UTC ``datetime`` as ``YYYYMMDDTHHMMSSZ``."""
    return dt.strftime('%Y%m%dT%H%M%SZ')


def _ics_status(state):
    """Map a booking state to an ics STATUS value."""
    return 'COMPLETED' if state == 'completed' else 'CONFIRMED'


def _build_summary(booking):
    """Compact title that reads well in a 1-line calendar cell.

    Format: "🚗 <slot> · <vehicle>"  e.g.  "🚗 A3 · MH12KJ2225"
    Falls back cleanly when slot name or vehicle is missing.
    """
    room_name = (booking.room_id.name or '') if booking.room_id else ''
    vehicle = booking.vehicle_number or ''
    core = room_name or '—'
    if vehicle:
        core = u'%s \u00b7 %s' % (core, vehicle)
    return u'\U0001F697 %s' % core  # 🚗


def _build_location(booking):
    room = booking.room_id
    office_name = (room.office_id.name if room and room.office_id else None) or 'Parking'
    slot_name = (room.name if room else None) or '-'
    location = u'%s \u00b7 Slot %s' % (office_name, slot_name)
    if room and room.location:
        location = u'%s \u00b7 %s' % (location, room.location)
    return location


# Plain-English label per booking state.
_STATE_LABELS = {
    'confirmed': u'\u2713 Reserved',      # ✓
    'checked_in': u'\u25B6 In progress',   # ▶
    'completed': u'\u2714 Completed',      # ✔
    'cancelled': u'\u2715 Cancelled',      # ✕
    'no_show': u'\u26A0 No-show',          # ⚠
}


def _build_description(booking):
    """Multi-line body shown inside the event when opened in Outlook."""
    room = booking.room_id
    slot = (room.name if room else '') or '—'
    office_name = (room.office_id.name if room and room.office_id else '') or 'Parking'
    vehicle = booking.vehicle_number or u'\u2014'
    who = (
        (booking.employee_id.name if booking.employee_id else None)
        or (booking.organizer_id.name if booking.organizer_id else None)
        or u'\u2014'
    )
    status_label = _STATE_LABELS.get(booking.state, (booking.state or '').title() or '—')
    title = booking.name or u'Parking booking'

    lines = [
        title,
        u'',
        u'Slot: %s' % slot,
        u'Location: %s' % office_name,
        u'Vehicle: %s' % vehicle,
        u'Booked by: %s' % who,
    ]
    if booking.is_guest_booking:
        guest_name = booking.guest_name or u'—'
        guest_vehicle = booking.guest_vehicle_number or u'—'
        lines.append(u'Visitor: %s (%s)' % (guest_name, guest_vehicle))
    lines.append(u'Status: %s' % status_label)

    # Real newlines; _escape_text converts them to the ics "\n" literal.
    return u'\n'.join(lines)


def _emit(lines, raw_line):
    """Append an ics content line after folding to the 75-octet limit."""
    lines.append(_fold_line(raw_line))


# --- Controller --------------------------------------------------------------


class ParkSmartCalendarFeed(Controller):
    """Public, token-scoped ics feed for a user's parking bookings."""

    @route(
        '/parking/calendar/<string:token>.ics',
        type='http', auth='public', csrf=False,
    )
    def parksmart_ics(self, token, **_kwargs):
        # 1. Resolve the opaque token to a user. The token field lives on
        #    res.users and is populated elsewhere; we treat this route as
        #    capability-based (possession of the token == read access).
        user = request.env['res.users'].sudo().search(
            [('parking_ics_token', '=', token), ('active', '=', True)], limit=1,
        )
        if not user:
            return request.not_found()

        # 2. Fetch bookings scoped to that user. 90-day lookback keeps the
        #    feed tight; 500-row cap prevents runaway payloads.
        cutoff = fields.Datetime.now() - timedelta(days=90)
        bookings = request.env['room.booking'].with_user(user).search([
            ('organizer_id', '=', user.id),
            ('state', 'in', ['confirmed', 'checked_in', 'completed']),
            ('stop_datetime', '>=', cutoff),
        ], limit=500, order='start_datetime asc')

        # 3. Build the ics payload.
        body = self._render_ics(bookings)

        # 4. Return as a downloadable text/calendar response. Short private
        #    cache lets clients poll without hammering the DB.
        return request.make_response(
            body,
            headers=[
                ('Content-Type', 'text/calendar; charset=utf-8'),
                ('Content-Disposition', 'attachment; filename="parksmart.ics"'),
                ('Cache-Control', 'private, max-age=300'),
            ],
        )

    # -- rendering ------------------------------------------------------------

    def _render_ics(self, bookings):
        """Return the full VCALENDAR document as UTF-8 bytes."""
        now_stamp = _format_utc(fields.Datetime.now())
        lines = []

        # Envelope — always emitted, even when there are zero bookings.
        _emit(lines, 'BEGIN:VCALENDAR')
        _emit(lines, 'VERSION:2.0')
        _emit(lines, 'PRODID:-//ParkSmart//Parking Booking//EN')
        _emit(lines, 'CALSCALE:GREGORIAN')
        _emit(lines, 'METHOD:PUBLISH')
        _emit(lines, 'X-WR-CALNAME:ParkSmart Parking Bookings')
        _emit(lines, 'X-WR-TIMEZONE:UTC')

        for booking in bookings:
            # Skip rows with missing datetimes — they cannot produce a
            # valid VEVENT and would blow up strftime().
            if not booking.start_datetime or not booking.stop_datetime:
                continue

            uid_core = booking.calendar_event_uid or ('booking-%s' % booking.id)
            uid = '%s@parksmart.local' % uid_core

            _emit(lines, 'BEGIN:VEVENT')
            _emit(lines, 'UID:%s' % _escape_text(uid))
            _emit(lines, 'DTSTAMP:%s' % now_stamp)
            _emit(lines, 'DTSTART:%s' % _format_utc(booking.start_datetime))
            _emit(lines, 'DTEND:%s' % _format_utc(booking.stop_datetime))
            _emit(lines, 'SUMMARY:%s' % _escape_text(_build_summary(booking)))
            _emit(lines, 'LOCATION:%s' % _escape_text(_build_location(booking)))
            _emit(lines, 'DESCRIPTION:%s' % _escape_text(_build_description(booking)))
            _emit(lines, 'STATUS:%s' % _ics_status(booking.state))
            # 15-minute popup reminder so Outlook nudges the driver before
            # their slot starts.
            if booking.state in ('confirmed', 'checked_in'):
                _emit(lines, 'BEGIN:VALARM')
                _emit(lines, 'ACTION:DISPLAY')
                _emit(lines, 'DESCRIPTION:%s' % _escape_text(
                    u'Parking slot %s starts soon' % (booking.room_id.name or '')
                ))
                _emit(lines, 'TRIGGER:-PT15M')
                _emit(lines, 'END:VALARM')
            _emit(lines, 'END:VEVENT')

        _emit(lines, 'END:VCALENDAR')

        # RFC 5545 mandates CRLF between content lines; a trailing CRLF keeps
        # strict parsers happy.
        return ('\r\n'.join(lines) + '\r\n').encode('utf-8')
