/** @odoo-module **/

/**
 * Toast hub for the parking app.
 *
 * Listens to the parking/dashboard bus channel and pushes short, tasteful
 * notifications via Odoo's stock notification service. This is intentionally
 * stateless and rides on the notification widget Odoo already paints in the
 * top-right — that gives us mobile-correct behaviour and a stable z-index
 * for free.
 *
 * The hub is registered as a `main_components` entry so it's always mounted
 * in the backend shell; that way, bus events fire whether or not the user
 * has a parking client action open.
 */

import { registry } from "@web/core/registry";
import { Component, onMounted, onWillUnmount, xml } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

const CHANNEL = "parking/dashboard";
const NOTIF_TYPE = "parking/booking";

// Event-to-message table. Keep messages short — they appear as toasts.
// `type` maps to Odoo's notification service levels.
const EVENT_MAP = {
    created:      { type: "success", label: "New parking booking" },
    updated:      { type: "info",    label: "Booking updated" },
    cancelled:    { type: "warning", label: "Booking cancelled" },
    checked_in:   { type: "success", label: "Checked in" },
    checked_out:  { type: "info",    label: "Checked out" },
    no_show:      { type: "danger",  label: "No-show flagged" },
    promoted:     { type: "success", label: "Waitlist promoted" },
};

export class ParkingToastHub extends Component {
    static template = xml`<div class="o_parking_toast_hub"/>`;
    static props = {};

    setup() {
        this.bus = useService("bus_service");
        this.notification = useService("notification");
        // Rate-limit noisy bursts: if a bulk cancel blasts 20 events, we
        // collapse them into a single "20 bookings cancelled" toast.
        this._pending = new Map();  // event -> count
        this._flushHandle = null;
        this._onPayload = this._onPayload.bind(this);

        onMounted(() => {
            this.bus.addChannel(CHANNEL);
            this.bus.subscribe(NOTIF_TYPE, this._onPayload);
        });
        onWillUnmount(() => {
            this.bus.unsubscribe(NOTIF_TYPE, this._onPayload);
            if (this._flushHandle) clearTimeout(this._flushHandle);
        });
    }

    _onPayload(payload) {
        if (!payload) return;
        const ev = payload.event;
        if (!EVENT_MAP[ev]) return;
        this._pending.set(ev, (this._pending.get(ev) || 0) + 1);
        if (this._flushHandle) return;
        // 600ms coalescing window — long enough to group a bulk op, short
        // enough to feel real-time on single events.
        this._flushHandle = setTimeout(() => {
            this._flush();
            this._flushHandle = null;
        }, 600);
    }

    _flush() {
        for (const [ev, count] of this._pending.entries()) {
            const meta = EVENT_MAP[ev];
            const msg = count > 1 ? `${meta.label} × ${count}` : meta.label;
            this.notification.add(msg, { type: meta.type });
        }
        this._pending.clear();
    }
}

registry.category("main_components").add("room.ParkingToastHub", {
    Component: ParkingToastHub,
});
