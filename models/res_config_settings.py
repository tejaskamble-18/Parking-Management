# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""Parking policy tunables.

Everything an admin can legitimately want to change at runtime lives here as
an ``ir.config_parameter`` row surfaced through the standard Settings UI. No
code change required to retune the allocator, no-show grace, EV limits, or
integration toggles.

Naming convention: every key is prefixed ``room.parking_`` so they cluster
together in ``ir.config_parameter`` and cannot collide with other modules.
"""

from odoo import _, fields, models
from odoo.exceptions import UserError


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # --- No-show policy -------------------------------------------------------
    parking_no_show_grace_minutes = fields.Integer(
        string="No-show grace (minutes)",
        config_parameter='room.parking_no_show_grace_minutes',
        default=15,
        help="Minutes past the booking start after which an unchecked-in booking is marked as no-show.",
    )
    parking_no_show_penalty_points = fields.Integer(
        string="No-show penalty (points)",
        config_parameter='room.parking_no_show_penalty_points',
        default=10,
        help="Points subtracted from a user's priority score on each no-show.",
    )
    parking_ban_threshold = fields.Integer(
        string="Auto-ban after (no-shows)",
        config_parameter='room.parking_ban_threshold',
        default=3,
        help="After this many no-shows in the rolling window, users are temporarily banned from booking.",
    )
    parking_ban_duration_days = fields.Integer(
        string="Ban duration (days)",
        config_parameter='room.parking_ban_duration_days',
        default=7,
        help="How long a temporary ban lasts.",
    )

    # --- EV charging policy ---------------------------------------------------
    parking_ev_max_hours_per_booking = fields.Float(
        string="EV max hours / booking",
        config_parameter='room.parking_ev_max_hours_per_booking',
        default=4.0,
        help="Hard cap on a single booking on an EV slot. Per-slot override available on the slot form.",
    )
    parking_ev_daily_cap_minutes = fields.Integer(
        string="EV daily cap (minutes)",
        config_parameter='room.parking_ev_daily_cap_minutes',
        default=480,
        help="Total minutes a user can book on EV slots per calendar day.",
    )
    parking_ev_cooldown_minutes = fields.Integer(
        string="EV cooldown (minutes)",
        config_parameter='room.parking_ev_cooldown_minutes',
        default=60,
        help="Minimum gap between consecutive bookings on the same EV slot, across users.",
    )

    # --- Allocation scoring weights ------------------------------------------
    # Integer weights keep arithmetic trivial and the configuration readable.
    # The allocator sums weighted booleans + the user's rolling priority_score.
    parking_score_weight_leadership = fields.Integer(
        string="Score: leadership bonus",
        config_parameter='room.parking_score_weight_leadership',
        default=20,
    )
    parking_score_weight_ev = fields.Integer(
        string="Score: EV bonus",
        config_parameter='room.parking_score_weight_ev',
        default=10,
    )
    parking_score_weight_fairness = fields.Integer(
        string="Score: fairness bonus",
        config_parameter='room.parking_score_weight_fairness',
        default=25,
        help="Applied to users who have not booked recently, to keep distribution even.",
    )
    parking_fairness_window_days = fields.Integer(
        string="Fairness window (days)",
        config_parameter='room.parking_fairness_window_days',
        default=14,
        help="Bookings older than this no longer count against the fairness weight.",
    )

    # --- Integration toggles --------------------------------------------------
    parking_outlook_sync_enabled = fields.Boolean(
        string="Sync bookings to Outlook (Microsoft 365)",
        config_parameter='room.parking_outlook_sync_enabled',
        default=False,
        help="When on, booking create/update/delete is mirrored to each organiser's Outlook calendar via Microsoft Graph.",
    )
    parking_outlook_pull_enabled = fields.Boolean(
        string="Pull cancellations from Outlook",
        config_parameter='room.parking_outlook_pull_enabled',
        default=False,
        help="When on, a cron checks Outlook every 15 minutes and cancels parking bookings whose Outlook event was deleted or cancelled.",
    )
    parking_teams_webhook_url = fields.Char(
        string="Microsoft Teams webhook URL",
        config_parameter='room.parking_teams_webhook_url',
        help="Optional Incoming Webhook URL. Leave empty to disable Teams notifications.",
    )

    def action_test_outlook_connection(self):
        """Verify that the current user's Microsoft account is connected and
        the Graph API is reachable. Shows a success/error notification."""
        user = self.env.user
        if not user.microsoft_calendar_rtoken:
            raise UserError(_(
                "No Microsoft account connected for your user. "
                "Go to Calendar → Settings and connect Microsoft 365 first."
            ))
        try:
            token = user._get_microsoft_calendar_token()
            if not token:
                raise UserError(_("Could not obtain a Microsoft access token. Try reconnecting your account."))
            import requests
            headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
            # Probe multiple Graph endpoints — the microsoft_calendar addon
            # only requests 'offline_access openid Calendars.ReadWrite', so
            # /me (User.Read) is expected to fail. We want the first one
            # that uses the Calendar scope to succeed.
            probes = [
                ('/me/calendar',   'Default calendar accessible'),
                ('/me/calendars',  'Calendar list accessible'),
                ('/me/events?$top=1', 'Events endpoint accessible'),
            ]
            results = []
            for path, label in probes:
                r = requests.get(f'https://graph.microsoft.com/v1.0{path}',
                                 headers=headers, timeout=8)
                results.append((path, r.status_code, r.text[:300]))
                if r.status_code == 200:
                    return {
                        'type': 'ir.actions.client',
                        'tag': 'display_notification',
                        'params': {
                            'title': _('Connection Successful'),
                            'message': _('%s (via %s). Outlook sync is ready.') % (label, path),
                            'type': 'success',
                            'sticky': False,
                        },
                    }
            # All probes failed — surface every response so the user can see
            # which endpoints Graph is rejecting and why.
            detail = '\n'.join(f'{p} → {code}: {body}' for p, code, body in results)
            raise UserError(_("All Graph probes failed:\n\n%s") % detail)
        except UserError:
            raise
        except Exception as exc:
            raise UserError(_("Connection test failed: %s") % str(exc)) from exc
