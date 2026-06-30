# -*- coding: utf-8 -*-
"""1.5 — Phase 3 integrations.

Nothing structural to migrate:
  * parking.carpool.trip is created by Odoo on upgrade.
  * hr.leave.type.parking_auto_cancel_bookings defaults to False — a safe,
    opt-in default so we don't surprise anyone by cancelling bookings on
    the very leave types they've historically tolerated.

Admins flip the flag per-type from the Time Off settings UI once they've
decided which leave types should release parking.
"""


def migrate(cr, version):
    # No-op migration; present so the 1.5 upgrade path is explicit and a
    # future pre/post hook can live alongside without needing a version bump.
    return
