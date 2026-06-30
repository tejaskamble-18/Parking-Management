# -*- coding: utf-8 -*-
"""Parking booking waitlist.

When a slot is already taken during a user's desired window, the user joins
the waitlist instead of failing the overlap constraint. The moment an
overlapping booking is cancelled or flagged as no-show, the oldest matching
waitlist entry is auto-promoted into a real booking.
"""

from markupsafe import Markup

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
from odoo.addons.room.services import AllocationService, Notifications


WAITLIST_STATES = [
    ('waiting', 'Waiting'),
    ('promoted', 'Promoted to Booking'),
    ('expired', 'Expired'),
    ('cancelled', 'Cancelled'),
]


class RoomBookingWaitlist(models.Model):
    _name = 'room.booking.waitlist'
    _description = 'Parking Booking Waitlist'
    _inherit = ['mail.thread']
    _order = 'create_date asc, id asc'

    room_id = fields.Many2one(
        'room.room',
        string='Parking Slot',
        required=True,
        index=True,
        ondelete='cascade',
        tracking=True,
    )
    user_id = fields.Many2one(
        'res.users',
        string='Requested By',
        required=True,
        default=lambda self: self.env.user,
        index=True,
        tracking=True,
    )
    employee_id = fields.Many2one('hr.employee', string='Employee')
    vehicle_number = fields.Char(string='Vehicle Number')
    desired_start = fields.Datetime(string='Desired Start', required=True, tracking=True)
    desired_stop = fields.Datetime(string='Desired End', required=True, tracking=True)
    note = fields.Char(string='Note')
    state = fields.Selection(
        WAITLIST_STATES,
        default='waiting',
        required=True,
        tracking=True,
        copy=False,
    )
    promoted_booking_id = fields.Many2one(
        'room.booking',
        string='Promoted Booking',
        readonly=True,
        copy=False,
    )
    # Related fields for convenience
    office_id = fields.Many2one(related='room_id.office_id', string='Office', store=True)
    company_id = fields.Many2one(related='room_id.company_id', string='Company', store=True)

    @api.constrains('desired_start', 'desired_stop')
    def _check_date_window(self):
        for entry in self:
            if entry.desired_start and entry.desired_stop and entry.desired_start >= entry.desired_stop:
                raise ValidationError(_('The desired end time must be after the desired start time.'))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_leave_waitlist(self):
        """Requester or manager cancels a waiting entry."""
        self.filtered(lambda e: e.state == 'waiting').write({'state': 'cancelled'})
        return True

    def action_promote(self):
        """Try to turn each waiting entry into a real booking.

        Flow per entry:
          1. Try the original slot (preserves user intent).
          2. If still blocked, ask the allocator for an alternative within the
             same office, ordered same-zone → same-floor → anything in office.
          3. If an alternative is picked, the booking lands on the new slot and
             chatter records the reassignment so the user sees *why* they're
             on a different slot than they asked for.

        Silent no-op when nothing is available; the next freed-window event
        will retry.
        """
        Booking = self.env['room.booking']
        allocator = AllocationService(self.env)
        created = Booking.browse()
        for entry in self:
            if entry.state != 'waiting':
                continue

            target_room = entry.room_id
            reassigned = False

            if self._slot_is_blocked(target_room, entry.desired_start, entry.desired_stop):
                alt = allocator.find_best_slot(
                    entry.user_id,
                    entry.desired_start, entry.desired_stop,
                    office_id=entry.room_id.office_id.id or None,
                    zone=entry.room_id.zone or None,
                    floor=entry.room_id.floor or None,
                    prefer_ev=entry.room_id.is_ev_charger,
                    prefer_accessible=entry.room_id.is_accessible,
                )
                if not alt:
                    # Still nothing that fits — leave the entry for later.
                    continue
                target_room = alt
                reassigned = True

            booking_vals = {
                'name': _('Waitlist promotion — %s', target_room.name or 'Parking'),
                'room_id': target_room.id,
                'start_datetime': entry.desired_start,
                'stop_datetime': entry.desired_stop,
                'vehicle_number': entry.vehicle_number or '',
                'organizer_id': entry.user_id.id,
            }
            if entry.employee_id:
                booking_vals['employee_id'] = entry.employee_id.id
            # System-triggered promotion: bypass ACLs with sudo(), but keep
            # ownership on organizer_id so the requester sees it in My Bookings.
            booking = Booking.sudo().create(booking_vals)
            entry.write({
                'state': 'promoted',
                'promoted_booking_id': booking.id,
            })

            # Ping the requester in their Odoo inbox. If we reassigned, say so.
            if entry.user_id.partner_id:
                link = Markup('<a href="#" data-oe-model="room.booking" data-oe-id="%d">%s</a>') % (
                    booking.id, booking.name,
                )
                if reassigned:
                    body = Markup(str(_(
                        'Your waitlist request for %(orig)s was reassigned to %(alt)s '
                        'because %(orig)s is still occupied in your window. '
                        'Booking %(link)s is confirmed.'
                    ))) % {
                        'orig': entry.room_id.name,
                        'alt': target_room.name,
                        'link': link,
                    }
                else:
                    body = Markup(str(_(
                        'Good news - slot %(slot)s freed up in your desired window. '
                        'Your waitlist request is now confirmed as booking %(link)s.'
                    ))) % {
                        'slot': entry.room_id.name,
                        'link': link,
                    }
                entry.message_post(body=body, partner_ids=[entry.user_id.partner_id.id])

            # Audit the reassignment on the booking chatter so managers see it.
            if reassigned:
                booking.message_post(body=_(
                    'Auto-promoted from waitlist. Originally requested slot %(orig)s; '
                    'allocator reassigned to %(alt)s within the same office.',
                    orig=entry.room_id.name,
                    alt=target_room.name,
                ))
                Notifications(self.env).booking_reassigned(booking, entry.room_id)
            created |= booking
        return created

    @api.model
    def _slot_is_blocked(self, room, start_dt, stop_dt):
        """True when any non-terminal booking overlaps the window on this slot."""
        return bool(self.env['room.booking'].sudo().search_count([
            ('room_id', '=', room.id),
            ('state', 'in', ('pending_approval', 'confirmed', 'checked_in')),
            ('start_datetime', '<', stop_dt),
            ('stop_datetime', '>', start_dt),
        ], limit=1))

    @api.model
    def auto_promote_for_slot_window(self, room_id, start_dt, stop_dt):
        """Called by room.booking when a blocking booking is cancelled or
        flagged no_show. Promotes waitlist entries whose desired window
        overlaps the freed window.

        Scans two pools (both ordered oldest first):
          * entries waitlisted for the exact freed slot
          * entries in the same office whose original slot is still blocked

        Each candidate is handed to ``action_promote`` which decides whether
        to place it on the original slot or on an allocator-picked alternative.
        """
        if not (room_id and start_dt and stop_dt):
            return 0
        Room = self.env['room.room'].sudo()
        room = Room.browse(room_id)
        if not room.exists():
            return 0

        exact = self.search([
            ('state', '=', 'waiting'),
            ('room_id', '=', room_id),
            ('desired_start', '<', stop_dt),
            ('desired_stop', '>', start_dt),
        ], order='create_date asc')

        same_office = self.search([
            ('state', '=', 'waiting'),
            ('room_id', '!=', room_id),
            ('room_id.office_id', '=', room.office_id.id),
            ('desired_start', '<', stop_dt),
            ('desired_stop', '>', start_dt),
        ], order='create_date asc') if room.office_id else self.browse()

        # Dedup by id (same_office can't overlap with exact because room_id
        # filter is exclusive, but be defensive).
        candidates = exact | (same_office - exact)
        if not candidates:
            return 0
        promoted = candidates.action_promote()
        return len(promoted)

    # ------------------------------------------------------------------
    # Convenience API for the dashboard / My Bookings UI
    # ------------------------------------------------------------------

    @api.model
    def action_join_waitlist(self, vals):
        """Create a waitlist entry for the current user. Called by OWL
        components. `vals` should include room_id, desired_start,
        desired_stop, optional vehicle_number, note."""
        allowed_keys = {'room_id', 'desired_start', 'desired_stop', 'vehicle_number', 'note', 'employee_id'}
        clean_vals = {k: v for k, v in (vals or {}).items() if k in allowed_keys}
        clean_vals['user_id'] = self.env.user.id
        if not clean_vals.get('room_id'):
            raise ValidationError(_('Please pick a parking slot.'))
        if not (clean_vals.get('desired_start') and clean_vals.get('desired_stop')):
            raise ValidationError(_('Please provide both start and end times.'))
        return self.create(clean_vals).id

    @api.model
    def get_my_waitlist_data(self):
        """Return the current user's waiting entries for My Bookings."""
        entries = self.search([
            ('user_id', '=', self.env.user.id),
            ('state', '=', 'waiting'),
        ], order='create_date asc')
        section_colors = self.env['room.room']._SECTION_COLORS
        rows = []
        for entry in entries:
            office = entry.room_id.office_id
            color = section_colors[(office.id or 0) % len(section_colors)]
            start_local = fields.Datetime.context_timestamp(self, entry.desired_start)
            stop_local = fields.Datetime.context_timestamp(self, entry.desired_stop)
            # Queue position — how many older waiting entries precede this
            # one for the same slot.
            position = self.search_count([
                ('state', '=', 'waiting'),
                ('room_id', '=', entry.room_id.id),
                ('create_date', '<', entry.create_date),
            ]) + 1
            rows.append({
                'id': entry.id,
                'slot_name': entry.room_id.name or '—',
                'slot_initials': (entry.room_id.name or '')[:3].upper(),
                'slot_color': color,
                'section_name': office.name or '',
                'vehicle_number': entry.vehicle_number or '',
                'date_label': start_local.strftime('%b %d, %Y'),
                'time_range': f"{start_local.strftime('%H:%M')} - {stop_local.strftime('%H:%M')}",
                'queue_position': position,
                'created_at': entry.create_date.strftime('%Y-%m-%d %H:%M') if entry.create_date else '',
            })
        return rows
