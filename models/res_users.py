from uuid import uuid4

from odoo import api, fields, models


class ResUsers(models.Model):
    _inherit = "res.users"

    # Personal, capability-based token used by /parking/calendar/<token>.ics so
    # Outlook or Google Calendar can subscribe to a user's parking bookings
    # without a session. Users can regenerate it to revoke existing subscribers.
    parking_ics_token = fields.Char(
        string="Parking Calendar Token",
        default=lambda self: str(uuid4()),
        copy=False,
        readonly=True,
        groups="base.group_user",
    )
    parking_ics_url = fields.Char(
        string="Parking Calendar Feed URL",
        compute="_compute_parking_ics_url",
        groups="base.group_user",
    )

    # --- Smart allocation / fairness fields -----------------------------------
    # Scoring state is stored here, not on hr.employee, because allocation runs
    # per res.user (bookings belong to users) and many users don't have an
    # employee record at all (contractors, service accounts).
    parking_priority_score = fields.Integer(
        string="Parking Priority Score",
        default=50,
        help="Dynamic score used by the smart allocator. Raised by leadership and EV requests, lowered by no-shows.",
        groups="room.group_parking_manager",
    )
    parking_no_show_count = fields.Integer(
        string="No-Show Count",
        default=0,
        readonly=True,
        help="Total bookings marked no_show. Used by the fairness penalty.",
        groups="room.group_parking_manager",
    )
    parking_temp_ban_until = fields.Datetime(
        string="Parking Banned Until",
        copy=False,
        help="If set in the future, this user cannot create new bookings.",
        groups="room.group_parking_manager",
    )
    parking_last_booking_at = fields.Datetime(
        string="Last Booking Created At",
        copy=False,
        readonly=True,
        help="Rolling usage marker used by the fairness weight.",
        groups="room.group_parking_manager",
    )
    # NOTE: no groups= here. Employees must be able to read their own
    # whitelist so the ir.rule on room.office / room.room can evaluate.
    # The field is hidden from the UI via groups= on the view-level <group>.
    parking_visible_office_ids = fields.Many2many(
        'room.office',
        'res_users_parking_office_rel',
        'user_id', 'office_id',
        string="Visible Parking Locations",
        help="Restrict which parking locations this user can see on the dashboard "
             "and book into. Leave empty to grant access to all locations.",
    )

    def _get_parking_scope_office_ids(self):
        """Expand ``parking_visible_office_ids`` to include descendant offices
        via ``parent_path``. Returns a list of ids or an empty list when no
        whitelist is set (meaning: no restriction)."""
        self.ensure_one()
        allowed = self.sudo().parking_visible_office_ids
        if not allowed:
            return []
        Office = self.env['room.office'].sudo()
        out = set(allowed.ids)
        for picked in allowed:
            base = picked.parent_path or ''
            if base:
                for o in Office.search([('parent_path', '=like', f'{base}%')]):
                    out.add(o.id)
        return list(out)

    @api.depends("parking_ics_token")
    def _compute_parking_ics_url(self):
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "")
        for user in self:
            if user.parking_ics_token:
                user.parking_ics_url = f"{base}/parking/calendar/{user.parking_ics_token}.ics"
            else:
                user.parking_ics_url = False

    def action_parking_regenerate_ics_token(self):
        """Revoke the old URL and issue a fresh token."""
        for user in self:
            user.parking_ics_token = str(uuid4())
        return True
