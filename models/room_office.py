# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models


class RoomLocationType(models.Model):
    _name = 'room.location.type'
    _description = "Parking Location Type"
    _order = "sequence, name"

    name = fields.Char(string="Type", required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)


class RoomOffice(models.Model):
    _name = 'room.office'
    _description = "Parking Location"
    _order = "complete_name, id"
    _parent_name = "parent_id"
    _parent_store = True
    _inherit = ['image.mixin']

    name = fields.Char(string="Location Name", required=True, translate=True)
    complete_name = fields.Char(
        string="Complete Name",
        compute="_compute_complete_name",
        store=True,
        recursive=True,
    )
    location_type_id = fields.Many2one(
        "room.location.type", string="Type",
        default=lambda self: self.env.ref('room.location_type_branch', raise_if_not_found=False),
        ondelete="restrict",
    )

    parent_id = fields.Many2one(
        "room.office", string="Parent Location",
        index=True, ondelete="restrict",
    )
    parent_path = fields.Char(index=True)
    child_ids = fields.One2many("room.office", "parent_id", string="Sub-Locations")
    child_count = fields.Integer(compute="_compute_child_count", string="Sub-Locations")

    responsible_id = fields.Many2one(
        "res.users", string="Responsible",
        default=lambda self: self.env.ref('base.user_admin', raise_if_not_found=False),
        help="Person in charge of managing this parking location.",
    )

    company_id = fields.Many2one(
        "res.company", string="Company",
        default=lambda self: self.env.company, required=True,
    )
    room_properties_definition = fields.PropertiesDefinition("Room Properties")
    room_ids = fields.One2many("room.room", "office_id", string="Parking Slots")

    # ── Capacity & live occupancy ─────────────────────────────────────────────
    total_slot_count = fields.Integer(
        string="Total Slots",
        compute="_compute_occupancy",
        store=False,
    )
    active_booking_count = fields.Integer(
        string="Active Now",
        compute="_compute_occupancy",
        store=False,
    )
    occupancy_rate = fields.Float(
        string="Occupancy %",
        compute="_compute_occupancy",
        store=False,
        digits=(5, 1),
    )

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------

    @api.depends("name", "parent_id.complete_name")
    def _compute_complete_name(self):
        for office in self:
            if office.parent_id:
                office.complete_name = f"{office.parent_id.complete_name} / {office.name}"
            else:
                office.complete_name = office.name

    @api.depends("child_ids")
    def _compute_child_count(self):
        for office in self:
            office.child_count = len(office.child_ids)

    def _compute_occupancy(self):
        now = fields.Datetime.now()
        Booking = self.env['room.booking'].sudo()
        Room = self.env['room.room'].sudo()
        for office in self:
            # Include this location + all descendants
            if office.parent_path:
                child_office_ids = self.search(
                    [('parent_path', 'like', office.parent_path + '%')]
                ).ids
            else:
                child_office_ids = [office.id]

            slots = Room.search([('office_id', 'in', child_office_ids), ('active', '=', True)])
            total = len(slots)

            # Only bookings whose time window covers right now
            active = Booking.search_count([
                ('room_id', 'in', slots.ids),
                ('state', 'in', ('confirmed', 'checked_in')),
                ('start_datetime', '<=', now),
                ('stop_datetime', '>=', now),
            ])
            office.total_slot_count = total
            office.active_booking_count = active
            # Store as a 0.0–1.0 fraction; the views render with
            # widget="percentage" which multiplies by 100 for display.
            office.occupancy_rate = (active / total) if total else 0.0

    def _compute_display_name(self):
        for office in self:
            office.display_name = office.complete_name

    # ------------------------------------------------------------------
    # Constraints & actions
    # ------------------------------------------------------------------

    @api.constrains("parent_id")
    def _check_parent_id(self):
        if self._has_cycle():
            raise ValueError("A location cannot be its own parent.")

    def action_view_children(self):
        return {
            'type': 'ir.actions.act_window',
            'name': 'Sub-Locations',
            'res_model': 'room.office',
            'view_mode': 'list,form',
            'domain': [('parent_id', '=', self.id)],
        }

    def action_view_slots(self):
        return {
            'type': 'ir.actions.act_window',
            'name': 'Parking Slots',
            'res_model': 'room.room',
            'view_mode': 'list,form',
            'domain': [('office_id', '=', self.id)],
            'context': {'default_office_id': self.id},
        }

    def action_view_map(self):
        """Open the parking layout map in a dark full-screen viewer (new tab)."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_url',
            'url': f'/parking/map/{self.id}',
            'target': 'new',
        }

    def action_view_bookings(self):
        now = fields.Datetime.now()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Active Bookings',
            'res_model': 'room.booking',
            'view_mode': 'list,form',
            'domain': [
                ('room_id.office_id', '=', self.id),
                ('state', 'in', ('confirmed', 'checked_in')),
                ('start_datetime', '<=', now),
                ('stop_datetime', '>=', now),
            ],
        }
