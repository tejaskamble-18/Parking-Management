# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from datetime import datetime, timedelta
from psycopg2 import IntegrityError

from odoo import fields
from odoo.addons.room.tests.common import RoomCommon
from odoo.exceptions import ValidationError
from odoo.tests.common import tagged
from odoo.tools import mute_logger

@tagged("post_install", "-at_install")
class TestRoomConstraints(RoomCommon):

    def test_booking_constraints(self):
        room_booking = self.env["room.booking"]
        # Create a booking overlapping an existing booking
        with self.assertRaises(ValidationError, msg="Bookings may not overlap"):
            room_booking.create({
                "name": "Meeting",
                "room_id": self.rooms[0].id,
                "start_datetime": datetime(2023, 5, 15, 10, 30),
                "stop_datetime": datetime(2023, 5, 15, 11, 30),
            })

        # Create two bookings that overlap
        with self.assertRaises(ValidationError, msg="Bookings may not overlap"):
            room_booking.create([
                {
                    "name": "Meeting 1",
                    "room_id": self.rooms[0].id,
                    "start_datetime": datetime(2023, 5, 15, 13, 0),
                    "stop_datetime": datetime(2023, 5, 15, 14, 0),
                }, {
                    "name": "Meeting 2",
                    "room_id": self.rooms[0].id,
                    "start_datetime": datetime(2023, 5, 15, 13, 30),
                    "stop_datetime": datetime(2023, 5, 15, 14, 30),
                }
            ])

        with self.assertRaises(ValidationError, msg="Stop date of a booking must be after start date"):
            room_booking.create({
                "name": "Meeting",
                "room_id": self.rooms[0].id,
                "start_datetime": datetime(2023, 5, 15, 10, 0),
                "stop_datetime": datetime(2023, 5, 15, 9, 0),
            })

    def test_no_past_booking(self):
        """Employees can't book a start time in the past; managers/admins bypass."""
        Booking = self.env["room.booking"]
        employee = self.env["res.users"].create({
            "name": "Parker Emp",
            "login": "parker_emp",
            "group_ids": [(6, 0, [self.env.ref("base.group_user").id])],
        })
        past = datetime(2020, 1, 1, 10, 0)
        future = fields.Datetime.now() + timedelta(days=1)

        # Employee: a past start is rejected
        with self.assertRaises(ValidationError, msg="Employees may not book in the past"):
            Booking.with_user(employee).create({
                "name": "Past booking",
                "room_id": self.rooms[1].id,
                "start_datetime": past,
                "stop_datetime": past + timedelta(hours=1),
            })

        # Employee: a near-future start is allowed
        Booking.with_user(employee).create({
            "name": "Future booking",
            "room_id": self.rooms[1].id,
            "start_datetime": future,
            "stop_datetime": future + timedelta(hours=1),
        })

        # Manager/admin bypass: a past start is allowed for the superuser
        Booking.create({
            "name": "Admin past booking",
            "room_id": self.rooms[1].id,
            "start_datetime": past + timedelta(hours=2),
            "stop_datetime": past + timedelta(hours=3),
        })

    @mute_logger('odoo.sql_db')
    def test_room_constraints(self):
        room_room = self.env["room.room"]
        # Create a room with an existing short code
        with self.assertRaises(IntegrityError, msg="Short code must be unique"):
            room_room.create({
                "name": "Room 3",
                "office_id": self.office.id,
                "short_code": self.rooms[0].short_code,
            })

        # Create a room with an existing access token
        with self.assertRaises(IntegrityError, msg="Access token must be unique"):
            room_room.create({
                "name": "Room 3",
                "office_id": self.office.id,
                "access_token": self.rooms[0].access_token,
            })
