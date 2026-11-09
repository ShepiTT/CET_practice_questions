/* Presentation only: preserve source characters; never rewrite question data. */
(function (root, factory) {
    'use strict';
    var api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    if (root) root.CETText = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';

    var BRACKET_HEADING = /【(?:答案解析|答案详解|题干译文|语法判断|语义判断|干扰项|答案|定位|信号|替换|排除|翻译|解析|精析|考点)】/g;
    var PLAIN_HEADING = /^((?:语法判断|语义判断|答案解析|答案详解|题干译文|定位解析|干扰项|解析|精析)(?=\s|[:：]|[\u3400-\u9fff]|$)(?:\s*[:：])?)(.*)$/;
    var SPEAKER = /^((?:M|W|Man|Woman)\s*[:：])\s*(.*)$/;
    var LABEL = /^([A-O]\s*[)）])\s*(.*)$/;
    var CJK_EDGE = /[\u3400-\u9fff\uf900-\ufaff，。！？；：、（）【】《》“”‘’]/;

    function kindOf(kind) {
        return kind === 'explanation' || kind === 'transcript' ? kind : 'passage';
    }

    function sourceLines(raw) {
        if (raw === null || raw === undefined) return [];
        var lines = String(raw).replace(/\r\n?/g, '\n').split('\n').map(function (line) {
            // Template indentation and PDF spacing are layout, not source words.
            return line.replace(/[\t\f\v\u00a0 ]+/g, ' ').trim();
        });
        while (lines.length && !lines[0]) lines.shift();
        while (lines.length && !lines[lines.length - 1]) lines.pop();
        return lines;
    }

    function joinLine(left, right) {
        if (!left) return right;
        if (!right) return left;
        var end = left.charAt(left.length - 1), start = right.charAt(0);
        // Keep a printed hyphen while joining its wrapped continuation. Do not
        // guess whether the source intended a hyphenated or a joined word.
        if (/[A-Za-z0-9]-$/.test(left) && /^[A-Za-z0-9]/.test(right)) return left + right;
        if (CJK_EDGE.test(end) || CJK_EDGE.test(start)) return left + right;
        if (/[([{£$€¥]/.test(end) || /^[,.;:!?%)\]}]/.test(right)) return left + right;
        return left + ' ' + right;
    }

    function inlineChoices(line) {
        if (!/^[A-D]\s*[)）]/.test(line)) return [line];
        var starts = [], match;
        var pattern = /(?:^|[\s。；;])([A-D]\s*[)）])\s*/g;
        while ((match = pattern.exec(line))) {
            starts.push(match.index + match[0].indexOf(match[1]));
        }
        if (starts.length < 2 || starts[0] !== 0) return [line];
        return starts.map(function (start, index) {
            return line.slice(start, index + 1 < starts.length ? starts[index + 1] : line.length).trim();
        });
    }

    function inlineDialogue(line) {
        if (!SPEAKER.test(line)) return [line];
        var starts = [], match;
        var pattern = /(?:^|\s)((?:M|W|Man|Woman)\s*[:：])/g;
        while ((match = pattern.exec(line))) {
            starts.push(match.index + match[0].indexOf(match[1]));
        }
        if (starts.length < 2) return [line];
        return starts.map(function (start, index) {
            return line.slice(start, index + 1 < starts.length ? starts[index + 1] : line.length).trim();
        });
    }

    function inlinePassageSections(line) {
        var starts = [], labels = [], match;
        var pattern = /(?:^|[\s。；;])([A-O]\s*[)）])\s*/g;
        while ((match = pattern.exec(line))) {
            starts.push(match.index + match[0].indexOf(match[1]));
            labels.push(match[1].charAt(0));
        }
        // A matching passage has a consecutive, unrepeated A...C (or longer)
        // sequence. Ordinary mentions of plan A) and B) are left untouched.
        if (labels.length < 3 || labels.some(function (label, index) {
            return label !== String.fromCharCode(65 + index);
        })) return [line];
        var result = [], prefix = line.slice(0, starts[0]).trim();
        if (prefix) result.push(prefix);
        starts.forEach(function (start, index) {
            result.push(line.slice(start, index + 1 < starts.length ? starts[index + 1] : line.length).trim());
        });
        return result;
    }

    function narrativeBreak(part, nextLine) {
        if (!part || part.type !== 'paragraph' || !/^["'(]*[A-Z]/.test(nextLine)) return false;
        var text = part.text.replace(/["'”’)]*$/, '');
        if (!/[.!?]$/.test(text)) return false;
        if (!/\.$/.test(text)) return true;
        // An initial, dotted acronym, or common title/date abbreviation at a
        // PDF line ending is not evidence of a paragraph boundary.
        if (/(?:^|\s)(?:[A-Z]\.)+$/.test(text)) return false;
        return !/\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc|e\.g|i\.e|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.$/i.test(text);
    }

    function unitsFor(lines, kind) {
        var units = [];
        function textUnit(text) {
            if (!text) return;
            var pieces = kind === 'explanation' ? inlineChoices(text) : [text];
            pieces.forEach(function (piece) {
                inlineDialogue(piece).forEach(function (turn) {
                    units.push({type: 'line', text: turn});
                });
            });
        }
        lines.forEach(function (line) {
            if (!line) {
                units.push({type: 'blank'});
                return;
            }
            if (kind !== 'explanation') {
                (kind === 'passage' ? inlinePassageSections(line) : [line]).forEach(textUnit);
                return;
            }
            var plain = line.match(PLAIN_HEADING);
            if (plain) {
                units.push({type: 'heading', text: plain[1]});
                textUnit(plain[2].trim());
                return;
            }
            var cursor = 0, found = false, match;
            BRACKET_HEADING.lastIndex = 0;
            while ((match = BRACKET_HEADING.exec(line))) {
                found = true;
                textUnit(line.slice(cursor, match.index).trim());
                units.push({type: 'heading', text: match[0]});
                cursor = match.index + match[0].length;
            }
            if (found) textUnit(line.slice(cursor).trim());
            else textUnit(line);
        });
        return units;
    }

    function optionRuns(parts, kind) {
        if (kind !== 'explanation') return parts;
        var result = [], index = 0;
        while (index < parts.length) {
            var run = [], cursor = index;
            while (cursor < parts.length && parts[cursor].type === 'labelled' && /^[A-D]/.test(parts[cursor].label)) {
                run.push(parts[cursor]);
                cursor += 1;
            }
            // Consider the entire consecutive run. A duplicate fifth label or
            // an incomplete run stays in its original order and representation.
            var labels = run.map(function (item) { return item.label.charAt(0); });
            if (run.length === 4 && labels.slice().sort().join('') === 'ABCD' && run.every(function (item) { return !!item.text; })) {
                result.push({type: 'options', items: run.slice().sort(function (a, b) {
                    return a.label.charCodeAt(0) - b.label.charCodeAt(0);
                }).map(function (item) { return {label: item.label, text: item.text}; })});
                index = cursor;
            } else if (run.length) {
                run.forEach(function (item) { result.push(item); });
                index = cursor;
            } else {
                result.push(parts[index]);
                index += 1;
            }
        }
        return result;
    }

    /**
     * Return a safe, inspectable presentation stream. Shapes:
     * paragraph {text}; dialogue {speaker,text}; heading {text};
     * labelled {label,text}; options {items:[{label,text}]}; blank {}.
     * Only an unambiguous complete A-D run is reordered, in explanations only.
     */
    function blocks(raw, kind) {
        kind = kindOf(kind);
        var parts = [], pending = null;
        function flush() {
            if (pending) parts.push(pending);
            pending = null;
        }
        unitsFor(sourceLines(raw), kind).forEach(function (unit) {
            if (unit.type !== 'line') {
                flush();
                parts.push(unit);
                return;
            }
            var line = unit.text, speaker = line.match(SPEAKER), label = line.match(LABEL);
            if (speaker) {
                flush();
                pending = {type: 'dialogue', speaker: speaker[1], text: speaker[2]};
            } else if (label) {
                flush();
                pending = {type: 'labelled', label: label[1], text: label[2]};
            } else {
                // An unlabelled line after an A-D explanation choice is kept
                // separate. Otherwise the subsequent analysis can be swallowed
                // into the final choice, and sorting it would move the analysis.
                if (pending && kind === 'explanation' && pending.type === 'labelled' && /^[A-D]/.test(pending.label)) flush();
                // A complete English question and its Chinese translation are
                // readable as separate paragraphs, rather than a glued sentence.
                if (pending && kind === 'explanation' && /\?$/.test(pending.text) && /^[\u3400-\u9fff]/.test(line)) flush();
                if (narrativeBreak(pending, line)) flush();
                if (!pending) pending = {type: 'paragraph', text: line};
                else pending.text = joinLine(pending.text, line);
            }
        });
        flush();
        return optionRuns(parts, kind);
    }

    function render(container, raw, options) {
        if (!container || !container.ownerDocument) throw new TypeError('CETText.render requires a DOM container.');
        var kind = kindOf(options && options.kind);
        var prefix = kind === 'explanation' ? 'explanation-' : 'reading-';
        var doc = container.ownerDocument, fragment = doc.createDocumentFragment();
        var parts = blocks(raw, kind);
        function element(tag, className, text) {
            var node = doc.createElement(tag);
            node.className = className;
            if (text !== undefined) node.textContent = text;
            return node;
        }
        function labelled(node, label, text, speaker) {
            node.appendChild(element('span', prefix + (speaker ? 'speaker' : 'label'), label));
            node.appendChild(element('span', prefix + 'body', text ? ' ' + text : ''));
            return node;
        }
        parts.forEach(function (part) {
            var node;
            if (part.type === 'heading') node = element('h5', prefix + 'heading', part.text);
            else if (part.type === 'blank') {
                node = element('div', prefix + 'blank');
                node.setAttribute('aria-hidden', 'true');
            } else if (part.type === 'dialogue') {
                node = labelled(element('p', prefix + 'dialogue'), part.speaker, part.text, true);
            } else if (part.type === 'labelled') {
                node = labelled(element('p', prefix + 'labelled'), part.label, part.text, false);
            } else if (part.type === 'options') {
                node = element('ul', prefix + 'options');
                part.items.forEach(function (item) {
                    node.appendChild(labelled(element('li', prefix + 'option'), item.label, item.text, false));
                });
            } else node = element('p', prefix + 'paragraph', part.text);
            fragment.appendChild(node);
        });
        // All source text reaches textContent only. In particular, HTML-looking
        // exam text or a malicious payload can never create markup or handlers.
        container.replaceChildren(fragment);
        return parts;
    }

    return {blocks: blocks, render: render};
}));
