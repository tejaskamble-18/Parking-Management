# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""Leave-approval hook: auto-cancel overlapping parking bookings.

When a leave request is validated, the organiser is definitionally not coming
in — cancelling their parking bookings frees the slot for the waitlist. We
also log a chatter note on each cancelled booking so the user (and audit)
understand why it disappeared.

Overlap check on the booking side (the forward-block: user tries to book
during their own approved leave) lives on room.booking as a constrains rule.
"""

import logging

from odoo import _, models

_logger = logging.getLogger(__name__)


class HrLeave(models.Model):
    _inherit = 'hr.leave'

    def _action_validate(self, check_state=True):
        res = super()._action_validate(check_state=check_state)
        # After super() returns, self is in 'validate' state. Operate sudo'd
        # because the approver may not have write access on the bookings of
        # the leave's employee.
        for leave in self.sudo():
            leave._cancel_overlapping_parking_bookings()
        return res

    def _cancel_overlapping_parking_bookings(self):
        self.ensure_one()
        # Respect the per-type whitelist: short / optional leave types
        # (e.g., late arrivals, doctor's hour) do not affect parking.
        if not (self.holiday_status_id and self.holiday_status_id.parking_auto_cancel_bookings):
            return
        user = self.employee_id.user_id
        if not (user and self.date_from and self.date_to):
            return

        Booking = self.env['room.booking'].sudo()
        bookings = Booking.search([
            ('organizer_id', '=', user.id),
            ('state', 'in', ('pending_approval', 'confirmed')),
            ('start_datetime', '<', self.date_to),
            ('stop_datetime', '>', self.date_from),
        ])
        if not bookings:
            return

        _logger.info(
            "Cancelling %s parking booking(s) for user %s due to approved leave %s",
            len(bookings), user.login, self.id,
        )

        # Snapshot the freed (room_id, start, stop) windows *before* we flip
        # state, so waitlist promotion scans against fresh data.
        freed_windows = [(b.room_id.id, b.start_datetime, b.stop_datetime) for b in bookings]

        for booking in bookings:
            booking.message_post(
                body=_(
                    "Auto-cancelled because the organiser's leave was approved "
                    "(%(date_from)s → %(date_to)s).",
                    date_from=self.date_from,
                    date_to=self.date_to,
                ),
            )
        bookings.write({'state': 'cancelled'})
        bookings._notify_dashboard('cancelled')

        # Trigger the waitlist scan for every freed window.
        Waitlist = self.env['room.booking.waitlist'].sudo()
        for room_id, start, stop in freed_windows:
            Waitlist.auto_promote_for_slot_window(room_id, start, stop)
