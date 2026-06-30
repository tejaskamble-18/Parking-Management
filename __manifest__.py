# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

{
    'name': 'Parking Management',
    'summary': 'Manage Parking Slots and Parking Bookings',
    'description': 'Book parking slots for employees and vehicles, with optional recurring reservations and real-time availability.',
    'category': 'Productivity/Room',
    'version': '1.10',
    'depends': ['mail', 'web_gantt', 'hr', 'hr_holidays', 'bus', 'microsoft_calendar'],
    'data': [
        'security/parking_groups.xml',
        'security/ir_rule.xml',
        'security/ir.model.access.csv',
        'data/room_location_type_data.xml',
        'data/parking_cron.xml',
        'views/room_booking_views.xml',
        'views/room_booking_waitlist_views.xml',
        'views/parking_check_in_wizard_views.xml',
        'views/room_room_views.xml',
        'views/res_users_views.xml',
        'views/res_config_settings_views.xml',
        'views/room_office_views.xml',
        'views/hr_employee_parking_views.xml',
        'views/room_menus.xml',
        'views/room_booking_templates_frontend.xml',
        'views/hr_leave_type_views.xml',
        'views/parking_pwa_templates.xml',
        'views/parking_reports.xml',
    ],
    'demo': [
        'demo/room_office.xml',
        'demo/room_room.xml',
        'demo/room_booking.xml',
    ],
    'installable': True,
    'application': True,
    'assets': {
        'web.assets_backend': [
            'room/static/src/parking_shared/**/*',
            'room/static/src/parking_dashboard/**/*',
            'room/static/src/parking_analytics/**/*',
            'room/static/src/parking_calendar/**/*',
            'room/static/src/my_bookings/**/*',
            'room/static/src/admin_panel/**/*',
        ],
        'web.assets_backend_lazy': [
            'room/static/src/room_booking_gantt_view/**/*',
        ],
        "web.assets_unit_tests": [
            "room/static/src/room_booking/**/*.js",
            "room/static/src/room_booking/**/*.xml",
            "room/static/tests/**/*",
        ],
        'room.assets_room_booking': [
            # 1 Define room variables (takes priority)
            "room/static/src/room_booking/primary_variables.scss",
            "room/static/src/room_booking/bootstrap_overridden.scss",

            #2 Load variables, Bootstrap and UI icons bundles
            ('include', 'web._assets_helpers'),
            ('include', 'web._assets_backend_helpers'),
            'web/static/src/scss/pre_variables.scss',
            'web/static/lib/bootstrap/scss/_variables.scss',
            'web/static/lib/bootstrap/scss/_variables-dark.scss',
            'web/static/lib/bootstrap/scss/_maps.scss',
            ('include', 'web._assets_bootstrap_backend'),
            "web/static/src/libs/fontawesome/css/font-awesome.css",
            "web/static/lib/odoo_ui_icons/*",
            'web/static/src/scss/base_frontend.scss',
            'web/static/src/core/utils/transitions.scss',
            'web/static/src/core/notifications/notification.scss',
            'web/static/src/core/ui/block_ui.scss',

            # Room's specific assets
            'room/static/src/room_booking/**/*',
        ],
    },
    'author': 'Odoo S.A.',
    'license': 'OEEL-1',
}
