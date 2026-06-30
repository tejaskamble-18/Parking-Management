# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import api, SUPERUSER_ID

# Old Selection key -> seeded room.location.type external id
MAPPING = {
    'head_office': 'room.location_type_head_office',
    'branch':      'room.location_type_branch',
    'building':    'room.location_type_building',
    'floor':       'room.location_type_floor',
    'zone':        'room.location_type_zone',
}


def migrate(cr, version):
    """Backfill location_type_id (new Many2one) from the old location_type
    Selection column for existing parking locations."""
    cr.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'room_office' AND column_name = 'location_type'
    """)
    if not cr.fetchone():
        return  # old column already gone — nothing to migrate

    env = api.Environment(cr, SUPERUSER_ID, {})
    for old_value, xmlid in MAPPING.items():
        type_rec = env.ref(xmlid, raise_if_not_found=False)
        if not type_rec:
            continue
        cr.execute("""
            UPDATE room_office
               SET location_type_id = %s
             WHERE location_type = %s
               AND location_type_id IS NULL
        """, (type_rec.id, old_value))
