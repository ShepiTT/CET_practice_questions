// Exercise the shipped player's IIFE with inert DOM/media doubles. No browser,
// HTTP requests, audio files, or question/history database are used.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
const start = source.indexOf('    (function () {', source.indexOf('/* ---- Listening player'));
const end = source.indexOf('    /* ---- Answering', start);
assert.ok(start >= 0 && end > start, 'The shipped listening-player block must be present');
const player = source.slice(start, end);

function element() {
    const classes = new Set();
    const listeners = new Map();
    return {
        dataset: {}, style: {setProperty() {}}, attributes: {}, textContent: '',
        classList: {
            add(...names) { names.forEach(name => classes.add(name)); },
            remove(...names) { names.forEach(name => classes.delete(name)); },
            contains(name) { return classes.has(name); },
            toggle(name, force) {
                const enabled = force === undefined ? !classes.has(name) : force;
                if (enabled) classes.add(name); else classes.delete(name);
                return enabled;
            }
        },
        setAttribute(name, value) { this.attributes[name] = String(value); },
        appendChild() {},
        addEventListener(name, callback, options) {
            const items = listeners.get(name) || [];
            items.push({callback, once: Boolean(options && options.once)});
            listeners.set(name, items);
        },
        removeEventListener(name, callback) {
            listeners.set(name, (listeners.get(name) || []).filter(item => item.callback !== callback));
        },
        callbacks(name) { return (listeners.get(name) || []).map(item => item.callback); },
        emit(name, event = {}) {
            for (const item of [...(listeners.get(name) || [])]) {
                if (!(listeners.get(name) || []).includes(item)) continue;
                if (item.once) this.removeEventListener(name, item.callback);
                item.callback(event);
            }
        }
    };
}

function harness() {
    const document = Object.assign(element(), {body: element(), createElement: element});
    const nodes = {};
    for (const selector of ['#audio-bar', '#audio-bar-title', '[data-player-group]',
        '[data-player-toggle]', '[data-player-seek]', '[data-player-current]',
        '[data-player-duration]', '[data-player-ticks]', '[data-player-rate]',
        '[data-player-restart]', '[data-player-close]']) nodes[selector] = element();
    const bar = nodes['#audio-bar'];
    bar.hidden = true;
    const plays = [];
    const audio = Object.assign(element(), {
        readyState: 0, duration: NaN, currentTime: 0, paused: true, ended: false,
        load() { this.readyState = 0; this.currentTime = 0; },
        play() {
            plays.push({src: this.dataset.src, time: this.currentTime});
            this.paused = false; this.emit('play');
            return Promise.resolve();
        },
        pause() { this.paused = true; this.emit('pause'); }
    });
    Object.defineProperty(audio, 'src', {set(value) {
        this.readyState = 0; this.duration = NaN; this.currentTime = 0; this.paused = true;
    }});
    nodes['#audio-player'] = audio;
    const boxes = [];
    function group(id, src, seconds) {
        const box = element();
        box.dataset = {groupId: String(id), audioStart: String(seconds)};
        const button = element();
        button.dataset = {src, paper: src, groupTitle: 'Group ' + id};
        button.closest = selector => selector === '.audio-controls' ? box : null;
        const hint = element();
        box.querySelector = selector => selector === '.audio-play' ? button : selector === '.audio-hint' ? hint : null;
        box.click = () => document.emit('click', {target: {
            closest: selector => selector === '.audio-play' ? button : null
        }});
        boxes.push(box);
        return box;
    }
    vm.runInNewContext(player, {
        document,
        $: selector => nodes[selector],
        $$: selector => selector === '.audio-controls' ? boxes : [],
        postJSON() { throw Error('Player test must not mutate data'); }
    });
    return {
        group, nodes, audio, bar, plays,
        metadata() { audio.duration = 1000; audio.readyState = 1; audio.emit('loadedmetadata'); },
        close() { nodes['[data-player-close]'].emit('click'); },
        toggle() { nodes['[data-player-toggle]'].emit('click'); }
    };
}

test('a closed player cannot autoplay after metadata arrives, including an already queued callback', () => {
    const h = harness();
    const first = h.group(1, '/audio/a.mp3', 38);
    first.click();
    const queued = h.audio.callbacks('loadedmetadata');
    h.close();
    h.metadata();
    queued.forEach(callback => callback());
    assert.equal(h.bar.hidden, true);
    assert.equal(h.audio.paused, true);
    assert.equal(h.plays.length, 0);
    assert.equal(h.audio.currentTime, 0);
    assert.equal(first.classList.contains('is-active'), false);
    assert.equal(h.nodes['[data-player-group]'].textContent, '');
});

for (const sameSource of [true, false]) {
    test(`switching ${sameSource ? 'groups on one recording' : 'recordings'} invalidates the old start request`, () => {
        const h = harness();
        const first = h.group(1, '/audio/a.mp3', 38);
        const second = h.group(2, sameSource ? '/audio/a.mp3' : '/audio/b.mp3', 115);
        first.click();
        const oldCallbacks = h.audio.callbacks('loadedmetadata');
        second.click();
        h.metadata();
        oldCallbacks.forEach(callback => callback());
        assert.deepEqual(h.plays, [{src: second.querySelector('.audio-play').dataset.src, time: 115}]);
        assert.equal(first.classList.contains('is-active'), false);
        assert.equal(second.classList.contains('is-active'), true);
        assert.equal(h.nodes['[data-player-group]'].textContent, 'Group 2');
    });
}

test('closing and reopening while loading starts only the newly chosen group', () => {
    const h = harness();
    const first = h.group(1, '/audio/a.mp3', 38);
    const second = h.group(2, '/audio/a.mp3', 115);
    first.click();
    const oldCallbacks = h.audio.callbacks('loadedmetadata');
    h.close();
    second.click();
    h.metadata();
    oldCallbacks.forEach(callback => callback());
    assert.equal(h.bar.hidden, false);
    assert.deepEqual(h.plays, [{src: '/audio/a.mp3', time: 115}]);
    assert.equal(second.classList.contains('is-playing'), true);
});

test('normal play, pause, resume, group seek and close keep their existing behavior', () => {
    const h = harness();
    const first = h.group(1, '/audio/a.mp3', 38);
    const second = h.group(2, '/audio/a.mp3', 115);
    first.click();
    h.metadata();
    assert.deepEqual(h.plays, [{src: '/audio/a.mp3', time: 38}]);
    assert.equal(first.classList.contains('is-playing'), true);
    h.audio.currentTime = 50;
    h.toggle();
    assert.equal(h.audio.paused, true);
    h.toggle();
    assert.deepEqual(h.plays.at(-1), {src: '/audio/a.mp3', time: 50});
    second.click();
    assert.deepEqual(h.plays.at(-1), {src: '/audio/a.mp3', time: 115});
    assert.equal(h.nodes['[data-player-group]'].textContent, 'Group 2');
    h.close();
    assert.equal(h.audio.paused, true);
    assert.equal(h.bar.hidden, true);
    assert.equal(second.classList.contains('is-active'), false);
});

test('an out-of-range group start reports the problem without playing', () => {
    const h = harness();
    const first = h.group(1, '/audio/a.mp3', 1001);
    first.click();
    h.metadata();
    assert.equal(h.plays.length, 0);
    assert.equal(first.querySelector('.audio-hint').textContent, '起点超过录音时长，请重新标记');
});
