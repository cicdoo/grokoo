/** @odoo-module **/
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Grokoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import { registry } from "@web/core/registry";

/**
 * Subscribes once to the "grokoo" bus notifications and fans them out to
 * the currently-open chat component, keyed by session_id.
 */
export const aiAssistantService = {
    dependencies: ["bus_service"],
    start(env, { bus_service }) {
        const handlers = new Map(); // session_id -> callback

        bus_service.subscribe("grokoo", (payload) => {
            const cb = handlers.get(payload.session_id);
            if (cb) {
                cb(payload);
            }
        });
        bus_service.start();

        return {
            register: (sessionId, cb) => handlers.set(sessionId, cb),
            unregister: (sessionId) => handlers.delete(sessionId),
        };
    },
};

registry.category("services").add("grokoo", aiAssistantService);
