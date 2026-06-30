# -*- coding: utf-8 -*-
"""Microsoft Graph event CRUD for parking bookings.

Piggybacks on the existing ``microsoft_calendar`` addon for token management:
we read each organiser's still-valid access token via
``user._get_microsoft_calendar_token()`` (which auto-refreshes on expiry) and
issue the event CRUD against the Graph ``/me/calendar/events`` endpoint.

We do **not** reuse the ``MicrosoftCalendarSync`` mixin — parking bookings
have a state machine (``checked_in``, ``no_show``) that doesn't map to a
``calendar.event`` cleanly. Instead we drive sync from the
``last_sync_status`` field on ``room.booking`` (added in Phase 1) and a
lightweight cron that processes pending rows, so Graph latency never blocks
a user-facing write.

Failure philosophy:
  * Never raise to the calling booking code — Graph being down must not
    stop someone from parking.
  * Record a meaningful ``last_sync_error`` so managers can debug.
  * The cron will retry pending rows; transient 5xx failures self-heal.
"""

import json
import logging

import requests

from odoo import _, fields

_logger = logging.getLogger(__name__)

_GRAPH_ROOT = 'https://graph.microsoft.com/v1.0'
_GRAPH_TIMEOUT = 10  # seconds; short so a wedged cron pass doesn't stall
# Odoo stores datetimes in naive UTC; Graph wants ISO-8601 with a timeZone hint.
_GRAPH_TZ = 'UTC'


class GraphSyncSkipped(Exception):
    """Raised internally when we should *silently* skip a booking — e.g. the
    organiser hasn't connected Microsoft yet, or sync is disabled globally.
    Not surfaced to the user.
    """


class GraphService:

    def __init__(self, env):
        self.env = env

    # ------------------------------------------------------------------
    # Public API — called from room.booking
    # ------------------------------------------------------------------

    def sync_booking(self, booking):
        """Create, update, or delete the Outlook event that mirrors this
        booking, based on its current state.

        Returns one of: 'synced', 'failed', 'disabled', 'skipped'.
        Never raises.
        """
        try:
            if not self._sync_enabled():
                return self._mark(booking, 'disabled')
            user = booking.organizer_id
            if not user or not self._user_authenticated(user):
                # User has no MS account connected; pause the booking in
                # 'disabled' so the cron stops re-checking it every pass.
                # If they connect later, a booking write will re-arm sync.
                return self._mark(booking, 'disabled',
                                  error=_("Organiser has not connected their Microsoft account."))

            if booking.state in ('cancelled', 'no_show') or not booking.active:
                if booking.outlook_event_id:
                    return self._delete_event(booking, user)
                # Nothing to delete — just mark as in-sync.
                return self._mark(booking, 'synced')

            if booking.outlook_event_id:
                return self._patch_event(booking, user)
            return self._create_event(booking, user)
        except Exception as exc:
            # Belt-and-braces: any unexpected error lands here so the cron
            # keeps moving. The booking row stays pending (not reset to
            # failed by this branch) so the next cron attempt retries.
            _logger.exception("Graph sync crashed for booking %s", booking.id)
            return self._mark(booking, 'failed', error=str(exc)[:250])

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------

    def _sync_enabled(self):
        return str(self.env['ir.config_parameter'].sudo().get_param(
            'room.parking_outlook_sync_enabled', 'False'
        )).strip().lower() in ('1', 'true', 'yes')

    def _user_authenticated(self, user):
        return bool(user.sudo().microsoft_calendar_rtoken)

    def _token_for(self, user):
        """Return a valid access token, or None (caller handles skip)."""
        token = user.sudo()._get_microsoft_calendar_token()
        return token or None

    def _request(self, method, path, token, payload=None):
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }
        body = json.dumps(payload) if payload is not None else None
        url = f'{_GRAPH_ROOT}{path}'
        try:
            resp = requests.request(method, url, headers=headers, data=body, timeout=_GRAPH_TIMEOUT)
        except requests.RequestException as exc:
            raise RuntimeError(f"Graph {method} {path} transport error: {exc}") from exc
        if resp.status_code in (200, 201, 204):
            return resp.json() if resp.content else {}
        # Let the caller decide what to do with the error; we want to
        # preserve Graph's error body for the chatter audit.
        raise RuntimeError(
            f"Graph {method} {path} returned {resp.status_code}: {resp.text[:300]}"
        )

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def _create_event(self, booking, user):
        token = self._token_for(user)
        if not token:
            return self._mark(booking, 'failed',
                              error=_("Could not obtain a Microsoft access token."))
        body = self._build_event_body(booking)
        resp = self._request('POST', '/me/calendar/events', token, payload=body)
        event_id = resp.get('id') or ''
        if not event_id:
            return self._mark(booking, 'failed',
                              error=_("Graph returned 201 but no event id."))
        booking.sudo().write({'outlook_event_id': event_id})
        return self._mark(booking, 'synced')

    def _patch_event(self, booking, user):
        token = self._token_for(user)
        if not token:
            return self._mark(booking, 'failed',
                              error=_("Could not obtain a Microsoft access token."))
        body = self._build_event_body(booking)
        path = f'/me/calendar/events/{booking.outlook_event_id}'
        try:
            self._request('PATCH', path, token, payload=body)
        except RuntimeError as exc:
            # If the event vanished on Outlook's side (user deleted from the
            # Outlook UI), fall back to create so we re-establish the link.
            if '404' in str(exc):
                booking.sudo().write({'outlook_event_id': False})
                return self._create_event(booking, user)
            raise
        return self._mark(booking, 'synced')

    def _delete_event(self, booking, user):
        token = self._token_for(user)
        if not token:
            return self._mark(booking, 'failed',
                              error=_("Could not obtain a Microsoft access token."))
        path = f'/me/calendar/events/{booking.outlook_event_id}'
        try:
            self._request('DELETE', path, token)
        except RuntimeError as exc:
            if '404' not in str(exc):
                # Non-404 errors are worth retrying.
                raise
            # 404 = event already gone on their side; idempotent success.
        booking.sudo().write({'outlook_event_id': False})
        return self._mark(booking, 'synced')

    # ------------------------------------------------------------------
    # Body shaping
    # ------------------------------------------------------------------

    def _build_event_body(self, booking):
        """Map a booking to Graph's event schema. Keep the payload minimal —
        we do not attempt to carry EV/waitlist/penalty metadata, since we
        don't want to pollute the user's personal calendar with internal
        parking terminology."""
        start_iso = booking.start_datetime.replace(microsecond=0).isoformat() if booking.start_datetime else None
        end_iso = booking.stop_datetime.replace(microsecond=0).isoformat() if booking.stop_datetime else None
        room = booking.room_id
        location_bits = [bit for bit in (room.office_id.name, room.name) if bit]
        location_name = " / ".join(location_bits) or "Parking"

        subject_bits = ['Parking']
        if room.name:
            subject_bits.append(room.name)
        if booking.vehicle_number:
            subject_bits.append(f'({booking.vehicle_number})')
        subject = ' '.join(subject_bits)

        body = {
            'subject': subject,
            'body': {
                'contentType': 'text',
                'content': _("Parking booking created in Odoo: %(name)s", name=booking.name or subject),
            },
            'start': {'dateTime': start_iso, 'timeZone': _GRAPH_TZ},
            'end': {'dateTime': end_iso, 'timeZone': _GRAPH_TZ},
            'location': {'displayName': location_name},
            'isReminderOn': True,
            'reminderMinutesBeforeStart': 15,
            'showAs': 'busy',
            # Keep the categories sensible so users can filter by them in Outlook.
            'categories': ['Parking'],
            # Mark it private — parking details shouldn't show as
            # scheduled-meeting-content to the user's coworkers.
            'sensitivity': 'private',
        }
        return body

    # ------------------------------------------------------------------
    # Two-way sync: pull cancellations/deletions from Outlook
    # ------------------------------------------------------------------

    def pull_outlook_cancellations(self, batch_size=200):
        """Use Microsoft Graph delta queries to detect Outlook events that were
        deleted or cancelled by the user, then cancel the matching parking booking.

        Stores a delta link per user in ``ir.config_parameter`` so each poll
        only fetches changes since the last run (incremental, not full scan).

        Returns the number of bookings cancelled.
        """
        if not self._sync_enabled():
            return 0

        ICP = self.env['ir.config_parameter'].sudo()
        pull_enabled = str(ICP.get_param('room.parking_outlook_pull_enabled', 'False')).strip().lower()
        if pull_enabled not in ('1', 'true', 'yes'):
            return 0

        # Only process users who have at least one synced booking with an outlook_event_id.
        Booking = self.env['room.booking'].sudo()
        synced_users = Booking.search([
            ('outlook_event_id', '!=', False),
            ('state', 'not in', ('cancelled', 'no_show')),
            ('active', '=', True),
        ], limit=batch_size).mapped('organizer_id')

        cancelled_count = 0
        for user in synced_users:
            if not self._user_authenticated(user):
                continue
            token = self._token_for(user)
            if not token:
                continue
            try:
                cancelled_count += self._pull_for_user(user, token, ICP, Booking)
            except Exception:
                _logger.exception("Outlook pull failed for user %s", user.id)
        return cancelled_count

    def _pull_for_user(self, user, token, ICP, Booking):
        """Fetch delta changes for one user and cancel any parking bookings
        whose Outlook event was deleted or cancelled."""
        param_key = f'room.outlook_delta_link_{user.id}'
        delta_link = ICP.get_param(param_key)

        if delta_link:
            url = delta_link
        else:
            # First run: fetch events from the past 30 days onward to seed the delta cursor.
            from datetime import datetime, timedelta
            since = (datetime.utcnow() - timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%SZ')
            url = (
                f'{_GRAPH_ROOT}/me/calendarView/delta'
                f'?startDateTime={since}'
                f'&endDateTime=2099-01-01T00:00:00Z'
                f'&$select=id,isCancelled,showAs'
            )

        headers = {
            'Authorization': f'Bearer {token}',
            'Accept': 'application/json',
            'Prefer': 'odata.maxpagesize=50',
        }

        cancelled_event_ids = set()
        next_link = url

        # Page through the delta response.
        while next_link:
            try:
                resp = requests.get(next_link, headers=headers, timeout=_GRAPH_TIMEOUT)
            except requests.RequestException as exc:
                _logger.warning("Outlook delta GET failed for user %s: %s", user.id, exc)
                break

            if resp.status_code == 410:
                # Delta token expired — clear and retry on next cron run.
                ICP.set_param(param_key, '')
                _logger.info("Outlook delta token expired for user %s, will reseed next run.", user.id)
                break

            if resp.status_code != 200:
                _logger.warning("Outlook delta returned %s for user %s", resp.status_code, user.id)
                break

            data = resp.json()
            for event in data.get('value', []):
                # Deleted events come through with @removed annotation; cancelled
                # events have isCancelled=True or showAs='free'.
                removed = event.get('@removed') or event.get('@odata.removed')
                is_cancelled = event.get('isCancelled', False)
                show_as_free = event.get('showAs', '') == 'free'
                if removed or is_cancelled or show_as_free:
                    eid = event.get('id')
                    if eid:
                        cancelled_event_ids.add(eid)

            # Persist delta link for the next run.
            new_delta = data.get('@odata.deltaLink')
            if new_delta:
                ICP.set_param(param_key, new_delta)
                next_link = None
            else:
                next_link = data.get('@odata.nextLink')

        if not cancelled_event_ids:
            return 0

        # Find active bookings matching the cancelled Outlook event IDs.
        to_cancel = Booking.search([
            ('organizer_id', '=', user.id),
            ('outlook_event_id', 'in', list(cancelled_event_ids)),
            ('state', 'not in', ('cancelled', 'no_show')),
            ('active', '=', True),
        ])
        for booking in to_cancel:
            try:
                booking.action_cancel()
                booking.message_post(body=(
                    "Booking automatically cancelled because the corresponding "
                    "Outlook calendar event was deleted or cancelled by the user."
                ))
                _logger.info("Cancelled booking %s (Outlook event %s deleted by user %s)",
                             booking.id, booking.outlook_event_id, user.name)
            except Exception:
                _logger.exception("Could not cancel booking %s during Outlook pull", booking.id)
        return len(to_cancel)

    # ------------------------------------------------------------------
    # Status marker
    # ------------------------------------------------------------------

    def _mark(self, booking, status, error=None):
        vals = {
            'last_sync_status': status,
            'last_sync_at': fields.Datetime.now(),
        }
        if status == 'failed' and error:
            vals['last_sync_error'] = error
        elif status == 'synced':
            vals['last_sync_error'] = False
        booking.sudo().write(vals)
        return status
