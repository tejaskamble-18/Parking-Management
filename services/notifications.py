# -*- coding: utf-8 -*-
"""Out-of-band notifications for parking events.

Two channels:

  * Microsoft Teams Incoming Webhook — fire-and-forget HTTP POST with an
    Adaptive Card envelope. URL lives in ``parking.policy``; empty URL ==
    channel disabled.

  * User inbox — we use ``booking.message_post`` with the organiser's
    partner, which is already the path used elsewhere. Centralised here so
    callers don't reimplement the payload.

Deliberately narrow surface: one function per notification type. This keeps
the call sites readable (``Notifications(env).booking_created(booking)``)
and leaves room to add new channels without touching callers.
"""

import json
import logging

import requests

from odoo import _, fields

_logger = logging.getLogger(__name__)

_TEAMS_TIMEOUT = 5  # fire-and-forget; don't block a booking flow


class Notifications:

    def __init__(self, env):
        self.env = env

    # ------------------------------------------------------------------
    # High-level notification entrypoints
    # ------------------------------------------------------------------

    def booking_created(self, booking):
        self._teams_card(
            title=_("New parking booking"),
            subtitle=self._booking_subtitle(booking),
            facts=self._booking_facts(booking),
        )

    def booking_reassigned(self, booking, original_room):
        self._teams_card(
            title=_("Parking reassignment"),
            subtitle=_(
                "%(user)s moved from %(orig)s to %(alt)s",
                user=booking.organizer_id.display_name,
                orig=original_room.name or '—',
                alt=booking.room_id.name or '—',
            ),
            facts=self._booking_facts(booking),
        )

    def booking_no_show(self, booking):
        self._teams_card(
            title=_("No-show recorded"),
            subtitle=self._booking_subtitle(booking),
            facts=self._booking_facts(booking) + [
                {'name': _('Organiser no-shows'),
                 'value': str(booking.organizer_id.sudo().parking_no_show_count or 0)},
            ],
            color='attention',
        )

    def booking_reminder(self, booking):
        """30-minute-before reminder. Called by the reminder cron (wired in
        Phase 4 once the schedule is locked)."""
        self._teams_card(
            title=_("Parking reminder — starts in 30 minutes"),
            subtitle=self._booking_subtitle(booking),
            facts=self._booking_facts(booking),
        )

    # ------------------------------------------------------------------
    # Teams webhook
    # ------------------------------------------------------------------

    def _teams_webhook_url(self):
        url = self.env['ir.config_parameter'].sudo().get_param('room.parking_teams_webhook_url', '')
        return (url or '').strip()

    def _teams_card(self, title, subtitle, facts, color='good'):
        """Post an Adaptive Card-flavoured MessageCard to the configured
        Teams channel. No-ops when the webhook URL is empty (the kill switch).
        """
        url = self._teams_webhook_url()
        if not url:
            return
        payload = {
            '@type': 'MessageCard',
            '@context': 'https://schema.org/extensions',
            'summary': title,
            'themeColor': {'good': '2E7D32', 'attention': 'C62828', 'warning': 'EF6C00'}.get(color, '1565C0'),
            'title': title,
            'sections': [{
                'activityTitle': subtitle,
                'facts': facts,
                'markdown': True,
            }],
        }
        try:
            requests.post(url, data=json.dumps(payload), headers={'Content-Type': 'application/json'}, timeout=_TEAMS_TIMEOUT)
        except requests.RequestException as exc:
            # Never fail a booking because Teams is down. Log once and move on.
            _logger.warning("Parking Teams webhook post failed: %s", exc)

    # ------------------------------------------------------------------
    # Booking payload helpers
    # ------------------------------------------------------------------

    def _booking_subtitle(self, booking):
        return _(
            "%(user)s · %(slot)s · %(window)s",
            user=(booking.organizer_id.display_name or booking.guest_name or _('Unknown')),
            slot=booking.room_id.name or '—',
            window=self._format_window(booking),
        )

    def _booking_facts(self, booking):
        facts = [
            {'name': _('Slot'), 'value': booking.room_id.name or '—'},
            {'name': _('Window'), 'value': self._format_window(booking)},
            {'name': _('Vehicle'), 'value': booking.vehicle_number or '—'},
        ]
        if booking.room_id.office_id:
            facts.append({'name': _('Office'), 'value': booking.room_id.office_id.name or '—'})
        return facts

    @staticmethod
    def _format_window(booking):
        if not (booking.start_datetime and booking.stop_datetime):
            return '—'
        start = fields.Datetime.context_timestamp(booking, booking.start_datetime)
        stop = fields.Datetime.context_timestamp(booking, booking.stop_datetime)
        return f"{start.strftime('%a %b %d %H:%M')} - {stop.strftime('%H:%M')}"
