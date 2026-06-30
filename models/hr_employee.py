# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import fields, models


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    # --- Fields mirrored from res.users (manager-visible parking profile) ----
    parking_priority_score = fields.Integer(
        related='user_id.parking_priority_score',
        string="Priority Score",
        readonly=False,
        groups="room.group_parking_manager",
    )
    parking_no_show_count = fields.Integer(
        related='user_id.parking_no_show_count',
        string="No-Shows",
        readonly=True,
        groups="room.group_parking_manager",
    )
    parking_temp_ban_until = fields.Datetime(
        related='user_id.parking_temp_ban_until',
        string="Banned Until",
        readonly=False,
        groups="room.group_parking_manager",
    )
    parking_last_booking_at = fields.Datetime(
        related='user_id.parking_last_booking_at',
        string="Last Booking",
        readonly=True,
        groups="room.group_parking_manager",
    )
    parking_visible_office_ids = fields.Many2many(
        related='user_id.parking_visible_office_ids',
        string="Visible Parking Locations",
        readonly=False,
        groups="room.group_parking_manager",
    )


class HrEmployeePublic(models.Model):
    _inherit = 'hr.employee.public'
    pass
