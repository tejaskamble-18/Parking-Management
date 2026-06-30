/** @odoo-module **/

import { useService } from "@web/core/utils/hooks";
import { onMounted, onWillUnmount } from "@odoo/owl";

const CHANNEL = "parking/dashboard";
const NOTIF_TYPE = "parking/booking";

/**
 * Subscribe a component to the parking bus channel.
 *
 * The backend emits a lightweight envelope (see room.booking._notify_dashboard)
 * every time a booking is created, updated, cancelled, checked in/out, flagged
 * no-show, or promoted from the waitlist. Components hand us a handler and we
 * wire/unwire the bus subscription automatically around their lifecycle.
 *
 *   useParkingBus((payload) => { this._reload(); });
 *
 * The handler runs with whatever `this` the caller bound; we don't re-bind.
 * Debounce is caller's responsibility — for dashboards, a 150ms debounce
 * prevents a cascade of reloads when a manager bulk-cancels 20 bookings.
 */
export function useParkingBus(handler) {
    const busService = useService("bus_service");
    // We wrap so we can unsubscribe by identity — the service stores the wrapper
    // against our callback, not the underlying caller handler.
    const callback = (payload, meta) => {
        try {
            handler(payload, meta);
        } catch (err) {
            // Never let a UI handler crash the bus worker loop.
            console.error("[parking-bus] handler error", err);
        }
    };
    onMounted(() => {
        busService.addChannel(CHANNEL);
        busService.subscribe(NOTIF_TYPE, callback);
    });
    onWillUnmount(() => {
        busService.unsubscribe(NOTIF_TYPE, callback);
        // Note: we intentionally don't removeChannel — other mounted parking
        // components (or the toast hub) may still be subscribed. The worker
        // de-dupes channel registrations.
    });
}

/**
 * Tiny debouncer so reload-on-every-event doesn't spam the server.
 * Returns a function you can call as many times as you want; it runs once,
 * `ms` milliseconds after the last call.
 */
export function debounced(fn, ms = 150) {
    let h = null;
    return (...args) => {
        if (h) clearTimeout(h);
        h = setTimeout(() => {
            h = null;
            fn(...args);
        }, ms);
    };
}
