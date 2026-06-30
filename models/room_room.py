# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

import base64
import io
import re
from datetime import datetime, time, timedelta
from uuid import uuid4

import pytz

from odoo import api, fields, models, _
from odoo.tools.translate import html_translate


def _slot_sort_key(name):
    """Sort A1, A2, A10 naturally (not A1, A10, A2)."""
    m = re.match(r'^\s*([A-Za-z]*)(\d+)?(.*)$', name or '')
    if not m:
        return (name or '', 0, '')
    return (m.group(1), int(m.group(2)) if m.group(2) else 0, m.group(3))


class RoomRoom(models.Model):
    _name = 'room.room'
    _inherit = ["mail.thread"]
    _description = "Room"
    _order = "name, id"

    SLOT_TYPE_SELECTION = [
        ('standard', 'Standard Parking'),
        ('visitor', 'Visitor Parking'),
        ('ev_charging', 'EV Charging'),
        ('executive', 'Executive Reserved'),
    ]

    # Configuration
    name = fields.Char(string="Parking Slot", required=True, tracking=2)
    active = fields.Boolean("Active", default=True)
    description = fields.Html(string="Amenities", translate=html_translate)
    location = fields.Char(string="Parking Location")
    slot_type = fields.Selection(
        SLOT_TYPE_SELECTION,
        string="Slot Type",
        default='standard',
        required=True,
        tracking=True,
        help="Classification of this parking slot.",
    )
    is_ev_charger = fields.Boolean(string="EV Charger")
    is_accessible = fields.Boolean(string="Accessibility")
    is_premium = fields.Boolean(string="Premium (VIP)")
    allowed_group_ids = fields.Many2many(
        "res.groups",
        "room_room_res_groups_rel",
        "room_id",
        "group_id",
        string="Allowed Groups",
        help="If set, only users in one of these groups can book this parking slot. "
             "Leave empty to allow any internal user.",
    )
    office_id = fields.Many2one("room.office", string="Office", required=True, tracking=3)
    # Coarse-grained locators used by the smart allocator's "nearest slot"
    # fallback: same zone → same floor → anything in the office. Both are
    # free-form text so a deployment can use whatever labels it already prints
    # on the ground.
    zone = fields.Char(string="Zone", index=True, help="Logical cluster within the office, e.g. 'North', 'Visitor', 'EV'.")
    floor = fields.Char(string="Floor", index=True, help="Floor or level label, e.g. 'B1', 'B2', 'Ground'.")
    # Per-slot overrides for EV policy. Null means 'inherit from parking.policy'.
    ev_cooldown_minutes = fields.Integer(
        string="EV Cooldown (minutes)",
        help="Minimum gap between two consecutive bookings on this EV slot. 0 = inherit global policy.",
    )
    ev_max_hours_override = fields.Float(
        string="EV Max Hours Override",
        help="Per-slot cap on booking duration for this EV charger. 0 = inherit global policy.",
    )
    room_properties = fields.Properties("Properties", definition="office_id.room_properties_definition")
    company_id = fields.Many2one(related="office_id.company_id", string="Company", store=True)
    room_booking_ids = fields.One2many("room.booking", "room_id", string="Bookings")
    short_code = fields.Char("Short Code", default=lambda self: str(uuid4())[:8], copy=False, required=True, tracking=1)
    # Technical/Statistics
    access_token = fields.Char("Access Token", default=lambda self: str(uuid4()), copy=False, readonly=True, required=True)
    is_available = fields.Boolean(string="Is Room Currently Available", compute="_compute_is_available", search="_search_is_available")
    availability_status = fields.Selection(
        [('free', 'Free'), ('busy', 'Busy')],
        string="Status",
        compute="_compute_availability_status",
    )
    next_booking_start = fields.Datetime("Next Booking Start", compute="_compute_next_booking_start")
    current_occupant_id = fields.Many2one("res.users", string="Current Occupant", compute="_compute_current_booking", store=False)
    current_vehicle = fields.Char(string="Vehicle", compute="_compute_current_booking", store=False)
    room_booking_url = fields.Char("Room Link", compute="_compute_room_booking_url")
    # Frontend design fields
    bookable_background_color = fields.Char("Available Background Color", default="#83c5be")
    booked_background_color = fields.Char("Booked Background Color", default="#dd2d4a")
    room_background_image = fields.Image("Background Image")

    _uniq_access_token = models.Constraint(
        'unique(access_token)',
        "The access token must be unique",
    )
    _uniq_short_code = models.Constraint(
        'unique(short_code)',
        "The short code must be unique.",
    )

    @api.depends("office_id")
    def _compute_display_name(self):
        super()._compute_display_name()
        for room in self.filtered(lambda room: room.name and room.office_id):
            room.display_name = f"{room.office_id.name} - {room.name}"

    @api.depends("room_booking_ids")
    def _compute_is_available(self):
        now = fields.Datetime.now()
        booked_rooms = {room.id for room, in self.env["room.booking"]._read_group(
            [("start_datetime", "<=", now), ("stop_datetime", ">=", now), ("room_id", "in", self.ids)],
            ["room_id"],
        )}
        for room in self:
            room.is_available = room.id not in booked_rooms

    @api.depends("is_available")
    def _compute_availability_status(self):
        for room in self:
            room.availability_status = 'free' if room.is_available else 'busy'

    def _search_is_available(self, operator, value):
        now = fields.Datetime.now()
        booked_ids = [r.id for r, in self.env["room.booking"]._read_group(
            [("start_datetime", "<=", now), ("stop_datetime", ">=", now),
             ("state", "in", ("confirmed", "checked_in", "pending_approval"))],
            ["room_id"],
        )]
        if (operator == '=' and value) or (operator == '!=' and not value):
            return [('id', 'not in', booked_ids)]
        return [('id', 'in', booked_ids)]

    @api.depends("room_booking_ids", "is_available")
    def _compute_current_booking(self):
        now = fields.Datetime.now()
        for room in self:
            if room.is_available:
                room.current_occupant_id = False
                room.current_vehicle = False
            else:
                current = room.room_booking_ids.filtered(
                    lambda b: b.start_datetime <= now <= b.stop_datetime
                    and b.state in ('confirmed', 'checked_in')
                )
                booking = current[:1]
                room.current_occupant_id = booking.organizer_id if booking else False
                room.current_vehicle = booking.vehicle_number if booking else False

    @api.depends("is_available", "room_booking_ids")
    def _compute_next_booking_start(self):
        now = fields.Datetime.now()
        next_booking_start_by_room = dict(self.env["room.booking"]._read_group(
            [("start_datetime", ">", now), ("room_id", "in", self.filtered('is_available').ids)],
            ["room_id"],
            ["start_datetime:min"],
        ))
        for room in self:
            room.next_booking_start = next_booking_start_by_room.get(room)

    @api.depends("short_code")
    def _compute_room_booking_url(self):
        for room in self:
            room.room_booking_url = f"{room.get_base_url()}/room/{room.short_code}/book"

    # ------------------------------------------------------
    # QR
    # ------------------------------------------------------

    def _get_booking_qr_data_uri(self):
        """Return a data:image/png;base64,... URI containing a QR code that
        points to this slot's public booking URL. Used by the tablet kiosk
        sidebar so a visitor can scan with their phone and finish their booking
        from their device."""
        self.ensure_one()
        try:
            import qrcode
            from qrcode.image.pil import PilImage
        except ImportError:
            return ''
        url = self.room_booking_url or ''
        if not url:
            return ''
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=6,
            border=2,
        )
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#0F172A", back_color="#FFFFFF")
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        b64 = base64.b64encode(buf.getvalue()).decode('ascii')
        return 'data:image/png;base64,%s' % b64

    # ------------------------------------------------------
    # CRUD / ORM
    # ------------------------------------------------------

    def write(self, vals):
        result = super().write(vals)
        for room in self:
            room._notify_booking_view("reload")
        return result

    # ------------------------------------------------------
    # ACTIONS
    # ------------------------------------------------------

    def action_open_booking_view(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_url",
            "url": self.room_booking_url,
            "target": "new",
        }

    def action_view_bookings(self):
        return {
            "type": "ir.actions.act_window",
            "res_model": "room.booking",
            "name": _("Bookings"),
            "domain": [("room_id", "in", self.ids)],
            "context": {"default_room_id": self.id if len(self) == 1 else False},
            "view_mode": "calendar,gantt,kanban,list,form",
        }

    @api.model
    def action_open_parking_booking_form(self, slot_id=None):
        context = {}
        if slot_id:
            context['default_room_id'] = slot_id
        return {
            "type": "ir.actions.act_window",
            "res_model": "room.booking",
            "name": _("New Parking Booking"),
            "views": [[False, "form"]],
            "target": "new",
            "context": context,
        }

    @api.model
    def get_admin_panel_data(self, status_filter='all', section_filter='all', search=''):
        """Return the data for the Admin Panel client action: KPIs, slot rows
        (with live status + current booking), section list for filters, and
        a recent-bookings list for the Booking Overrides tab."""
        now = fields.Datetime.now()
        today_start = fields.Datetime.to_datetime(fields.Date.context_today(self))
        today_end = today_start + timedelta(days=1)

        domain = [('active', '=', True)]
        if search:
            domain += ['|', ('name', 'ilike', search), ('location', 'ilike', search)]
        if section_filter and section_filter != 'all':
            try:
                domain += [('office_id', '=', int(section_filter))]
            except (TypeError, ValueError):
                pass

        slots = self.search(domain)
        slots = slots.sorted(key=lambda s: (s.office_id.name or '', _slot_sort_key(s.name)))

        bookings_today = self.env['room.booking'].search([
            ('room_id', 'in', slots.ids),
            ('stop_datetime', '>=', today_start),
            ('start_datetime', '<', today_end),
        ])
        bookings_by_slot = bookings_today.grouped('room_id')

        def _fmt(dt):
            if not dt:
                return ''
            return fields.Datetime.context_timestamp(self, dt).strftime('%H:%M')

        all_rows = []
        total_available = total_occupied = total_reserved = 0
        for slot in slots:
            slot_bookings = bookings_by_slot.get(slot, self.env['room.booking'])
            current = slot_bookings.filtered(lambda b: b.start_datetime <= now <= b.stop_datetime)
            upcoming = slot_bookings.filtered(lambda b: b.start_datetime > now).sorted('start_datetime')
            if current:
                status = 'occupied'
                booking = current[:1]
                total_occupied += 1
            elif upcoming:
                status = 'reserved'
                booking = upcoming[:1]
                total_reserved += 1
            else:
                status = 'available'
                booking = self.env['room.booking']
                total_available += 1
            booker_name = booking.employee_id.name or booking.organizer_id.name or ''
            all_rows.append({
                'id': slot.id,
                'name': slot.name,
                'section_id': slot.office_id.id,
                'section_name': slot.office_id.name or '',
                'status': status,
                'location': slot.location or '',
                'is_ev': slot.is_ev_charger,
                'is_accessible': slot.is_accessible,
                'is_premium': slot.is_premium,
                'booker_name': booker_name,
                'booker_initials': ''.join(p[:1] for p in (booker_name or '').split()[:2]).upper(),
                'time_range': f"{_fmt(booking.start_datetime)} - {_fmt(booking.stop_datetime)}" if booking else '',
            })

        if status_filter and status_filter != 'all':
            all_rows = [r for r in all_rows if r['status'] == status_filter]

        total_all = self.search_count([('active', '=', True)])
        occupied_all = len({b.room_id.id for b in bookings_today
                            if b.start_datetime <= now <= b.stop_datetime})
        reserved_all = len({b.room_id.id for b in bookings_today
                            if b.start_datetime > now})

        sections = [{'id': o.id, 'name': o.name}
                    for o in self.env['room.office'].search([], order='name')]

        # Booking Overrides tab: recent + upcoming bookings.
        overrides = self.env['room.booking'].search([
            ('start_datetime', '>=', today_start - timedelta(days=2)),
        ], order='start_datetime asc', limit=100)
        override_rows = []
        for b in overrides:
            start_local = fields.Datetime.context_timestamp(self, b.start_datetime) if b.start_datetime else None
            stop_local = fields.Datetime.context_timestamp(self, b.stop_datetime) if b.stop_datetime else None
            if b.stop_datetime and b.stop_datetime < now:
                b_status = 'completed'
            elif b.start_datetime and b.start_datetime <= now <= b.stop_datetime:
                b_status = 'occupied'
            else:
                b_status = 'upcoming'
            override_rows.append({
                'id': b.id,
                'name': b.name,
                'slot_name': b.room_id.name,
                'section_name': b.room_id.office_id.name or '',
                'booker_name': b.employee_id.name or b.organizer_id.name or '',
                'vehicle_number': b.vehicle_number or '',
                'start_label': start_local.strftime('%b %d, %H:%M') if start_local else '',
                'stop_label': stop_local.strftime('%H:%M') if stop_local else '',
                'status': b_status,
                'active': b.active,
            })

        return {
            'kpi': {
                'total': total_all,
                'available': max(total_all - occupied_all - reserved_all, 0),
                'occupied': occupied_all,
                'reserved': reserved_all,
            },
            'slots': all_rows,
            'sections': sections,
            'overrides': override_rows,
            'is_admin': self.env.user.has_group('base.group_system'),
        }

    @api.model
    def admin_toggle_slot_active(self, slot_id):
        slot = self.browse(int(slot_id))
        slot.active = not slot.active
        return True

    @api.model
    def admin_bulk_toggle_active(self, slot_ids, active):
        """Manager-only bulk block/unblock. ``active`` is True to unblock,
        False to take slots offline."""
        if not self.env.user.has_group('room.group_parking_manager'):
            from odoo.exceptions import ValidationError
            raise ValidationError(_("Only Parking Managers can bulk-block slots."))
        slots = self.browse([int(s) for s in (slot_ids or [])]).exists()
        slots.write({'active': bool(active)})
        return len(slots)

    @api.model
    def action_seed_parking_demo(self):
        """Create a realistic set of sections, slots, and today's bookings so
        the parking dashboard fills up for a client demo. Safe to run multiple
        times — existing records are skipped by name."""
        Office = self.env['room.office']
        Booking = self.env['room.booking']
        Employee = self.env['hr.employee']

        offices = {}
        for name in ('Section A', 'Section B', 'Section C'):
            office = Office.search([('name', '=', name)], limit=1)
            if not office:
                office = Office.create({'name': name})
            offices[name] = office

        slot_plan = {
            'Section A': [('A%d' % i, None) for i in range(1, 11)],
            'Section B': [('B%d' % i, None) for i in range(1, 13)],
            'Section C': [('C%d' % i, None) for i in range(1, 11)],
        }
        # Feature flags we want to sprinkle in
        ev_slots = {'A3', 'B2', 'B11', 'C5'}
        accessible_slots = {'A1'}
        premium_names = ['P1', 'P2', 'P3', 'P4', 'P5']

        # Floor / zone metadata per section so the list view shows meaningful
        # location data, not just the office.
        section_meta = {
            'Section A': {'floor': 'G',  'zone': 'Zone A'},
            'Section B': {'floor': 'B1', 'zone': 'Zone B'},
            'Section C': {'floor': 'B2', 'zone': 'Zone C'},
        }

        for office_name, names in slot_plan.items():
            meta = section_meta[office_name]
            for slot_name, _feat in names:
                if self.search_count([('name', '=', slot_name)]):
                    continue
                self.create({
                    'name': slot_name,
                    'office_id': offices[office_name].id,
                    'is_ev_charger': slot_name in ev_slots,
                    'is_accessible': slot_name in accessible_slots,
                    'floor': meta['floor'],
                    'zone': meta['zone'],
                    'location': f"{office_name} - {meta['zone']}",
                })
        for idx, slot_name in enumerate(premium_names):
            if self.search_count([('name', '=', slot_name)]):
                continue
            self.create({
                'name': slot_name,
                'office_id': offices['Section A'].id,
                'is_premium': True,
                'is_ev_charger': idx < 2,
                'floor': 'G',
                'zone': 'Premium',
                'location': 'Section A - Premium',
            })

        employee_names = [
            'Emma Wilson', 'David Park', 'Sarah Chen', 'Lisa Wong',
            'Chris Evans', 'James Miller', 'Nina Patel', 'Kevin Hart',
            'Monica Liu', 'Alex Johnson', 'Mike Ross',
        ]
        employees = {}
        for name in employee_names:
            emp = Employee.search([('name', '=', name)], limit=1)
            if not emp:
                emp = Employee.create({'name': name})
            employees[name] = emp

        # Build "today H:00" in the user's timezone, convert back to naive UTC
        # so the dashboard displays the intended wall-clock hours.
        user_tz = pytz.timezone(self.env.user.tz or 'UTC')
        today_date = fields.Date.context_today(self)
        midnight_local = user_tz.localize(datetime.combine(today_date, time(0, 0)))

        def _at(hour):
            return midnight_local.replace(hour=hour).astimezone(pytz.UTC).replace(tzinfo=None)

        sample_bookings = [
            # (slot, title, start_h, end_h, employee)
            ('A2', 'Daily Commute',  9, 17, 'Emma Wilson'),
            ('A4', 'Client Visit',   9, 17, 'David Park'),
            ('A6', 'Daily Commute',  9, 17, 'Sarah Chen'),
            ('A9', 'Daily Commute',  9, 17, 'Sarah Chen'),
            ('B2', 'Morning Commute', 8, 16, 'Lisa Wong'),
            ('B6', 'Team Offsite',   8, 16, 'Chris Evans'),
            ('B9', 'Morning Commute', 8, 16, 'James Miller'),
            ('B10', 'Daily Commute', 8, 16, 'Nina Patel'),
            ('C3', 'Workshop',      10, 18, 'Kevin Hart'),
            ('C6', 'Design Review', 10, 18, 'Monica Liu'),
            ('C9', 'Workshop',      10, 18, 'Kevin Hart'),
            ('P2', 'Executive',      8, 20, 'Alex Johnson'),
            ('P4', 'Executive',      8, 20, 'Mike Ross'),
        ]
        seed_titles = {title for _s, title, *_r in sample_bookings}
        day_start_utc = _at(0)
        day_end_utc = _at(0) + timedelta(days=1)
        # Wipe any previous seed bookings for today so re-seeding gives clean times.
        Booking.search([
            ('name', 'in', list(seed_titles)),
            ('start_datetime', '>=', day_start_utc),
            ('start_datetime', '<', day_end_utc),
        ]).unlink()

        for slot_name, title, sh, eh, emp_name in sample_bookings:
            slot = self.search([('name', '=', slot_name)], limit=1)
            if not slot:
                continue
            Booking.create({
                'name': title,
                'room_id': slot.id,
                'employee_id': employees[emp_name].id,
                'vehicle_number': 'KA-01-%s-%04d' % (slot_name[0], slot.id),
                'start_datetime': _at(sh),
                'stop_datetime': _at(eh),
            })
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    # ------------------------------------------------------
    # DASHBOARD
    # ------------------------------------------------------

    _SECTION_COLORS = ['#3B82F6', '#8B5CF6', '#14B8A6', '#F59E0B', '#EF4444', '#EC4899', '#0EA5E9', '#84CC16']

    @api.model
    def _user_visible_office_ids(self):
        """Office ids the current user is allowed to see/book at.
        Managers and admins see everything. Regular employees are limited
        to ``parking_visible_office_ids`` on their res.users record (and the
        descendants of those offices, so picking a parent grants access to
        all its branches). Returns None when there is no restriction."""
        user = self.env.user.sudo()
        if user.has_group('room.group_parking_manager') or user.has_group('base.group_system'):
            return None
        # read via sudo because the field has groups="group_parking_manager"
        allowed = user.parking_visible_office_ids
        if not allowed:
            return None
        Office = self.env['room.office'].sudo()
        # Expand each picked office into itself + descendants via parent_path.
        visible = set(allowed.ids)
        all_offices = Office.search([])
        for picked in allowed:
            base = picked.parent_path or ''
            if base:
                for o in all_offices:
                    if o.parent_path and o.parent_path.startswith(base):
                        visible.add(o.id)
        return list(visible)

    @api.model
    def get_parking_offices(self):
        """Return offices with slot counts and hierarchy info for the location
        picker. Includes offices that have at least one active slot (directly
        or via descendants), so parent locations without direct slots but
        with branches under them still appear. Non-manager users are scoped
        to their ``parking_visible_office_ids`` whitelist."""
        Office = self.env['room.office']
        now = fields.Datetime.now()
        visible_ids = self._user_visible_office_ids()
        room_domain = [('active', '=', True)]
        if visible_ids is not None:
            room_domain.append(('office_id', 'in', visible_ids))
        all_rooms = self.search(room_domain)
        rooms_by_office = {}
        for r in all_rooms:
            rooms_by_office.setdefault(r.office_id.id, []).append(r)

        # Currently busy room ids for this moment (to compute free_count).
        active_bookings = self.env['room.booking'].sudo().search([
            ('start_datetime', '<=', now),
            ('stop_datetime', '>=', now),
            ('state', 'in', ('confirmed', 'checked_in', 'pending_approval')),
            ('room_id', 'in', all_rooms.ids),
        ])
        busy_room_ids = set(active_bookings.mapped('room_id').ids)

        office_domain = []
        if visible_ids is not None:
            office_domain.append(('id', 'in', visible_ids))
        offices = Office.search(office_domain)

        def descendants_of(office):
            base = office.parent_path or ''
            return offices.filtered(lambda x: x.parent_path and x.parent_path.startswith(base))

        out = []
        for o in offices.sorted(lambda o: (o.complete_name or '').lower()):
            descendants = descendants_of(o)
            scope_ids = [x.id for x in descendants]
            slots = [r for oid in scope_ids for r in rooms_by_office.get(oid, [])]
            total = len(slots)
            if total == 0:
                continue
            free = sum(1 for r in slots if r.id not in busy_room_ids)
            out.append({
                'id': o.id,
                'name': o.name,
                'complete_name': o.complete_name or o.name,
                'parent_id': o.parent_id.id if o.parent_id else None,
                'location_type': o.location_type,
                'depth': (o.parent_path or '').strip('/').count('/'),
                'slot_count': total,
                'free_count': free,
            })
        return out

    @api.model
    def get_parking_dashboard_data(self, office_id=None):
        """Aggregate data for the ParkSmart dashboard client action.

        Returns a JSON-serialisable dict with KPIs, a per-section slot grid
        (including current/next booking info), a summary for the right rail,
        and quick stats. Datetimes are formatted in the user's timezone.
        """
        def _fmt(dt):
            if not dt:
                return ''
            return fields.Datetime.context_timestamp(self, dt).strftime('%H:%M')

        now = fields.Datetime.now()
        today_start = fields.Datetime.to_datetime(fields.Date.context_today(self))
        today_end = today_start + timedelta(days=1)

        slot_domain = [('active', '=', True)]
        if office_id:
            slot_domain.append(('office_id', '=', int(office_id)))
        visible_ids = self._user_visible_office_ids()
        if visible_ids is not None:
            slot_domain.append(('office_id', 'in', visible_ids))
        slots = self.search(slot_domain)
        slots = slots.sorted(key=lambda s: (s.office_id.name or '', _slot_sort_key(s.name)))
        total = len(slots)

        bookings_today = self.env['room.booking'].search([
            ('room_id', 'in', slots.ids),
            ('stop_datetime', '>=', today_start),
            ('start_datetime', '<', today_end),
        ])
        bookings_by_slot = bookings_today.grouped('room_id')

        sections_map = {}
        for slot in slots:
            key = slot.office_id.id or 0
            section = sections_map.setdefault(key, {
                'id': key,
                'name': slot.office_id.name or _('Unassigned'),
                'color': self._SECTION_COLORS[(key or 0) % len(self._SECTION_COLORS)],
                'slots': [],
                'total': 0,
                'free': 0,
            })
            slot_bookings = bookings_by_slot.get(slot, self.env['room.booking'])
            current = slot_bookings.filtered(lambda b: b.start_datetime <= now <= b.stop_datetime)
            upcoming = slot_bookings.filtered(lambda b: b.start_datetime > now).sorted('start_datetime')
            booking = current[:1] or upcoming[:1]
            if current:
                status = 'taken'
            elif upcoming:
                status = 'reserved'
            else:
                status = 'free'
            booker = ''
            time_label = ''
            if booking:
                booker = booking.employee_id.name or booking.organizer_id.name or booking.name
                time_label = f"{_fmt(booking.start_datetime)}-{_fmt(booking.stop_datetime)}"
            section['total'] += 1
            if status == 'free':
                section['free'] += 1
            section['slots'].append({
                'id': slot.id,
                'name': slot.name,
                'status': status,
                'booker': booker,
                'time': time_label,
                'is_ev': slot.is_ev_charger,
                'is_accessible': slot.is_accessible,
                'is_premium': slot.is_premium,
                'location': slot.location or '',
            })

        # Bubble Premium slots into a dedicated virtual section (keeps them
        # visually grouped even if their office differs).
        premium_slots = [s for sec in sections_map.values() for s in sec['slots'] if s['is_premium']]
        for sec in sections_map.values():
            sec['slots'] = [s for s in sec['slots'] if not s['is_premium']]
            sec['total'] = len(sec['slots'])
            sec['free'] = sum(1 for s in sec['slots'] if s['status'] == 'free')
        sections = [sec for sec in sections_map.values() if sec['slots']]
        sections.sort(key=lambda s: s['name'])
        if premium_slots:
            sections.append({
                'id': 'premium',
                'name': 'Premium',
                'color': '#F59E0B',
                'slots': premium_slots,
                'total': len(premium_slots),
                'free': sum(1 for s in premium_slots if s['status'] == 'free'),
                'is_premium_section': True,
            })

        taken_count = sum(1 for sec in sections for s in sec['slots'] if s['status'] == 'taken')
        reserved_count = sum(1 for sec in sections for s in sec['slots'] if s['status'] == 'reserved')
        available = total - taken_count - reserved_count
        utilization = round((taken_count + reserved_count) * 100 / total) if total else 0

        summary_sections = [{
            'name': sec['name'],
            'color': sec['color'],
            'percent': round((sec['total'] - sec['free']) * 100 / sec['total']) if sec['total'] else 0,
        } for sec in sections]

        ev_count = self.search_count([('active', '=', True), ('is_ev_charger', '=', True)])
        accessible_count = self.search_count([('active', '=', True), ('is_accessible', '=', True)])
        active_users_today = len(bookings_today.mapped('employee_id')) or len(bookings_today.mapped('organizer_id'))

        user = self.env.user
        is_manager = user.has_group('room.group_parking_manager') or user.has_group('base.group_system')
        return {
            'today_label': fields.Date.context_today(self).strftime('%B %d'),
            'is_manager': is_manager,
            'kpi': {
                'total': total,
                'available': available,
                'occupied': taken_count,
                'reserved': reserved_count,
                'utilization': utilization,
            },
            'lot_capacity': {
                'available': available,
                'occupied': taken_count,
                'reserved': reserved_count,
                'total': total,
                'pct_available': round(available * 100 / total) if total else 0,
                'pct_occupied': round(taken_count * 100 / total) if total else 0,
                'pct_reserved': round(reserved_count * 100 / total) if total else 0,
            },
            'sections': sections,
            'summary': {
                'utilization': utilization,
                'sections': summary_sections,
            },
            'quick_stats': {
                'ev_spots': ev_count,
                'accessible': accessible_count,
                'active_users': active_users_today,
            },
        }

    # ------------------------------------------------------
    # TOOLS
    # ------------------------------------------------------

    def _notify_booking_view(self, method, bookings=False):
        """The room booking page is meant to be used on a 'static' device (such
        as a tablet) and is not expected to be reloaded manually. We thus need
        a way to notify the frontend page of any change inside the room
        configuration (in which case we reload the view to apply those changes)
        or any booking update.
        """
        self.ensure_one()
        if method == "reload":
            self.env["bus.bus"]._sendone(f"room_booking#{self.access_token}", f"room#{self.id}/reload", self.room_booking_url)
        elif method in ["create", "delete", "update"]:
            self.env["bus.bus"]._sendone(
                f"room_booking#{self.access_token}",
                f"room#{self.id}/booking/{method}", [{
                    "id": booking.id,
                    "name": booking.name,
                    "start_datetime": booking.start_datetime,
                    "stop_datetime": booking.stop_datetime,
                } for booking in (bookings or [])]
            )
        else:
            raise NotImplementedError(f"Method '{method}' is not implemented for '_notify_booking_view'")
