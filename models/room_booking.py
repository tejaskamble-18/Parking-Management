# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging
from datetime import datetime, time, timedelta
from uuid import uuid4

from dateutil.relativedelta import relativedelta
from markupsafe import Markup

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)
from odoo.exceptions import ValidationError
from odoo.addons.room.services import AllocationService, GraphService, Notifications, ParkingPolicy


# Fairness / anti-abuse limits. Adjust here if policy changes.
MAX_ADVANCE_BOOKING_DAYS = 15       # employees can't book further than this
PAST_BOOKING_GRACE_MINUTES = 5      # employees can't book in the past (grace absorbs "now"/clock skew)
CHECK_IN_WINDOW_MINUTES = 15        # check-in opens only 15 min before start


BOOKING_STATE_SELECTION = [
    ('pending_approval', 'Pending Approval'),
    ('confirmed', 'Confirmed'),
    ('checked_in', 'Checked In'),
    ('completed', 'Completed'),
    ('cancelled', 'Cancelled'),
    ('no_show', 'No Show'),
]


class RoomBooking(models.Model):
    _name = 'room.booking'
    _inherit = ["mail.thread"]
    _description = "Parking Booking"
    _order = "start_datetime desc, id"

    name = fields.Char(string="Parking Booking Title", required=True, tracking=1)
    active = fields.Boolean(default=True, tracking=8)
    state = fields.Selection(
        BOOKING_STATE_SELECTION,
        string="Status",
        default='confirmed',
        required=True,
        tracking=9,
        copy=False,
    )
    room_id = fields.Many2one("room.room", string="Parking Slot", required=True, index=True, ondelete="cascade", group_expand="_read_group_room_id", tracking=4)
    start_datetime = fields.Datetime(string="Booking Start Time", required=True, index=True, tracking=2)
    stop_datetime = fields.Datetime(string="Booking End Time", required=True, tracking=3)
    organizer_id = fields.Many2one("res.users", string="Organizer", default=lambda self: self.env.user.id if not self.env.user._is_public() else False, index=True, tracking=5)

    # Parking-specific fields
    vehicle_number = fields.Char(string="Vehicle Number", tracking=6)
    employee_id = fields.Many2one("hr.employee", string="Employee", tracking=7)
    location = fields.Char(string="Parking Location", related="room_id.location", store=True, readonly=True)

    # Visitor / guest booking
    is_guest_booking = fields.Boolean(string="Visitor Booking",
        help="Check this if the vehicle belongs to a guest/visitor — a host employee books on their behalf.")
    guest_name = fields.Char(string="Guest Name")
    guest_company = fields.Char(string="Guest Organisation")
    guest_vehicle_number = fields.Char(string="Guest Vehicle Number")

    # Check-in / Check-out tracking
    checked_in_at = fields.Datetime(string="Checked In At", readonly=True, copy=False)
    checked_out_at = fields.Datetime(string="Checked Out At", readonly=True, copy=False)

    # Stable UID for the Outlook/Google ics calendar feed (Phase B).
    calendar_event_uid = fields.Char(
        string="Calendar Event UID",
        default=lambda self: str(uuid4()),
        copy=False,
        readonly=True,
        index=True,
    )

    # Microsoft Graph event sync. Populated by the Phase 3 sync service; kept
    # here so the schema is forward-compatible without requiring M365 to be on.
    outlook_event_id = fields.Char(
        string="Outlook Event ID",
        copy=False,
        readonly=True,
        index=True,
    )
    last_sync_status = fields.Selection(
        [
            ('pending', 'Pending'),
            ('synced', 'Synced'),
            ('failed', 'Failed'),
            ('disabled', 'Sync Disabled'),
        ],
        string="Last Sync Status",
        default='pending',
        copy=False,
        readonly=True,
    )
    last_sync_at = fields.Datetime(string="Last Synced At", copy=False, readonly=True)
    last_sync_error = fields.Char(string="Last Sync Error", copy=False, readonly=True)

    # Phase 5: reminder gate — set True by _cron_send_reminders so we never
    # double-fire the 30-min notification for a single booking.
    reminder_sent = fields.Boolean(string="Reminder Sent", copy=False, default=False)

    # Drives the slot picker in the booking form. Computed from the entered
    # time window so users only see slots actually available for their dates.
    available_slot_ids = fields.Many2many(
        'room.room',
        compute='_compute_available_slots',
        string='Available Slots',
    )
    available_slot_count = fields.Integer(
        compute='_compute_available_slots',
        string='Available Slot Count',
    )

    @api.depends('start_datetime', 'stop_datetime', 'preferred_office_id')
    def _compute_available_slots(self):
        Room = self.env['room.room']
        Booking = self.env['room.booking']
        for booking in self:
            if not (booking.start_datetime and booking.stop_datetime):
                booking.available_slot_ids = Room
                booking.available_slot_count = 0
                continue
            # Any slot that has a blocking booking overlapping the window is busy.
            blocking_states = ('pending_approval', 'confirmed', 'checked_in')
            busy = Booking.search([
                ('id', '!=', booking.id or 0),
                ('state', 'in', blocking_states),
                ('start_datetime', '<', booking.stop_datetime),
                ('stop_datetime', '>', booking.start_datetime),
            ])
            busy_slot_ids = busy.mapped('room_id').ids
            slot_domain = [('active', '=', True), ('id', 'not in', busy_slot_ids)]
            if booking.preferred_office_id:
                slot_domain.append(('office_id', '=', booking.preferred_office_id.id))
            # Scope to the booker's visible offices when they are a regular
            # employee with a whitelist set.
            visible_ids = Room._user_visible_office_ids()
            if visible_ids is not None:
                slot_domain.append(('office_id', 'in', visible_ids))
            available = Room.search(slot_domain)
            # Always include the currently-selected slot (if any) so users
            # editing an existing booking don't see it vanish from the list.
            if booking.room_id and booking.room_id not in available:
                available |= booking.room_id
            booking.available_slot_ids = available
            booking.available_slot_count = len(available)

    # Recurrence
    is_recurring = fields.Boolean(string="Recurring Booking")
    recurrence_type = fields.Selection(
        [('daily', 'Every day'), ('weekly', 'Every week'), ('monthly', 'Every month')],
        string="Repeats",
    )
    recurrence_end_date = fields.Date(string="Ends on")
    recurrence_count = fields.Integer(string="Or after this many bookings (optional)")
    parent_booking_id = fields.Many2one(
        "room.booking", string="Parent Booking",
        ondelete="cascade", index=True, copy=False,
    )
    recurrence_child_ids = fields.One2many(
        "room.booking", "parent_booking_id", string="Recurrences",
    )

    # Fields used to group bookings in gantt view
    office_id = fields.Many2one(related="room_id.office_id", string="Office", readonly=True, store=True)
    company_id = fields.Many2one(related="room_id.company_id", string="Company", readonly=True, store=True)

    # Location filter — used in the booking form to narrow the slot dropdown.
    # Stored so the user's location preference is remembered on the record.
    preferred_office_id = fields.Many2one(
        'room.office', string='Filter by Location',
    )

    @api.depends("room_id.name", "vehicle_number", "name")
    def _compute_display_name(self):
        for booking in self:
            bits = [b for b in (booking.room_id.name, booking.vehicle_number) if b]
            booking.display_name = " - ".join(bits) or booking.name or _("New Booking")

    @api.onchange('organizer_id')
    def _onchange_organizer_id(self):
        if self.organizer_id and not self.name:
            self.name = f"{self.organizer_id.name} - Daily Parking"

    @api.onchange('preferred_office_id')
    def _onchange_preferred_office_id(self):
        if self.preferred_office_id and self.room_id:
            if self.room_id.office_id != self.preferred_office_id:
                self.room_id = False

    @api.constrains("start_datetime", "stop_datetime")
    def _check_date_boundaries(self):
        for booking in self:
            if booking.start_datetime >= booking.stop_datetime:
                raise ValidationError(_(
                    "The start date of %(booking_name)s must be earlier than the end date.",
                    booking_name=booking.name
                ))

    @api.constrains("start_datetime", "stop_datetime")
    def _check_unique_slot(self):
        min_start = min(self.mapped("start_datetime"))
        max_stop = max(self.mapped("stop_datetime"))
        bookings_by_room = self.search([("room_id", "in", self.room_id.ids), ("start_datetime", "<", max_stop), ("stop_datetime", ">", min_start)]).grouped("room_id")
        for booking in self:
            if bookings_by_room.get(booking.room_id) and bookings_by_room[booking.room_id].filtered(
                lambda b: b.id != booking.id and b.start_datetime < booking.stop_datetime and b.stop_datetime > booking.start_datetime
            ):
                raise ValidationError(_(
                    "Parking slot %(room_name)s is already booked during the selected time slot.",
                    room_name=booking.room_id.name
                ))

    @api.constrains("start_datetime")
    def _check_advance_booking_limit(self):
        """Employees can't book more than MAX_ADVANCE_BOOKING_DAYS into the future.
        Recurrence children and manager / admin overrides bypass the rule."""
        user = self.env.user
        if user.has_group('room.group_parking_manager') or user.has_group('base.group_system'):
            return
        now = fields.Datetime.now()
        limit = now + timedelta(days=MAX_ADVANCE_BOOKING_DAYS)
        for booking in self:
            if booking.parent_booking_id or booking.is_recurring:
                continue
            if booking.start_datetime and booking.start_datetime > limit:
                raise ValidationError(_(
                    "Parking can only be booked up to %(days)s days in advance. "
                    "Please pick a date on or before %(limit)s.",
                    days=MAX_ADVANCE_BOOKING_DAYS,
                    limit=fields.Datetime.context_timestamp(booking, limit).strftime('%Y-%m-%d'),
                ))

    @api.constrains("start_datetime")
    def _check_no_past_booking(self):
        """Employees can't book a start time in the past. A small grace window
        absorbs the Quick Book "now" preset and minor clock skew. Recurrence
        children and manager / admin overrides bypass the rule."""
        user = self.env.user
        if user.has_group('room.group_parking_manager') or user.has_group('base.group_system'):
            return
        floor = fields.Datetime.now() - timedelta(minutes=PAST_BOOKING_GRACE_MINUTES)
        for booking in self:
            if booking.parent_booking_id or booking.is_recurring:
                continue
            if booking.start_datetime and booking.start_datetime < floor:
                raise ValidationError(_(
                    "Parking can't be booked in the past. Please pick a start "
                    "time no earlier than %(limit)s.",
                    limit=fields.Datetime.context_timestamp(booking, floor).strftime('%Y-%m-%d %H:%M'),
                ))

    @api.constrains("organizer_id", "start_datetime", "stop_datetime")
    def _check_no_overlapping_own_booking(self):
        """The same organiser cannot hold two overlapping parking reservations.
        Prevents slot hoarding. Visitor bookings (host reserves on behalf of
        a guest) are exempt since one host may legitimately book several
        visitor slots for different guests. Managers / admins bypass."""
        user = self.env.user
        if user.has_group('room.group_parking_manager') or user.has_group('base.group_system'):
            return
        blocking_states = ('pending_approval', 'confirmed', 'checked_in')
        for booking in self:
            if booking.is_guest_booking:
                continue
            if not (booking.organizer_id and booking.start_datetime and booking.stop_datetime):
                continue
            conflict = self.search([
                ('id', '!=', booking.id),
                ('organizer_id', '=', booking.organizer_id.id),
                ('state', 'in', blocking_states),
                ('is_guest_booking', '=', False),
                ('start_datetime', '<', booking.stop_datetime),
                ('stop_datetime', '>', booking.start_datetime),
            ], limit=1)
            if conflict:
                raise ValidationError(_(
                    "%(user)s already has a parking booking (%(other)s) that "
                    "overlaps this time window. Please cancel the other booking "
                    "first or pick a different time.",
                    user=booking.organizer_id.display_name,
                    other=conflict.display_name,
                ))

    @api.constrains("room_id", "organizer_id")
    def _check_slot_access(self):
        """If the slot has allowed_group_ids, the booker (organizer) must be
        in at least one of them. Managers bypass the rule."""
        for booking in self:
            allowed = booking.room_id.allowed_group_ids
            if not allowed:
                continue
            user = booking.organizer_id
            if not user:
                continue
            if user.has_group('room.group_parking_manager'):
                continue
            if not any(g in user.group_ids for g in allowed):
                raise ValidationError(_(
                    'The parking slot "%(slot)s" is restricted. The organizer '
                    '%(user)s is not allowed to book it.',
                    slot=booking.room_id.name,
                    user=user.display_name,
                ))

    @api.constrains("room_id", "organizer_id")
    def _check_slot_in_user_visible_offices(self):
        """Honour the per-employee ``parking_visible_office_ids`` whitelist:
        if set, the organiser can only book slots at one of those offices
        (or a descendant). Managers / admins bypass."""
        for booking in self:
            user = booking.organizer_id
            if not user or not booking.room_id:
                continue
            if user.has_group('room.group_parking_manager') or user.has_group('base.group_system'):
                continue
            allowed = user.sudo().parking_visible_office_ids
            if not allowed:
                continue  # no restriction set = access to all
            slot_office = booking.room_id.office_id
            if not slot_office:
                continue
            # Allowed if the slot's office (or any of its ancestors via
            # parent_path) is in the whitelist.
            slot_path_ids = [int(p) for p in (slot_office.parent_path or '').strip('/').split('/') if p]
            if not set(slot_path_ids) & set(allowed.ids):
                raise ValidationError(_(
                    'You are not allowed to book the slot "%(slot)s" — '
                    '%(office)s is not in your assigned parking locations. '
                    'Contact your parking manager to request access.',
                    slot=booking.room_id.name,
                    office=slot_office.name,
                ))

    @api.constrains("organizer_id")
    def _check_user_not_banned(self):
        """Users whose ``parking_temp_ban_until`` is in the future cannot
        create new bookings. Managers bypass the gate so they can still fix
        things on behalf of a banned user."""
        now = fields.Datetime.now()
        for booking in self:
            user = booking.organizer_id
            if not (user and user.parking_temp_ban_until):
                continue
            if user.parking_temp_ban_until <= now:
                continue
            if self.env.user.has_group('room.group_parking_manager'):
                continue
            raise ValidationError(_(
                "%(user)s is temporarily restricted from booking parking until %(until)s "
                "due to repeated no-shows.",
                user=user.display_name,
                until=fields.Datetime.context_timestamp(booking, user.parking_temp_ban_until),
            ))

    @api.constrains("organizer_id", "start_datetime")
    def _check_organizer_not_fully_remote(self):
        """Soft gate: if the organiser's primary work location is 'home' we
        don't block, but we *do* block guest/visitor bookings and any
        employee whose hr.employee.work_location_type is 'home' on the
        booking date — those users shouldn't be on-site and the slot is
        more useful to someone else. Manager bypass preserved so edge
        cases (employee coming in for a one-off) can still go through.
        """
        if self.env.user.has_group('room.group_parking_manager'):
            return
        for booking in self:
            user = booking.organizer_id
            if not (user and user.employee_id):
                continue
            emp = user.employee_id
            if (emp.work_location_type or '') == 'home':
                raise ValidationError(_(
                    "%(user)s is listed as working from home. Ask a parking "
                    "manager to book on your behalf if you're coming on-site.",
                    user=user.display_name,
                ))

    @api.constrains("organizer_id", "start_datetime", "stop_datetime")
    def _check_user_not_on_leave(self):
        """Forward block: cannot book during your own approved leave. Soft
        guard — relies on the hr_holidays model being installed (declared as
        a hard dep in the manifest). Manager bypass mirrors the ban gate."""
        Leave = self.env.get('hr.leave')
        if Leave is None:
            return
        now_user_is_manager = self.env.user.has_group('room.group_parking_manager')
        for booking in self:
            user = booking.organizer_id
            if not (user and user.employee_id and booking.start_datetime and booking.stop_datetime):
                continue
            if now_user_is_manager:
                continue
            overlap = Leave.sudo().search_count([
                ('employee_id', '=', user.employee_id.id),
                ('state', '=', 'validate'),
                ('holiday_status_id.parking_auto_cancel_bookings', '=', True),
                ('date_from', '<', booking.stop_datetime),
                ('date_to', '>', booking.start_datetime),
            ], limit=1)
            if overlap:
                raise ValidationError(_(
                    "Cannot book parking for %(user)s during their approved leave.",
                    user=user.display_name,
                ))

    @api.constrains("room_id", "organizer_id", "start_datetime", "stop_datetime")
    def _check_ev_rules(self):
        """Three rules, only applied to EV-charger slots:
          1. Per-booking duration cap (slot override > global policy).
          2. Per-user per-day total minutes cap across all EV bookings.
          3. Cross-booking cooldown on the same slot (any user).

        Managers bypass all three so they can patch around edge cases.
        """
        if self.env.user.has_group('room.group_parking_manager'):
            return
        ev_bookings = self.filtered(lambda b: b.room_id.is_ev_charger and b.start_datetime and b.stop_datetime)
        if not ev_bookings:
            return

        policy = ParkingPolicy.load(self.env)
        for booking in ev_bookings:
            booking._check_ev_duration(policy)
            booking._check_ev_daily_cap(policy)
            booking._check_ev_cooldown(policy)

    def _check_ev_duration(self, policy):
        self.ensure_one()
        duration_h = (self.stop_datetime - self.start_datetime).total_seconds() / 3600.0
        cap_h = self.room_id.ev_max_hours_override or policy.ev_max_hours_per_booking
        if cap_h and duration_h > cap_h + 1e-6:
            raise ValidationError(_(
                "EV slot %(slot)s is limited to %(cap)s hours per booking "
                "(this booking is %(duration).1f h).",
                slot=self.room_id.name,
                cap=cap_h,
                duration=duration_h,
            ))

    def _check_ev_daily_cap(self, policy):
        self.ensure_one()
        cap_min = policy.ev_daily_cap_minutes
        if not cap_min or not self.organizer_id:
            return
        # Day window in the booking's own day (UTC) — simple and predictable.
        day_start = self.start_datetime.replace(hour=0, minute=0, second=0, microsecond=0)
        day_stop = day_start + timedelta(days=1)
        siblings = self.search([
            ('id', '!=', self.id or 0),
            ('organizer_id', '=', self.organizer_id.id),
            ('state', 'in', ('pending_approval', 'confirmed', 'checked_in')),
            ('room_id.is_ev_charger', '=', True),
            ('start_datetime', '<', day_stop),
            ('stop_datetime', '>', day_start),
        ])
        total_min = (self.stop_datetime - self.start_datetime).total_seconds() / 60.0
        for s in siblings:
            total_min += (s.stop_datetime - s.start_datetime).total_seconds() / 60.0
        if total_min > cap_min + 1e-6:
            raise ValidationError(_(
                "This booking would push %(user)s past the EV daily cap of %(cap)s minutes "
                "(total would be %(total).0f min).",
                user=self.organizer_id.display_name,
                cap=cap_min,
                total=total_min,
            ))

    def _check_ev_cooldown(self, policy):
        self.ensure_one()
        # Per-slot override > global policy; 0 on both disables the rule.
        cooldown_min = self.room_id.ev_cooldown_minutes or policy.ev_cooldown_minutes
        if not cooldown_min:
            return
        gap = timedelta(minutes=cooldown_min)
        # A booking conflicts with ours if it lies within gap either side of our window.
        conflict = self.search_count([
            ('id', '!=', self.id or 0),
            ('room_id', '=', self.room_id.id),
            ('state', 'in', ('pending_approval', 'confirmed', 'checked_in', 'completed')),
            ('start_datetime', '<', self.stop_datetime + gap),
            ('stop_datetime', '>', self.start_datetime - gap),
        ], limit=1)
        if conflict:
            raise ValidationError(_(
                "EV slot %(slot)s needs a %(min)s-minute cooldown between bookings.",
                slot=self.room_id.name,
                min=cooldown_min,
            ))

    @api.constrains("is_recurring", "recurrence_type", "recurrence_end_date", "recurrence_count", "start_datetime", "stop_datetime")
    def _check_recurrence_config(self):
        for booking in self:
            if not booking.is_recurring:
                continue
            if not booking.recurrence_type:
                raise ValidationError(_("Please choose how often this booking should repeat (Every day, Every week, or Every month)."))
            if not booking.recurrence_end_date and not booking.recurrence_count:
                raise ValidationError(_('Please set an "Ends on" date for the recurrence.'))
            # Sanity cap — no single action should create more than 200 bookings.
            estimate = booking._estimate_recurrence_count()
            if estimate > 200:
                raise ValidationError(_(
                    "This would create %(count)s bookings, which is above the 200 maximum. "
                    'Please choose an earlier "Ends on" date, or lower the occurrence count.',
                    count=estimate,
                ))

    def _estimate_recurrence_count(self):
        """How many bookings this recurrence will generate (parent + children)."""
        self.ensure_one()
        if not (self.is_recurring and self.start_datetime and self.stop_datetime):
            return 0
        step = self._recurrence_step()
        if not step:
            return 0
        max_count = self.recurrence_count or 0
        end_date = self.recurrence_end_date
        count = 1
        current = self.start_datetime + step
        safety = 500
        while safety > 0:
            safety -= 1
            if max_count and count >= max_count:
                break
            if end_date and current.date() > end_date:
                break
            if not max_count and not end_date:
                break
            count += 1
            current += step
        return count

    # ------------------------------------------------------
    # CRUD / ORM
    # ------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        # Smart allocation: when the caller supplies a time window but no
        # room_id, let the allocator pick one. Preference hints can be passed
        # via context (parking_prefer_ev, parking_prefer_office_id, etc.).
        vals_list = [self._auto_pick_slot(v) or v for v in vals_list]

        bookings = super().create(vals_list)
        # Route Premium-slot bookings through approval when the booker is not
        # Leadership / Manager. Keeps the slot reserved (overlap constraint
        # still matches pending rows) but requires a sign-off before it can
        # be checked in.
        for booking in bookings:
            if booking._needs_approval() and booking.state == 'confirmed':
                booking.state = 'pending_approval'
                booking.message_post(
                    body=_(
                        'Approval requested: %(slot)s is a Premium / VIP slot. '
                        'A Leadership user must approve before check-in.',
                        slot=booking.room_id.name or '',
                    ),
                    message_type='notification',
                )
        # Notify frontend views of new bookings
        for room, bookings_ in bookings.grouped("room_id").items():
            room._notify_booking_view("create", bookings_)
        bookings._notify_dashboard('created')
        # Touch the organiser's last-booking marker — used by the fairness weight.
        organisers = bookings.mapped('organizer_id')
        if organisers:
            organisers.sudo().write({'parking_last_booking_at': fields.Datetime.now()})
        # Auto-generate recurring occurrences for any parent booking flagged as recurring.
        # Children themselves have is_recurring=False, so this does not recurse infinitely.
        for booking in bookings:
            if booking.is_recurring and not booking.parent_booking_id:
                booking._generate_recurrences()
        # Arm Outlook sync + fire Teams notification. Both are best-effort;
        # neither will stop a booking from being created.
        bookings._arm_outlook_sync()
        for booking in bookings:
            Notifications(self.env).booking_created(booking)
        return bookings

    @api.model
    def _auto_pick_slot(self, vals):
        """Return a *new* vals dict with room_id filled in, or None when we
        can't or shouldn't pick. Honest failure: if no slot matches, we leave
        vals alone and let the required-field check raise downstream."""
        if vals.get('room_id'):
            return None
        start = vals.get('start_datetime')
        stop = vals.get('stop_datetime')
        if not (start and stop):
            return None

        organizer_id = vals.get('organizer_id') or self.env.uid
        user = self.env['res.users'].browse(organizer_id)
        if not user.exists():
            return None

        ctx = self.env.context
        allocator = AllocationService(self.env)
        picked = allocator.find_best_slot(
            user,
            start, stop,
            prefer_ev=bool(ctx.get('parking_prefer_ev')),
            prefer_accessible=bool(ctx.get('parking_prefer_accessible')),
            office_id=ctx.get('parking_prefer_office_id'),
            zone=ctx.get('parking_prefer_zone'),
            floor=ctx.get('parking_prefer_floor'),
        )
        if not picked:
            return None
        return {**vals, 'room_id': picked.id}

    def _needs_approval(self):
        """A booking needs manager approval when it reserves a Premium slot
        and the organizer is not already a Leadership / Manager user."""
        self.ensure_one()
        if not (self.room_id and self.room_id.is_premium):
            return False
        user = self.organizer_id
        if not user:
            return False
        # Leadership implies Employee; Manager implies Leadership. Either bypass.
        return not user.has_group('room.group_parking_leadership')

    def _recurrence_step(self):
        self.ensure_one()
        return {
            'daily': relativedelta(days=1),
            'weekly': relativedelta(weeks=1),
            'monthly': relativedelta(months=1),
        }.get(self.recurrence_type)

    def _iter_recurrence_dates(self):
        """Yield (start, stop) tuples for each additional occurrence; the
        parent record itself is not yielded — it already exists."""
        self.ensure_one()
        step = self._recurrence_step()
        if not step:
            return
        duration = self.stop_datetime - self.start_datetime
        max_count = self.recurrence_count or 0
        end_date = self.recurrence_end_date
        generated = 1  # parent counts as the first occurrence
        current_start = self.start_datetime + step
        while True:
            if max_count and generated >= max_count:
                break
            if end_date and current_start.date() > end_date:
                break
            if not max_count and not end_date:
                break
            yield current_start, current_start + duration
            generated += 1
            current_start += step

    def _generate_recurrences(self):
        for booking in self:
            child_vals = [{
                'name': booking.name,
                'room_id': booking.room_id.id,
                'vehicle_number': booking.vehicle_number,
                'employee_id': booking.employee_id.id,
                'organizer_id': booking.organizer_id.id,
                'start_datetime': start,
                'stop_datetime': stop,
                'is_recurring': False,
                'parent_booking_id': booking.id,
            } for start, stop in booking._iter_recurrence_dates()]
            if child_vals:
                self.create(child_vals)

    def unlink(self):
        # Best-effort inline Outlook delete before the row is gone. Wrapped
        # in try/except — we don't want a failing Graph delete to strand a
        # user who's trying to clean up.
        graph = GraphService(self.env)
        for booking in self.filtered(lambda b: b.outlook_event_id):
            try:
                graph.sync_booking(booking)
            except Exception:  # noqa: BLE001 — swallow by design
                pass
        # Notify frontend of deleted bookings
        bookings_by_room = self.grouped("room_id")
        for room, bookings in bookings_by_room.items():
            room._notify_booking_view("delete", bookings)
        return super(RoomBooking, self).unlink()

    # Fields whose change warrants an Outlook re-sync. Everything else is
    # Odoo-internal (state, sync markers themselves, etc.) and not worth a
    # round-trip. Re-evaluated on every write.
    _OUTLOOK_SYNC_TRIGGERS = frozenset({
        'name', 'start_datetime', 'stop_datetime', 'room_id',
        'vehicle_number', 'state', 'active',
    })

    def write(self, vals):
        bookings_by_room = self.grouped("room_id")
        res = super(RoomBooking, self).write(vals)
        # Notify frontend of updated bookings
        if new_room_id := vals.get("room_id"):
            new_room = self.env["room.room"].browse(new_room_id)
            for room, bookings in bookings_by_room.items():
                room._notify_booking_view("delete", bookings)
                new_room._notify_booking_view("create", bookings)
        elif {"name", "start_datetime", "stop_datetime"} & vals.keys():
            for room, bookings in bookings_by_room.items():
                room._notify_booking_view("update", bookings)
        # Re-arm Outlook sync when something user-visible changed. Skip when
        # the only change is on the sync markers themselves to avoid a loop.
        if vals.keys() & self._OUTLOOK_SYNC_TRIGGERS:
            self._arm_outlook_sync()
        return res

    @api.model
    def _read_group_room_id(self, rooms, domain):
        # Display all the rooms in the gantt view even if they have no booking,
        # and order them by office first, then by usual order (because the
        # office name is shown in the display name)
        if self.env.context.get("room_booking_gantt_show_all_rooms"):
            return rooms.search([], order=f"office_id, {rooms._order}")
        return rooms

    # ------------------------------------------------------
    # ACTIONS
    # ------------------------------------------------------

    def action_cancel(self):
        """Archive the booking(s) — used as the 'Cancel' action from My Bookings."""
        freed = [(b.room_id.id, b.start_datetime, b.stop_datetime) for b in self]
        self.write({'active': False, 'state': 'cancelled'})
        for room, bookings in self.grouped('room_id').items():
            room._notify_booking_view('delete', bookings)
        self._notify_dashboard('cancelled')
        self._auto_promote_waitlist(freed)
        return True

    def action_cancel_series(self):
        """Archive the entire recurrence series (parent + every occurrence).
        Used by 'Cancel the whole series' in My Bookings."""
        all_series = self.env['room.booking']
        for booking in self:
            root = booking
            while root.parent_booking_id:
                root = root.parent_booking_id
            all_series |= root | root.recurrence_child_ids
        freed = [(b.room_id.id, b.start_datetime, b.stop_datetime) for b in all_series]
        all_series.write({'active': False, 'state': 'cancelled'})
        for room, bookings in all_series.grouped('room_id').items():
            room._notify_booking_view('delete', bookings)
        all_series._notify_dashboard('cancelled')
        self._auto_promote_waitlist(freed)
        return True

    def _auto_promote_waitlist(self, freed_windows):
        """Ask the waitlist model to promote any entries that overlap the
        just-freed windows. `freed_windows` is a list of
        (room_id, start_datetime, stop_datetime) tuples."""
        Waitlist = self.env['room.booking.waitlist'].sudo()
        for room_id, start_dt, stop_dt in freed_windows:
            if room_id and start_dt and stop_dt:
                Waitlist.auto_promote_for_slot_window(room_id, start_dt, stop_dt)

    def action_check_in(self):
        """Mark the booking as actually in-use. Called from My Bookings.
        Check-in opens only CHECK_IN_WINDOW_MINUTES before the booking start
        time - earlier than that, the slot still belongs to the previous
        occupant's availability window. Managers and admins can override
        via a confirmation popup."""
        now = fields.Datetime.now()
        user = self.env.user
        is_privileged = (
            user.has_group('room.group_parking_manager')
            or user.has_group('base.group_system')
        )
        open_window = timedelta(minutes=CHECK_IN_WINDOW_MINUTES)
        force = self.env.context.get('force_early_check_in')

        # Managers / admins attempting early check-in — route through the
        # confirm wizard unless they've already confirmed.
        if is_privileged and not force:
            early = self.filtered(
                lambda b: b.state == 'confirmed'
                and b.start_datetime and b.start_datetime - now > open_window
            )
            if early:
                mins_early = int((early[0].start_datetime - now).total_seconds() // 60)
                wizard = self.env['parking.check.in.wizard'].create({
                    'booking_ids': [(6, 0, self.ids)],
                    'minutes_early': mins_early,
                    'first_start': early[0].start_datetime,
                })
                return {
                    'type': 'ir.actions.act_window',
                    'name': _('Confirm Early Check-In'),
                    'res_model': 'parking.check.in.wizard',
                    'res_id': wizard.id,
                    'view_mode': 'form',
                    'target': 'new',
                }

        changed = self.browse()
        for booking in self:
            if booking.state not in ('confirmed',):
                continue
            if not is_privileged and booking.start_datetime - now > open_window:
                local_start = fields.Datetime.context_timestamp(booking, booking.start_datetime)
                raise ValidationError(_(
                    "Check-in opens %(mins)s minutes before the booking starts "
                    "(%(start)s). Please wait until then.",
                    mins=CHECK_IN_WINDOW_MINUTES,
                    start=local_start.strftime('%b %d, %H:%M'),
                ))
            booking.write({
                'state': 'checked_in',
                'checked_in_at': now,
            })
            changed |= booking
        changed._notify_dashboard('checked_in')
        return True

    def action_check_out(self):
        """Close out a checked-in booking (or a confirmed one that's just ending)."""
        changed = self.browse()
        for booking in self:
            if booking.state not in ('confirmed', 'checked_in'):
                continue
            booking.write({
                'state': 'completed',
                'checked_out_at': fields.Datetime.now(),
            })
            changed |= booking
        changed._notify_dashboard('checked_out')
        return True

    def action_mark_confirmed(self):
        """Manual reset to confirmed — used by admin from Booking Overrides."""
        self.filtered(lambda b: b.state in ('cancelled', 'no_show')).write({
            'state': 'confirmed',
            'active': True,
        })
        return True

    def action_approve(self):
        """Leadership-only: approve a pending_approval booking and flip it
        to confirmed so check-in becomes possible."""
        changed = self.browse()
        for booking in self:
            if booking.state != 'pending_approval':
                continue
            if not self.env.user.has_group('room.group_parking_leadership'):
                raise ValidationError(_('Only Leadership users can approve parking requests.'))
            booking.state = 'confirmed'
            booking.message_post(
                body=_(
                    'Booking approved by %s.',
                    self.env.user.display_name,
                ),
                message_type='notification',
            )
            changed |= booking
        changed._notify_dashboard('updated')
        return True

    def action_reject(self):
        """Leadership-only: reject a pending_approval booking; marks it
        cancelled and archived so the slot frees up."""
        changed = self.browse()
        for booking in self:
            if booking.state != 'pending_approval':
                continue
            if not self.env.user.has_group('room.group_parking_leadership'):
                raise ValidationError(_('Only Leadership users can reject parking requests.'))
            booking.write({'state': 'cancelled', 'active': False})
            booking.message_post(
                body=_(
                    'Booking rejected by %s. The requester has been notified.',
                    self.env.user.display_name,
                ),
                message_type='notification',
            )
            # Tell the frontend tablet kiosk to refresh.
            booking.room_id._notify_booking_view('delete', booking)
            changed |= booking
        changed._notify_dashboard('cancelled')
        return True

    # ------------------------------------------------------
    # CRONS
    # ------------------------------------------------------

    @api.model
    def _cron_flag_no_shows(self):
        """Runs every 15 minutes. Any confirmed booking whose start is more
        than the grace period in the past and which was never checked in is
        marked 'no_show' so the slot returns to the available pool, a penalty
        is applied to the organiser's priority score, and any waiting
        waitlist entry for that window is auto-promoted."""
        policy = ParkingPolicy.load(self.env)
        now = fields.Datetime.now()
        threshold = now - timedelta(minutes=policy.no_show_grace_minutes)
        candidates = self.search([
            ('state', '=', 'confirmed'),
            ('start_datetime', '<=', threshold),
            ('start_datetime', '>', now - timedelta(hours=48)),
            ('checked_in_at', '=', False),
        ])
        if not candidates:
            return 0
        freed = [(b.room_id.id, b.start_datetime, b.stop_datetime) for b in candidates]
        candidates.write({'state': 'no_show'})
        candidates._apply_no_show_penalty(policy)
        candidates._notify_dashboard('no_show')
        # Fire a Teams card per no-show. Teams webhook is fire-and-forget, so
        # even a large cron pass doesn't stall: failed webhook calls log a
        # warning and move on.
        notifier = Notifications(self.env)
        for booking in candidates:
            notifier.booking_no_show(booking)
        self._auto_promote_waitlist(freed)
        return len(candidates)

    def _apply_no_show_penalty(self, policy):
        """Increment the organiser's lifetime no-show counter, subtract the
        configured penalty from their priority score, and apply a temporary
        booking ban if they've crossed the threshold in the rolling window.

        One DB write per organiser — not per booking — so a batch of no-shows
        from the cron stays cheap.
        """
        if not self:
            return
        now = fields.Datetime.now()
        window_start = now - timedelta(days=max(policy.fairness_window_days, 1))
        penalty = policy.no_show_penalty_points
        Booking = self.env['room.booking'].sudo()

        # Aggregate new no-shows per organiser in the current batch.
        per_user_new = {}
        for booking in self:
            uid = booking.organizer_id.id
            if uid:
                per_user_new[uid] = per_user_new.get(uid, 0) + 1
        if not per_user_new:
            return

        users = self.env['res.users'].sudo().browse(list(per_user_new))
        for user in users:
            # Count no-shows in the rolling window (includes the ones we just
            # flagged — they are already state='no_show' after the write above).
            rolling = Booking.search_count([
                ('organizer_id', '=', user.id),
                ('state', '=', 'no_show'),
                ('start_datetime', '>=', window_start),
            ])
            vals = {
                'parking_no_show_count': (user.parking_no_show_count or 0) + per_user_new[user.id],
                'parking_priority_score': (user.parking_priority_score or 0) - penalty * per_user_new[user.id],
            }
            if policy.ban_threshold and rolling >= policy.ban_threshold:
                ban_until = now + timedelta(days=max(policy.ban_duration_days, 1))
                # Only extend — never shorten — an existing ban.
                if not user.parking_temp_ban_until or user.parking_temp_ban_until < ban_until:
                    vals['parking_temp_ban_until'] = ban_until
            user.write(vals)

    def action_delete_recurrence_series(self):
        """Delete the entire recurrence series (parent + all child occurrences).
        Follows parent_booking_id up to the root, then unlinks — child records
        are removed by the DB cascade on parent_booking_id."""
        roots = self.browse()
        for booking in self:
            root = booking
            while root.parent_booking_id:
                root = root.parent_booking_id
            roots |= root
        roots.with_context(active_test=False).unlink()
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    # ------------------------------------------------------
    # MY BOOKINGS
    # ------------------------------------------------------

    @api.model
    def get_my_bookings_data(self):
        """Return bookings for the current user, bucketed into upcoming/past,
        with KPI counts. Matches the My Bookings client action."""
        user = self.env.user
        employee = self.env['hr.employee'].search([('user_id', '=', user.id)], limit=1)
        domain = ['|',
                  ('organizer_id', '=', user.id),
                  ('employee_id', 'in', employee.ids if employee else [])]

        all_bookings = self.with_context(active_test=False).search(domain, order='start_datetime desc')
        if not all_bookings:
            # Fallback for demos: if the current user has no personal bookings,
            # show all bookings created so the page is not empty.
            all_bookings = self.with_context(active_test=False).search([], order='start_datetime desc', limit=50)

        active_bookings = all_bookings.filtered('active')
        cancelled_bookings = all_bookings - active_bookings

        now = fields.Datetime.now()
        # Pending-approval bookings surface in the Upcoming tab regardless of
        # datetime so the requester can see the request status at a glance.
        pending = active_bookings.filtered(lambda b: b.state == 'pending_approval')
        upcoming = active_bookings.filtered(
            lambda b: b.state != 'pending_approval' and b.stop_datetime >= now
        )
        completed = active_bookings.filtered(
            lambda b: b.state != 'pending_approval' and b.stop_datetime < now
        )

        section_colors = self.env['room.room']._SECTION_COLORS
        today_date = fields.Date.context_today(self)
        tomorrow = today_date + timedelta(days=1)
        yesterday = today_date - timedelta(days=1)
        recurrence_label_map = dict(self._fields['recurrence_type'].selection)

        def _serialize(booking, status):
            office = booking.room_id.office_id
            section_color = section_colors[(office.id or 0) % len(section_colors)]
            start_local = fields.Datetime.context_timestamp(self, booking.start_datetime)
            stop_local = fields.Datetime.context_timestamp(self, booking.stop_datetime)
            d = start_local.date()
            if d == today_date:
                date_label = 'Today'
            elif d == tomorrow:
                date_label = 'Tomorrow'
            elif d == yesterday:
                date_label = 'Yesterday'
            else:
                date_label = start_local.strftime('%b %d, %Y')
            # Walk up to the series root to count siblings.
            root = booking
            while root.parent_booking_id:
                root = root.parent_booking_id
            series_size = 0
            rec_label = ''
            if root.is_recurring or root.recurrence_child_ids:
                series_size = len(root.recurrence_child_ids) + 1
                rec_label = recurrence_label_map.get(root.recurrence_type, 'Recurring')
            # A booking is "check-in-ready" if it's confirmed, its start is in
            # the past (or starting within 10 min), and it hasn't been checked
            # in yet.
            now_dt = fields.Datetime.now()
            can_check_in = (
                booking.state == 'confirmed'
                and booking.start_datetime
                and booking.start_datetime <= now_dt + timedelta(minutes=10)
                and booking.stop_datetime >= now_dt
            )
            can_check_out = booking.state == 'checked_in'
            return {
                'id': booking.id,
                'name': booking.name,
                'slot_name': booking.room_id.name or '',
                'section_name': office.name or '',
                'slot_color': section_color,
                'slot_initials': (booking.room_id.name or '')[:3].upper(),
                'date_label': date_label,
                'time_range': f"{start_local.strftime('%H:%M')} - {stop_local.strftime('%H:%M')}",
                'vehicle_number': booking.vehicle_number or '',
                'employee_name': booking.employee_id.name or booking.organizer_id.name or '',
                'is_recurring': bool(rec_label),
                'recurrence_label': rec_label,
                'is_in_series': series_size > 1,
                'series_size': series_size,
                'status': status,
                'booking_state': booking.state,
                'is_guest_booking': booking.is_guest_booking,
                'guest_name': booking.guest_name or '',
                'guest_vehicle_number': booking.guest_vehicle_number or '',
                'can_check_in': can_check_in,
                'can_check_out': can_check_out,
            }

        # Pending approvals first, then active upcoming — feels natural when
        # the requester opens their list and wants to see "am I approved yet?".
        upcoming_data = (
            [_serialize(b, 'pending') for b in pending.sorted('start_datetime')[:50]]
            + [_serialize(b, 'upcoming') for b in upcoming.sorted('start_datetime')[:50]]
        )
        past_data = (
            [_serialize(b, 'completed') for b in completed[:50]]
            + [_serialize(b, 'cancelled') for b in cancelled_bookings[:50]]
        )
        # A no-show sits in 'completed' bucket (by stop_datetime) but we want
        # it flagged distinctly in the UI.
        for row in past_data:
            if row['booking_state'] == 'no_show':
                row['status'] = 'no_show'

        return {
            'kpi': {
                'total': len(all_bookings),
                'upcoming': len(upcoming) + len(pending),
                'completed': len(completed),
                'cancelled': len(cancelled_bookings),
                'pending_approval': len(pending),
            },
            'upcoming': upcoming_data,
            'past': past_data,
        }

    # ------------------------------------------------------
    # ANALYTICS
    # ------------------------------------------------------

    _ANALYTICS_SECTION_COLORS = ['#3B82F6', '#8B5CF6', '#14B8A6', '#F59E0B', '#EF4444', '#EC4899', '#0EA5E9', '#84CC16']

    @api.model
    def get_parking_analytics_data(self, range_days=7, office_id=None):
        """Aggregate numbers for the analytics dashboard client action."""
        range_days = max(int(range_days or 7), 1)
        today_date = fields.Date.context_today(self)
        today_dt = fields.Datetime.to_datetime(today_date)
        tomorrow_dt = today_dt + timedelta(days=1)
        range_start = today_dt - timedelta(days=range_days)

        Room = self.env['room.room']
        slot_domain = [('active', '=', True)]
        if office_id:
            slot_domain.append(('office_id', '=', int(office_id)))
        total_slots = Room.search_count(slot_domain)
        scoped_slot_ids = Room.search(slot_domain).ids
        working_hours_per_day = 10  # baseline for utilization math

        booking_domain = [
            ('start_datetime', '>=', range_start),
            ('start_datetime', '<', tomorrow_dt),
        ]
        if office_id:
            booking_domain.append(('room_id', 'in', scoped_slot_ids))
        bookings = self.search(booking_domain)
        total_bookings = len(bookings)

        total_hours = 0.0
        for booking in bookings:
            total_hours += (booking.stop_datetime - booking.start_datetime).total_seconds() / 3600.0
        capacity_hours = total_slots * range_days * working_hours_per_day
        utilization = round(total_hours * 100 / capacity_hours) if capacity_hours else 0
        avg_duration = round(total_hours / total_bookings, 1) if total_bookings else 0
        active_users = len(bookings.mapped('employee_id')) or len(bookings.mapped('organizer_id'))

        # --- Peak usage hours (average vehicles present per hour of the day) ---
        hour_totals = [0] * 24
        for booking in bookings:
            start_h = booking.start_datetime.hour
            end_h = booking.stop_datetime.hour
            for h in range(start_h, min(end_h + 1, 24)):
                hour_totals[h] += 1
        peak_labels = []
        peak_values = []
        for h in range(7, 19):
            label = f"{h if h <= 12 else h - 12}{'am' if h < 12 else 'pm'}"
            if h == 12:
                label = '12pm'
            peak_labels.append(label)
            peak_values.append(round(hour_totals[h] / range_days, 1))

        # --- Monthly booking trend (last 7 months, earliest first) ---
        trend_labels = []
        trend_values = []
        first_of_month = today_date.replace(day=1)
        for i in range(6, -1, -1):
            month_start = first_of_month - relativedelta(months=i)
            month_end = month_start + relativedelta(months=1)
            month_domain = [
                ('start_datetime', '>=', fields.Datetime.to_datetime(month_start)),
                ('start_datetime', '<', fields.Datetime.to_datetime(month_end)),
            ]
            if office_id:
                month_domain.append(('room_id', 'in', scoped_slot_ids))
            count = self.search_count(month_domain)
            trend_labels.append(month_start.strftime('%b'))
            trend_values.append(count)

        # --- Weekly occupancy rate (hours used ÷ hours available per weekday) ---
        weekday_hours_used = [0.0] * 7
        weekday_hours_avail = [0.0] * 7
        iter_day = range_start.date()
        while iter_day < today_date:
            weekday_hours_avail[iter_day.weekday()] += total_slots * working_hours_per_day
            iter_day += timedelta(days=1)
        for booking in bookings:
            weekday_hours_used[booking.start_datetime.weekday()] += (
                booking.stop_datetime - booking.start_datetime
            ).total_seconds() / 3600.0
        weekly_labels = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        weekly_values = [
            round(weekday_hours_used[i] * 100 / weekday_hours_avail[i]) if weekday_hours_avail[i] else 0
            for i in range(7)
        ]

        # --- Usage by section (donut) ---
        section_counts = {}
        for booking in bookings:
            office = booking.room_id.office_id
            key = office.id or 0
            bucket = section_counts.setdefault(key, {
                'name': office.name or _('Unassigned'),
                'count': 0,
            })
            bucket['count'] += 1
        sorted_sections = sorted(section_counts.values(), key=lambda s: -s['count'])
        section_total = sum(s['count'] for s in sorted_sections) or 1
        for idx, sec in enumerate(sorted_sections):
            sec['color'] = self._ANALYTICS_SECTION_COLORS[idx % len(self._ANALYTICS_SECTION_COLORS)]
            sec['percent'] = round(sec['count'] * 100 / section_total)

        # --- Top parking users ---
        user_map = {}
        for booking in bookings:
            emp = booking.employee_id
            if not emp:
                continue
            bucket = user_map.setdefault(emp.id, {
                'id': emp.id,
                'name': emp.name,
                'department': emp.department_id.name or '—',
                'initials': ''.join(part[:1] for part in (emp.name or '').split()[:2]).upper() or '•',
                'count': 0,
                'hours': 0.0,
            })
            bucket['count'] += 1
            bucket['hours'] += (booking.stop_datetime - booking.start_datetime).total_seconds() / 3600.0
        top_users = sorted(user_map.values(), key=lambda u: -u['count'])[:5]
        max_count = top_users[0]['count'] if top_users else 1
        user_colors = ['#EC4899', '#3B82F6', '#14B8A6', '#8B5CF6', '#F59E0B']
        for idx, user in enumerate(top_users):
            user['avg_duration'] = round(user['hours'] / user['count'], 1) if user['count'] else 0
            user['utilization_pct'] = round(user['count'] * 100 / max_count) if max_count else 0
            user['color'] = user_colors[idx % len(user_colors)]

        # Denominator: bookings that were "supposed to happen" in the range
        # (anything not cancelled and whose stop_datetime is in the past).
        now_ = fields.Datetime.now()
        terminal_bookings = bookings.filtered(
            lambda b: b.state in ('completed', 'no_show', 'checked_in') and b.stop_datetime and b.stop_datetime <= now_
        )
        no_show_bookings = terminal_bookings.filtered(lambda b: b.state == 'no_show')
        no_show_rate = round(len(no_show_bookings) * 100 / len(terminal_bookings)) if terminal_bookings else 0

        # Repeat-offender table: top 5 users by no-show count in the range.
        offender_map = {}
        for b in no_show_bookings:
            emp = b.employee_id
            key = emp.id if emp else ('org', b.organizer_id.id)
            name = emp.name if emp else (b.organizer_id.name or '—')
            dept = (emp.department_id.name if emp and emp.department_id else '—')
            initials = ''.join(p[:1] for p in (name or '').split()[:2]).upper() or '•'
            offender_map.setdefault(key, {
                'name': name, 'department': dept, 'initials': initials,
                'no_show_count': 0, 'total_bookings': 0,
            })
            offender_map[key]['no_show_count'] += 1

        # Fill in total bookings per offender (for rate calc).
        for b in bookings:
            emp = b.employee_id
            key = emp.id if emp else ('org', b.organizer_id.id)
            if key in offender_map:
                offender_map[key]['total_bookings'] += 1

        offender_palette = ['#EF4444', '#F97316', '#F59E0B', '#EC4899', '#8B5CF6']
        top_offenders = sorted(offender_map.values(), key=lambda u: -u['no_show_count'])[:5]
        for idx, u in enumerate(top_offenders):
            u['rate'] = round(u['no_show_count'] * 100 / u['total_bookings']) if u['total_bookings'] else 0
            u['color'] = offender_palette[idx % len(offender_palette)]

        return {
            'range_days': range_days,
            'kpi': {
                'total_bookings': total_bookings,
                'utilization': utilization,
                'active_users': active_users,
                'avg_duration': avg_duration,
            },
            'peak_hours': {'labels': peak_labels, 'values': peak_values},
            'monthly_trend': {'labels': trend_labels, 'values': trend_values},
            'weekly_occupancy': {'labels': weekly_labels, 'values': weekly_values},
            'usage_by_section': sorted_sections,
            'top_users': top_users,
            'no_show': {
                'count': len(no_show_bookings),
                'total_terminal': len(terminal_bookings),
                'rate_pct': no_show_rate,
            },
            'no_show_offenders': top_offenders,
        }

    # ------------------------------------------------------
    # QUICK BOOK — one-shot API for the Phase 4 UI
    # ------------------------------------------------------

    @api.model
    def action_quick_book(self, vals):
        """One-call booking for the Quick Book dialog.

        ``vals`` must include ``start_datetime`` and ``stop_datetime`` (ISO-8601
        strings or naive-UTC datetimes). Optional keys: ``vehicle_number``,
        ``prefer_ev``, ``prefer_accessible``, ``prefer_office_id``,
        ``prefer_zone``, ``prefer_floor``, ``name``.

        The allocator picks a slot, all existing constraints fire (EV caps,
        bans, premium approval, etc.), and the returned payload tells the UI
        exactly what happened so it can render a rich confirmation toast
        without a second round-trip.
        """
        clean = dict(vals or {})
        start = clean.pop('start_datetime', None)
        stop = clean.pop('stop_datetime', None)
        if isinstance(start, str):
            start = fields.Datetime.from_string(start.replace('T', ' ')[:19])
        if isinstance(stop, str):
            stop = fields.Datetime.from_string(stop.replace('T', ' ')[:19])
        if not (start and stop) or start >= stop:
            raise ValidationError(_("Please pick a valid time window."))

        ctx = {
            'parking_prefer_ev': bool(clean.pop('prefer_ev', False)),
            'parking_prefer_accessible': bool(clean.pop('prefer_accessible', False)),
        }
        office = clean.pop('prefer_office_id', None)
        if office:
            ctx['parking_prefer_office_id'] = int(office)
        zone = clean.pop('prefer_zone', None)
        if zone:
            ctx['parking_prefer_zone'] = zone
        floor = clean.pop('prefer_floor', None)
        if floor:
            ctx['parking_prefer_floor'] = floor

        title = (clean.pop('name', None) or '').strip() or _("Quick booking")
        employee = self.env['hr.employee'].search(
            [('user_id', '=', self.env.uid)], limit=1,
        )
        booking_vals = {
            'name': title,
            'start_datetime': start,
            'stop_datetime': stop,
            'organizer_id': self.env.uid,
        }
        if employee:
            booking_vals['employee_id'] = employee.id
        if clean.get('vehicle_number'):
            booking_vals['vehicle_number'] = clean['vehicle_number']

        booking = self.with_context(**ctx).create(booking_vals)
        # The allocator may have failed silently (required-field would raise
        # before we reach here, so if we have a row, a slot was picked).
        return {
            'id': booking.id,
            'name': booking.name,
            'slot_name': booking.room_id.name or '',
            'office_name': booking.room_id.office_id.name or '',
            'start_datetime': fields.Datetime.to_string(booking.start_datetime),
            'stop_datetime': fields.Datetime.to_string(booking.stop_datetime),
            'state': booking.state,
            'needs_approval': booking.state == 'pending_approval',
            'is_ev': booking.room_id.is_ev_charger,
            'is_accessible': booking.room_id.is_accessible,
            'is_premium': booking.room_id.is_premium,
        }

    # ------------------------------------------------------
    # ANALYTICS EXTRAS (Phase 4 charts)
    # ------------------------------------------------------

    @api.model
    def get_parking_heatmap_data(self, range_days=14, office_id=None):
        """Hour (0-23) × weekday (Mon-Sun) booking-count matrix.

        Returns ``{'matrix': [[...x24]x7], 'max': int}``.
        The matrix is sparse and tiny (168 cells) so the client can render
        a heatmap grid in a single pass without further aggregation.
        """
        range_days = max(int(range_days or 14), 1)
        today_dt = fields.Datetime.to_datetime(fields.Date.context_today(self))
        range_start = today_dt - timedelta(days=range_days)
        domain = [
            ('start_datetime', '>=', range_start),
            ('start_datetime', '<', today_dt + timedelta(days=1)),
        ]
        if office_id:
            scoped_ids = self.env['room.room'].search([('active', '=', True), ('office_id', '=', int(office_id))]).ids
            domain.append(('room_id', 'in', scoped_ids))
        bookings = self.search(domain)
        matrix = [[0] * 24 for _ in range(7)]
        for b in bookings:
            if not b.start_datetime:
                continue
            matrix[b.start_datetime.weekday()][b.start_datetime.hour] += 1
        peak = max((v for row in matrix for v in row), default=0)
        return {
            'range_days': range_days,
            'matrix': matrix,
            'max': peak,
        }



    @api.model
    def admin_bulk_cancel(self, booking_ids):
        """Manager-only: cancel any number of bookings at once. Each still
        goes through ``action_cancel`` so waitlist promotion and bus events
        fire normally."""
        if not self.env.user.has_group('room.group_parking_manager'):
            raise ValidationError(_("Only Parking Managers can bulk-cancel."))
        bookings = self.browse([int(bid) for bid in (booking_ids or [])]).exists()
        bookings.action_cancel()
        return len(bookings)

    # ------------------------------------------------------
    # PHASE 5 — Intelligence & Automation
    # ------------------------------------------------------

    @api.model
    def _cron_send_reminders(self):
        """Send 30-minute-ahead reminders for confirmed bookings.

        Runs every 15 min. The 25–40 min window gives two cron passes to catch
        the booking; ``reminder_sent`` ensures only one notification fires.
        """
        now = fields.Datetime.now()
        window_start = now + timedelta(minutes=25)
        window_end = now + timedelta(minutes=40)
        due = self.search([
            ('state', 'in', ('confirmed', 'checked_in')),
            ('start_datetime', '>=', window_start),
            ('start_datetime', '<=', window_end),
            ('reminder_sent', '=', False),
        ])
        if not due:
            return
        notif = Notifications(self.env)
        for booking in due:
            try:
                notif.booking_reminder(booking)
                # Inline chatter note so the user sees it in the booking record.
                booking.message_post(
                    body=_("Reminder sent — your booking starts in about 30 minutes."),
                    message_type='notification',
                )
            except Exception:
                _logger.exception("Reminder failed for booking %s", booking.id)
        due.write({'reminder_sent': True})

    @api.model
    def get_demand_forecast(self, days=7, office_id=None):
        """Predict per-day booking demand for the next ``days`` days.

        Uses the same weekday's bookings over the past 4 weeks as the baseline.
        Returns a list of ``{date, label, predicted, capacity}`` dicts ordered
        by date asc. ``capacity`` is the total active slot count.
        """
        days = max(int(days or 7), 1)
        today = fields.Date.context_today(self)
        today_dt = fields.Datetime.to_datetime(today)
        slot_domain = [('active', '=', True)]
        if office_id:
            slot_domain.append(('office_id', '=', int(office_id)))
        scoped_slot_ids = self.env['room.room'].search(slot_domain).ids
        capacity = len(scoped_slot_ids)

        history_start = today_dt - timedelta(weeks=4)
        history_domain = [
            ('start_datetime', '>=', history_start),
            ('start_datetime', '<', today_dt),
            ('state', 'not in', ('cancelled', 'no_show')),
        ]
        if office_id:
            history_domain.append(('room_id', 'in', scoped_slot_ids))
        history = self.search(history_domain)
        # weekday (0=Mon) → count
        weekday_counts = {}
        weekday_weeks = {}
        for b in history:
            wd = b.start_datetime.weekday()
            weekday_counts[wd] = weekday_counts.get(wd, 0) + 1
            weekday_weeks.setdefault(wd, set()).add(b.start_datetime.isocalendar()[1])

        result = []
        for delta in range(days):
            d = today + timedelta(days=delta)
            wd = d.weekday()
            weeks_seen = len(weekday_weeks.get(wd, set())) or 1
            predicted = round(weekday_counts.get(wd, 0) / weeks_seen)
            result.append({
                'date': fields.Date.to_string(d),
                'label': d.strftime('%a %d'),
                'predicted': predicted,
                'capacity': capacity,
            })
        return result

    @api.model
    def get_my_calendar_month_data(self, year, month):
        """Return snapshot counts + per-day booking data for the given month."""
        import calendar as _cal
        import pytz

        user = self.env.user
        tz_name = self.env.context.get('tz') or user.tz or 'UTC'
        user_tz = pytz.timezone(tz_name)

        last_day = _cal.monthrange(year, month)[1]
        utc_start = user_tz.localize(datetime(year, month, 1)).astimezone(pytz.UTC).replace(tzinfo=None)
        utc_end = user_tz.localize(datetime(year, month, last_day, 23, 59, 59)).astimezone(pytz.UTC).replace(tzinfo=None)

        employee = self.env['hr.employee'].search([('user_id', '=', user.id)], limit=1)
        domain = [
            '|',
            ('organizer_id', '=', user.id),
            ('employee_id', 'in', employee.ids if employee else []),
            ('start_datetime', '>=', fields.Datetime.to_string(utc_start)),
            ('start_datetime', '<=', fields.Datetime.to_string(utc_end)),
        ]
        bookings = self.with_context(active_test=False).search(domain)

        state_key = {
            'confirmed': 'confirmed', 'checked_in': 'checked_in',
            'cancelled': 'cancelled', 'no_show': 'no_show',
            'pending_approval': 'pending',
        }
        snapshot = {'confirmed': 0, 'checked_in': 0, 'cancelled': 0, 'no_show': 0, 'pending': 0}
        days = {}

        for b in bookings:
            key = state_key.get(b.state)
            if key:
                snapshot[key] += 1
            start_local = pytz.UTC.localize(b.start_datetime).astimezone(user_tz)
            stop_local = pytz.UTC.localize(b.stop_datetime).astimezone(user_tz)
            day_key = start_local.strftime('%Y-%m-%d')
            days.setdefault(day_key, []).append({
                'id': b.id,
                'state': b.state,
                'slot': b.room_id.name or '',
                'time': f"{start_local.strftime('%H:%M')} - {stop_local.strftime('%H:%M')}",
            })

        return {'snapshot': snapshot, 'days': days}

    @api.model
    def get_my_booking_patterns(self):
        """Analyse the current user's last 60 days of bookings.

        Returns::

            {
              'total': int,
              'preferred_days': [str],   # e.g. ['Mon', 'Wed']
              'preferred_hour': str,     # e.g. '9:00 – 12:00'
              'preferred_zone': str | None,
              'preferred_floor': str | None,
              'no_show_rate': float,     # 0-100
              'suggestion': str,         # human-readable hint
            }

        Returns ``None`` when the user has fewer than 3 bookings (not enough
        signal to surface patterns).
        """
        uid = self.env.uid
        since = fields.Datetime.now() - timedelta(days=60)
        bookings = self.search([
            ('organizer_id', '=', uid),
            ('start_datetime', '>=', since),
        ])
        if len(bookings) < 3:
            return None

        DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        HOUR_BUCKETS = [
            (6, 9, 'Early morning (6-9)'),
            (9, 12, 'Morning (9-12)'),
            (12, 14, 'Midday (12-14)'),
            (14, 17, 'Afternoon (14-17)'),
            (17, 22, 'Evening (17-22)'),
        ]

        day_counts = {}
        bucket_counts = {}
        zone_counts = {}
        floor_counts = {}
        no_show_count = 0

        for b in bookings:
            if not b.start_datetime:
                continue
            wd = b.start_datetime.weekday()
            day_counts[wd] = day_counts.get(wd, 0) + 1
            hr = b.start_datetime.hour
            for lo, hi, label in HOUR_BUCKETS:
                if lo <= hr < hi:
                    bucket_counts[label] = bucket_counts.get(label, 0) + 1
                    break
            if b.room_id.zone:
                zone_counts[b.room_id.zone] = zone_counts.get(b.room_id.zone, 0) + 1
            if b.room_id.floor:
                floor_counts[b.room_id.floor] = floor_counts.get(b.room_id.floor, 0) + 1
            if b.state == 'no_show':
                no_show_count += 1

        max_day_count = max(day_counts.values(), default=0)
        preferred_days = sorted(
            [DAY_NAMES[wd] for wd, cnt in day_counts.items() if cnt >= max(1, max_day_count - 1)],
            key=lambda d: DAY_NAMES.index(d),
        )
        preferred_hour = max(bucket_counts, key=bucket_counts.get) if bucket_counts else None
        preferred_zone = max(zone_counts, key=zone_counts.get) if zone_counts else None
        preferred_floor = max(floor_counts, key=floor_counts.get) if floor_counts else None
        no_show_rate = round(no_show_count / len(bookings) * 100, 1)

        # Build a natural-language suggestion sentence.
        parts = []
        if preferred_days:
            parts.append(_("You usually park on %(days)s", days=', '.join(preferred_days)))
        if preferred_hour:
            parts.append(_("%(time)s", time=preferred_hour))
        if preferred_zone:
            parts.append(_("in zone %(zone)s", zone=preferred_zone))
        suggestion = ' · '.join(parts) if parts else _("Keep booking to unlock personalised tips.")

        return {
            'total': len(bookings),
            'preferred_days': preferred_days,
            'preferred_hour': preferred_hour,
            'preferred_zone': preferred_zone,
            'preferred_floor': preferred_floor,
            'no_show_rate': no_show_rate,
            'suggestion': suggestion,
        }

    @api.model
    def get_empty_list_help(self, help_message):
        result_help_message = super().get_empty_list_help(help_message)
        if self.env.user.has_group('room.group_parking_manager') and not self.env["room.room"].search_count([]):
            result_help_message += Markup('<a class="btn btn-outline-primary" href="/odoo/parking-slots/new">%s</a>') % _("Create a Parking Slot")
        return result_help_message

    # ---------------------------------------------------------------
    # Real-time dashboard bus channel
    # ---------------------------------------------------------------
    # Payload contract (keep this doc in sync with the OWL subscriber):
    #   {
    #     "event":      "created" | "updated" | "cancelled" | "checked_in"
    #                 | "checked_out" | "no_show" | "promoted",
    #     "booking_id": int,
    #     "room_id":    int,
    #     "office_id":  int | null,
    #     "state":      str,
    #     "ts":         ISO-8601 UTC,
    #   }
    # The channel name is shared across all employees so the OWL dashboard can
    # subscribe with a single addChannel call. Sensitive details (organiser
    # names, vehicle numbers) are deliberately *not* broadcast — clients
    # re-fetch via the existing dashboard RPCs when they see an event.
    # ---------------------------------------------------------------
    # Outlook sync plumbing
    # ---------------------------------------------------------------

    def _arm_outlook_sync(self):
        """Flag these bookings for the async sync cron. Called from create
        and from write when any user-visible field changes. Skipped when the
        global kill switch is off so we don't pile up pending rows for no
        reason."""
        if not self:
            return
        sync_on = str(self.env['ir.config_parameter'].sudo().get_param(
            'room.parking_outlook_sync_enabled', 'False'
        )).strip().lower() in ('1', 'true', 'yes')
        if not sync_on:
            return
        # Only arm rows that aren't already pending — avoids a duplicate
        # chatter note on every write.
        stale = self.filtered(lambda b: b.last_sync_status != 'pending')
        if stale:
            stale.sudo().write({'last_sync_status': 'pending'})

    def action_resync_outlook(self):
        """Manual retry button on the booking form. Runs the sync inline so
        the user gets immediate feedback."""
        self.ensure_one()
        GraphService(self.env).sync_booking(self)
        return True

    @api.model
    def _cron_sync_outlook(self, limit=50):
        """Async Outlook sync loop. Each pass processes up to ``limit``
        bookings in the ``pending`` state, oldest first, so large backlogs
        drain predictably without hogging the worker. Failures are recorded
        on the row and retried on the next pass."""
        pending = self.search(
            [('last_sync_status', '=', 'pending')],
            order='write_date asc',
            limit=limit,
        )
        if not pending:
            return 0
        graph = GraphService(self.env)
        for booking in pending:
            graph.sync_booking(booking)
        return len(pending)

    @api.model
    def _cron_pull_outlook_cancellations(self):
        """Pull deleted/cancelled events from Outlook and cancel the
        corresponding parking bookings. Runs every 15 minutes.
        Only active when parking_outlook_pull_enabled = True in Settings."""
        graph = GraphService(self.env)
        count = graph.pull_outlook_cancellations()
        if count:
            _logger.info("Outlook pull: cancelled %d parking booking(s) from Outlook deletions.", count)

    _DASHBOARD_BUS_CHANNEL = "parking/dashboard"

    def _notify_dashboard(self, event):
        """Emit a lightweight state-change event to the parking dashboard bus
        channel. Safe to call in bulk — one send per record.

        Callers are the handful of places where booking state actually changes
        (create, cancel, check-in/out, no-show cron, waitlist promotion).
        """
        if not self:
            return
        now = fields.Datetime.now()
        bus = self.env["bus.bus"].sudo()
        for booking in self:
            bus._sendone(
                self._DASHBOARD_BUS_CHANNEL,
                "parking/booking",
                {
                    "event": event,
                    "booking_id": booking.id,
                    "room_id": booking.room_id.id,
                    "office_id": booking.room_id.office_id.id or None,
                    "state": booking.state,
                    "ts": now.isoformat() if now else None,
                },
            )
