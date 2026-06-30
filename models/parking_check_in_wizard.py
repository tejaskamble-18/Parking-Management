# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models, _


class ParkingCheckInWizard(models.TransientModel):
    """Confirm early check-in for managers. Regular users are blocked
    by the check-in window rule; managers can override but must confirm."""
    _name = 'parking.check.in.wizard'
    _description = 'Early Check-In Confirmation'

    booking_ids = fields.Many2many('room.booking', string='Bookings', required=True)
    minutes_early = fields.Integer(string='Minutes Early', readonly=True)
    first_start = fields.Datetime(string='Scheduled Start', readonly=True)
    message = fields.Html(compute='_compute_message', readonly=True)

    @api.depends('minutes_early', 'first_start', 'booking_ids')
    def _compute_message(self):
        for wiz in self:
            count = len(wiz.booking_ids)
            if wiz.minutes_early >= 60:
                hours = wiz.minutes_early // 60
                mins = wiz.minutes_early % 60
                early_str = _("%(h)sh %(m)smin", h=hours, m=mins) if mins else _("%(h)s hours", h=hours)
            else:
                early_str = _("%(m)s minutes", m=wiz.minutes_early)
            wiz.message = _(
                "<p>This check-in is <b>%(early)s</b> before the scheduled start time.</p>"
                "<p>Normal check-in opens %(window)s minutes ahead. You are overriding the policy as a manager.</p>"
                "<p>Proceed with check-in for <b>%(count)s</b> booking(s)?</p>",
                early=early_str,
                window=15,
                count=count,
            )

    def action_confirm(self):
        self.ensure_one()
        return self.booking_ids.with_context(force_early_check_in=True).action_check_in()
