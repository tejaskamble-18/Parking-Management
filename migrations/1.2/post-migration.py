# -*- coding: utf-8 -*-
"""Clean up orphans left from the pre-1.2 layout.

Before 1.2 the addon shipped both a legacy `group_room_manager` (plus its
`res_groups_privilege_room`) and the new `group_parking_manager`. 1.2 drops
the legacy pair and routes all manager ACLs through `group_parking_manager`.

Odoo's `--update` only overwrites declared records; it doesn't remove them.
So on existing installs we need to:

  1. Transfer any users still in `group_room_manager` to `group_parking_manager`
     (so they don't silently lose permissions).
  2. Delete the two orphan XML-id records.
"""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    env = _get_env(cr)

    legacy_group = env.ref('room.group_room_manager', raise_if_not_found=False)
    parking_group = env.ref('room.group_parking_manager', raise_if_not_found=False)

    if legacy_group and parking_group and legacy_group.user_ids:
        _logger.info(
            "Transferring %s user(s) from group_room_manager to group_parking_manager",
            len(legacy_group.user_ids),
        )
        parking_group.sudo().write({'user_ids': [(4, u.id) for u in legacy_group.user_ids]})

    for xmlid in ('room.group_room_manager', 'room.res_groups_privilege_room'):
        rec = env.ref(xmlid, raise_if_not_found=False)
        if rec:
            _logger.info("Removing orphan record %s (id=%s)", xmlid, rec.id)
            rec.sudo().unlink()


def _get_env(cr):
    from odoo.api import Environment
    from odoo import SUPERUSER_ID
    return Environment(cr, SUPERUSER_ID, {})
