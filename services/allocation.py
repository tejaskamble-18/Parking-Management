# -*- coding: utf-8 -*-
"""Smart parking slot allocation.

The allocator picks a single slot for a user's request window. Responsibility
split:

  * `find_best_slot` — given a user + window + hints, return one room.room
    record or None. Handles access filtering, availability, zone/floor
    fallback, and premium gating.

  * `score_user` — integer score (higher = more deserving). Used for premium
    eligibility and, later, for waitlist ordering.

The service reads a frozen ``ParkingPolicy`` snapshot once per instance;
re-instantiate per operation rather than holding a long-lived reference so
policy changes in the Settings UI take effect immediately.
"""

from datetime import timedelta

from .policy import ParkingPolicy


# A booking's "blocking" states are the ones that occupy a slot for overlap
# purposes. Cancelled/no_show/completed do not block.
BLOCKING_STATES = ('pending_approval', 'confirmed', 'checked_in')


class AllocationService:

    def __init__(self, env):
        self.env = env
        self.policy = ParkingPolicy.load(env)

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def score_user(self, user, is_ev_request=False):
        """Compute a priority score for `user` against this request.

        The scoring is intentionally additive and transparent — every weight
        is visible in the Settings UI, and the caller can log the breakdown
        to chatter for audit purposes. The scoring fields on res.users are
        manager-only (groups="room.group_parking_manager"), so we read via
        sudo — the booking user is not expected to read their own score.
        """
        p = self.policy
        user_sudo = user.sudo()
        score = int(user_sudo.parking_priority_score or 0)

        # Leadership — use has_group so the manager group (which *implies*
        # leadership) counts. Direct `in group_ids` only catches direct
        # membership and would miss managers.
        if user.has_group('room.group_parking_leadership'):
            score += p.score_weight_leadership

        # EV request — only applies when the user explicitly asks for an EV
        # slot. The bonus lets EV drivers outrank non-EV users for scarce
        # chargers without starving the rest of the lot.
        if is_ev_request:
            score += p.score_weight_ev

        # Fairness — users who haven't booked recently get a boost so premium
        # slots don't gravitate to the same handful of people every morning.
        if self._is_eligible_for_fairness_bonus(user):
            score += p.score_weight_fairness

        return score

    def _is_eligible_for_fairness_bonus(self, user):
        last_booked = user.sudo().parking_last_booking_at
        if not last_booked:
            return True
        from odoo import fields
        cutoff = fields.Datetime.now() - timedelta(days=self.policy.fairness_window_days)
        return last_booked < cutoff

    # ------------------------------------------------------------------
    # Slot selection
    # ------------------------------------------------------------------

    def find_best_slot(self, user, start_dt, stop_dt, *,
                       prefer_ev=False, prefer_accessible=False,
                       office_id=None, zone=None, floor=None):
        """Return the best `room.room` for this request, or an empty recordset.

        The ranking goes: hard filters (access, availability) first, then a
        tiered fallback (same zone → floor → any-in-office → any). Within a
        tier we prefer slots whose capability matches the request (EV/accessible)
        and order by slot name for stable output.
        """
        Room = self.env['room.room']
        if not (start_dt and stop_dt and start_dt < stop_dt):
            return Room

        candidates = self._eligible_slots(user, start_dt, stop_dt, office_id=office_id)
        if not candidates:
            return Room

        # Premium gating: only leadership or users above a score cutoff can
        # win a premium slot. Non-eligible users simply have premium slots
        # dropped from their candidate pool — they are never told "you're not
        # important enough", the premium slot is invisible to them.
        if not self._can_book_premium(user):
            candidates = candidates.filtered(lambda r: not r.is_premium)

        tiers = self._tiered_candidates(candidates, zone=zone, floor=floor)

        for tier in tiers:
            if not tier:
                continue
            # _rank_within_tier returns a Python list (sorted() output); pick
            # the first record, which is already a singleton recordset.
            ranked = self._rank_within_tier(tier, prefer_ev=prefer_ev, prefer_accessible=prefer_accessible)
            if ranked:
                return ranked[0]
        return Room

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _eligible_slots(self, user, start_dt, stop_dt, office_id=None):
        """All active slots the user is allowed to book that are free in the
        given window. Returns a recordset."""
        Room = self.env['room.room']
        Booking = self.env['room.booking']

        domain = [('active', '=', True)]
        if office_id:
            domain.append(('office_id', '=', office_id))
        # Honour per-employee location whitelist. Managers / admins have
        # no restriction (the helper returns None for them).
        visible_ids = Room.with_user(user.id)._user_visible_office_ids()
        if visible_ids is not None:
            domain.append(('office_id', 'in', visible_ids))
        rooms = Room.sudo().search(domain)

        # Access filter: a slot with allowed_group_ids is gated to those groups.
        # Empty allowed_group_ids = open to any internal user.
        user_group_ids = set(user.group_ids.ids)
        rooms = rooms.filtered(
            lambda r: not r.allowed_group_ids or bool(set(r.allowed_group_ids.ids) & user_group_ids)
        )

        # Availability filter: exclude slots with an overlapping blocking booking.
        # `_read_group(..., ['room_id'])` yields tuples of *records*, so pull
        # `.id` off each. (Earlier version used the record itself as a set key
        # and always returned "not busy" — silently letting the allocator hand
        # back a room that was actually taken.)
        busy_room_ids = {
            room.id for (room,) in Booking.sudo()._read_group(
                [
                    ('room_id', 'in', rooms.ids),
                    ('state', 'in', BLOCKING_STATES),
                    ('start_datetime', '<', stop_dt),
                    ('stop_datetime', '>', start_dt),
                ],
                ['room_id'],
            )
        }
        return rooms.filtered(lambda r: r.id not in busy_room_ids)

    def _tiered_candidates(self, candidates, zone=None, floor=None):
        """Split a candidate recordset into preference tiers.

        Tier order:
          1. same zone + same floor
          2. same zone
          3. same floor
          4. anything else
        """
        same_zone_floor = candidates.browse()
        same_zone = candidates.browse()
        same_floor = candidates.browse()
        rest = candidates.browse()
        for r in candidates:
            if zone and r.zone == zone and floor and r.floor == floor:
                same_zone_floor |= r
            elif zone and r.zone == zone:
                same_zone |= r
            elif floor and r.floor == floor:
                same_floor |= r
            else:
                rest |= r
        # When no hint is given, tier-1..3 are empty and everything lands in rest.
        return [same_zone_floor, same_zone, same_floor, rest]

    def _rank_within_tier(self, tier, prefer_ev=False, prefer_accessible=False):
        """Order slots within a tier: capability match first, then alpha."""
        def key(r):
            # Lower tuple sorts first, so negate booleans.
            return (
                0 if prefer_ev and r.is_ev_charger else 1 if prefer_ev else 0,
                0 if prefer_accessible and r.is_accessible else 1 if prefer_accessible else 0,
                # Premium last within a tier — we only pick it when nothing else matches.
                1 if r.is_premium else 0,
                r.name or '',
                r.id,
            )
        return sorted(tier, key=key)

    def _can_book_premium(self, user):
        """Leadership OR score above half the leadership weight can book premium
        slots. Intentional soft gate so high-performing employees without a
        formal leadership title are not shut out."""
        # has_group walks implied groups, so a Parking Manager (which implies
        # Leadership) passes this gate.
        if user.has_group('room.group_parking_leadership'):
            return True
        # Score-based fallback — manager-gated field, always read sudo.
        return int(user.sudo().parking_priority_score or 0) >= (self.policy.score_weight_leadership // 2 + 50)
