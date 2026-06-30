# -*- coding: utf-8 -*-
"""1.3 — Phase 1 foundation backfill.

Odoo creates the new columns on upgrade with their declared default, which is
fine for fresh values. We only need to touch fields whose *historical* value
has real meaning:

  * res.users.parking_no_show_count — seed from the count of existing
    ``state='no_show'`` bookings per organiser, so the allocator's fairness
    weight sees an accurate picture from day one.

  * room.booking.last_sync_status — flip pre-existing rows from the default
    'pending' to 'disabled' so the Phase 3 Outlook sync doesn't try to push
    historical bookings on first activation.

Everything else (scores default 50, bans null, EV overrides 0 = inherit) is
handled by the field defaults.
"""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    _backfill_no_show_counts(cr)
    _mark_historical_bookings_as_sync_disabled(cr)


def _backfill_no_show_counts(cr):
    cr.execute("""
        UPDATE res_users u
           SET parking_no_show_count = COALESCE(nb.cnt, 0)
          FROM (
              SELECT organizer_id, COUNT(*) AS cnt
                FROM room_booking
               WHERE state = 'no_show'
                 AND organizer_id IS NOT NULL
            GROUP BY organizer_id
          ) nb
         WHERE u.id = nb.organizer_id
    """)
    _logger.info("parking: backfilled no_show counts for %s users", cr.rowcount)


def _mark_historical_bookings_as_sync_disabled(cr):
    cr.execute("""
        UPDATE room_booking
           SET last_sync_status = 'disabled'
         WHERE last_sync_status = 'pending'
           AND outlook_event_id IS NULL
    """)
    _logger.info("parking: marked %s historical bookings as sync-disabled", cr.rowcount)
