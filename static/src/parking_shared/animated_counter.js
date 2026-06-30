/** @odoo-module **/

import { Component, onMounted, onWillUpdateProps, useState } from "@odoo/owl";

export class AnimatedCounter extends Component {
    static template = "room.AnimatedCounter";
    static props = {
        value: { type: [Number, String] },
        suffix: { type: String, optional: true },
        duration: { type: Number, optional: true },
        decimals: { type: Number, optional: true },
    };
    static defaultProps = { suffix: "", duration: 800, decimals: 0 };

    setup() {
        this.state = useState({ current: 0 });
        this._frame = null;
        onMounted(() => this._animate(0, Number(this.props.value) || 0));
        onWillUpdateProps((next) => {
            const target = Number(next.value) || 0;
            if (target !== this.state.current) {
                this._animate(this.state.current, target);
            }
        });
    }

    _animate(from, to) {
        if (this._frame) cancelAnimationFrame(this._frame);
        const start = performance.now();
        const dur = this.props.duration;
        const step = (now) => {
            const t = Math.min((now - start) / dur, 1);
            const eased = 1 - Math.pow(1 - t, 3); // ease-out cubic
            const next = from + (to - from) * eased;
            this.state.current = this.props.decimals
                ? Number(next.toFixed(this.props.decimals))
                : Math.round(next);
            if (t < 1) this._frame = requestAnimationFrame(step);
        };
        this._frame = requestAnimationFrame(step);
    }

    get display() {
        const v = this.props.decimals
            ? this.state.current.toFixed(this.props.decimals)
            : this.state.current;
        return `${v}${this.props.suffix}`;
    }
}
