/** @odoo-module **/
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Grokoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import { Component, useRef, onPatched, onMounted } from "@odoo/owl";
import { AiMessage } from "./message";

export class AiMessageList extends Component {
    static template = "grokoo.MessageList";
    static components = { AiMessage };
    static props = {
        messages: Array,
        running: Boolean,
        onOpenArtifact: { type: Function, optional: true },
    };

    setup() {
        this.scrollRef = useRef("scroll");
        onMounted(() => this._scrollToBottom());
        onPatched(() => this._scrollToBottom());
    }

    _scrollToBottom() {
        const el = this.scrollRef.el;
        if (el) {
            el.scrollTop = el.scrollHeight;
        }
    }
}
