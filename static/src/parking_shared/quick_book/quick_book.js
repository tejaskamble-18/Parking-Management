/** @odoo-module **/

/**
 * Quick Book dialog.
 *
 * Minimal form: start time, end time, and a small cluster of one-click
 * preference chips (EV / Accessible / specific office). Behind the scenes it
 * calls `room.booking.action_quick_book`, which runs the server-side allocator
 * and enforces every existing policy (bans, EV caps, premium approval).
 *
 * The dialog handles three outcomes cleanly:
 *   - slot assigned       → success toast with "Booked X · slot Y"
 *   - pending approval    → info toast explaining approval is required
 *   - no slot available   → error toast + offer to join the waitlist
 */

import { Component, useState, onMounted, useRef } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { useService } from "@web/core/utils/hooks";

export class QuickBookDialog extends Component {
    static template = "room.QuickBookDialog";
    static components = { Dialog };
    static props = {
        close: Function,
        onBooked: { type: Function, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.startRef = useRef("startInput");
        const now = new Date();
        // Default: +10 min → +70 min, rounded to nearest 5 min.
        const startMs = Math.ceil((now.getTime() + 10 * 60000) / (5 * 60000)) * (5 * 60000);
        const stopMs = startMs + 60 * 60000;
        this.state = useState({
            startDatetime: this._fmtLocal(new Date(startMs)),
            stopDatetime: this._fmtLocal(new Date(stopMs)),
            vehicleNumber: "",
            preferEv: false,
            preferAccessible: false,
            officeId: "",
            offices: [],
            busy: false,
            error: "",
        });
        onMounted(async () => {
            // Prefetch office list so the dropdown doesn't flash empty. If the
            // call fails (e.g. permission), we just hide the dropdown.
            try {
                const rows = await this.orm.searchRead(
                    "room.office", [], ["id", "name"],
                    { order: "name" },
                );
                this.state.offices = rows;
            } catch (_err) {
                this.state.offices = [];
            }
            if (this.startRef.el) this.startRef.el.focus();
        });
    }

    /** Format a Date as `YYYY-MM-DDTHH:MM` in the *local* timezone for <input type="datetime-local"/>. */
    _fmtLocal(d) {
        const pad = (n) => String(n).padStart(2, "0");
        return (
            `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
            `T${pad(d.getHours())}:${pad(d.getMinutes())}`
        );
    }

    /** Convert a `datetime-local` input value (local time) to a naive-UTC string. */
    _localToUtcString(value) {
        if (!value) return "";
        const local = new Date(value);
        if (Number.isNaN(local.getTime())) return "";
        const pad = (n) => String(n).padStart(2, "0");
        const d = new Date(local.getTime() - local.getTimezoneOffset() * 60000);
        return (
            `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ` +
            `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:00`
        );
    }

    togglePref(key) {
        this.state[key] = !this.state[key];
    }

    /**
     * Preset chips: 1h, 2h, 4h starting now (+5min). Lets users fire a booking
     * in a single click when they just walked up to the lot.
     */
    applyPreset(hours) {
        const now = new Date();
        const startMs = Math.ceil((now.getTime() + 5 * 60000) / (5 * 60000)) * (5 * 60000);
        const stopMs = startMs + hours * 60 * 60000;
        this.state.startDatetime = this._fmtLocal(new Date(startMs));
        this.state.stopDatetime = this._fmtLocal(new Date(stopMs));
    }

    async submit() {
        if (this.state.busy) return;
        this.state.error = "";
        const startStr = this._localToUtcString(this.state.startDatetime);
        const stopStr = this._localToUtcString(this.state.stopDatetime);
        if (!startStr || !stopStr) {
            this.state.error = "Please pick a valid start and end time.";
            return;
        }
        if (new Date(this.state.startDatetime) >= new Date(this.state.stopDatetime)) {
            this.state.error = "End time must be after start time.";
            return;
        }
        this.state.busy = true;
        try {
            const result = await this.orm.call(
                "room.booking",
                "action_quick_book",
                [{
                    start_datetime: startStr,
                    stop_datetime: stopStr,
                    vehicle_number: this.state.vehicleNumber || "",
                    prefer_ev: this.state.preferEv,
                    prefer_accessible: this.state.preferAccessible,
                    prefer_office_id: this.state.officeId ? parseInt(this.state.officeId, 10) : null,
                }],
            );
            if (result.needs_approval) {
                this.notification.add(
                    `${result.slot_name} reserved — waiting for Leadership approval.`,
                    { type: "info" },
                );
            } else {
                this.notification.add(
                    `Booked ${result.office_name ? result.office_name + " · " : ""}${result.slot_name}.`,
                    { type: "success" },
                );
            }
            if (this.props.onBooked) this.props.onBooked(result);
            this.props.close();
        } catch (err) {
            // Odoo wraps ValidationError in err.data.message for ORM RPC.
            const msg = err?.data?.message || err?.message || "Could not book. Please try again.";
            this.state.error = msg;
            this.state.busy = false;
        }
    }

    cancel() {
        this.props.close();
    }
}
