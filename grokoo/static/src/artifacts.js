/** @odoo-module **/
// Copyright 2026 CICDoo (https://cicdoo.com)
// SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Grokoo-Commercial
// Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).

// Pull embedded HTML out of an assistant reply so it can render visually (e.g. a
// CSS/SVG chart) in the artifact panel instead of showing as a code block. We
// only break the text at HTML blocks: a fence tagged ```html, or an untagged
// ``` fence whose content starts with a tag. Every other fenced block (json,
// python, …) is left inside the surrounding markdown run so normal replies
// aren't fragmented. An unclosed fence (mid-stream) stays markdown until its
// closing ``` arrives.
export function splitSegments(text) {
    const re = /```([^\n`]*)\n([\s\S]*?)```/g;
    const segs = [];
    let last = 0;
    let m;
    while ((m = re.exec(text))) {
        const lang = (m[1] || "").trim().toLowerCase();
        const content = m[2];
        const isHtml = lang === "html" || (lang === "" && /^\s*<[!a-zA-Z]/.test(content));
        if (!isHtml) {
            continue; // leave non-HTML code blocks in the markdown run
        }
        if (m.index > last) {
            segs.push({ type: "md", text: text.slice(last, m.index) });
        }
        segs.push({ type: "html", text: content });
        last = re.lastIndex;
    }
    if (last < text.length) {
        segs.push({ type: "md", text: text.slice(last) });
    }
    return segs;
}

// Just the embedded HTML block strings, in order.
export function extractHtmlBlocks(text) {
    return splitSegments(text)
        .filter((s) => s.type === "html")
        .map((s) => s.text);
}
