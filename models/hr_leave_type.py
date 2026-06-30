# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import fields, models


class HrLeaveType(models.Model):
    _inherit = 'hr.leave.type'

    # Whitelist flag: only leave types with this set will trigger the
    # parking booking auto-cancel hook on approval. Defaults to False so
    # short / optional leaves (late arrivals, doctor's hour) don't nuke
    # someone's parking reservation for the rest of the day.
    parking_auto_cancel_bookings = fields.Boolean(
        string="Cancel parking bookings on approval",
        default=False,
        help="When a leave of this type is approved, any overlapping parking "
             "bookings for the employee will be cancelled and their slots "
             "released to the waitlist.",
    )
