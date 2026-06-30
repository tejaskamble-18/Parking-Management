/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { QuickBookDialog } from "../quick_book/quick_book";

/**
 * SmartSuggest — personalised parking insights card.
 *
 * Calls get_my_booking_patterns() once on mount. When the user has fewer
 * than 3 bookings the backend returns null and we render a gentle nudge
 * instead of surfacing empty patterns.
 *
 * The "Book my usual" button pre-fills QuickBookDialog with the user's
 * preferred zone and floor derived from their history.
 */
export class SmartSuggest extends Component {
    static template = "room.SmartSuggest";
    static props = {};

    setup() {
        this.orm = useService("orm");
        this.dialog = useService("dialog");
        this.state = useState({ patterns: null, loading: true });
        onWillStart(async () => {
            try {
                this.state.patterns = await this.orm.call(
                    "room.booking",
                    "get_my_booking_patterns",
                    [],
                );
            } catch (_e) {
                // Non-fatal — widget stays hidden.
            } finally {
                this.state.loading = false;
            }
        });
    }

    onBookUsual() {
        const p = this.state.patterns;
        const defaults = {};
        if (p?.preferred_zone) defaults.prefer_zone = p.preferred_zone;
        if (p?.preferred_floor) defaults.prefer_floor = p.preferred_floor;
        this.dialog.add(QuickBookDialog, { defaults });
    }

    get noShowBadge() {
        const rate = this.state.patterns?.no_show_rate ?? 0;
        if (rate >= 20) return { label: `${rate}% no-show`, cls: "o_ss_badge_warn" };
        if (rate >= 10) return { label: `${rate}% no-show`, cls: "o_ss_badge_info" };
        return null;
    }
}
