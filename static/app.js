/* CET 真题练习 — interface behaviour.
   Presentation only. Every request below goes to an endpoint that already
   existed, with the same payload it always had; nothing here grades, stores
   or rewrites question data. Exam text reaches the page through textContent
   or CETText.render, never as markup. */
(function () {
    'use strict';

    var root = document.documentElement;

    function $(selector, scope) { return (scope || document).querySelector(selector); }
    function $$(selector, scope) { return Array.prototype.slice.call((scope || document).querySelectorAll(selector)); }

    function icon(name) {
        var namespace = 'http://www.w3.org/2000/svg';
        var svg = document.createElementNS(namespace, 'svg');
        svg.setAttribute('class', 'icon');
        svg.setAttribute('aria-hidden', 'true');
        svg.setAttribute('focusable', 'false');
        var use = document.createElementNS(namespace, 'use');
        use.setAttribute('href', '#i-' + name);
        svg.appendChild(use);
        return svg;
    }

    function span(className, text) {
        var node = document.createElement('span');
        node.className = className;
        node.textContent = text;
        return node;
    }

    var reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
    function scrollBehavior() { return reducedMotion.matches ? 'auto' : 'smooth'; }

    /* Play a one-shot highlight, and leave no class behind to replay it later. */
    function highlightOnce(element, className) {
        element.classList.remove(className);
        void element.offsetWidth;   // restart if it is already running
        element.classList.add(className);
        var done = function (event) {
            if (event.target !== element) return;
            element.classList.remove(className);
            element.removeEventListener('animationend', done);
        };
        element.addEventListener('animationend', done);
    }

    /* ---- Theme ----------------------------------------------------------- */

    var themeToggle = $('[data-theme-toggle]');

    function savedTheme() {
        try {
            var stored = localStorage.getItem('cet-theme');
            if (stored === 'light' || stored === 'dark') return stored;
        } catch (error) { /* storage can be blocked */ }
        var cookie = document.cookie.match(/(?:^|; )cet-theme=(light|dark)/);
        return cookie ? cookie[1] : null;
    }

    function setTheme(theme, remember) {
        root.setAttribute('data-theme', theme);
        root.setAttribute('data-bs-theme', theme);
        if (themeToggle) {
            themeToggle.setAttribute('aria-pressed', String(theme === 'dark'));
            themeToggle.title = theme === 'dark' ? '切换到浅色界面' : '切换到深色界面';
        }
        if (!remember) return;
        try { localStorage.setItem('cet-theme', theme); } catch (error) { /* keep the cookie below */ }
        // The desktop app picks a new local port on each launch; a cookie is shared across ports.
        document.cookie = 'cet-theme=' + theme + '; path=/; max-age=31536000; SameSite=Lax';
    }

    setTheme(root.getAttribute('data-theme') === 'dark' ? 'dark' : 'light', false);
    if (themeToggle) {
        themeToggle.addEventListener('click', function () {
            setTheme(root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark', true);
        });
    }
    (function () {
        var system = window.matchMedia('(prefers-color-scheme: dark)');
        var follow = function (event) { if (!savedTheme()) setTheme(event.matches ? 'dark' : 'light', false); };
        if (system.addEventListener) system.addEventListener('change', follow);
        else if (system.addListener) system.addListener(follow);
    })();

    /* ---- Requests -------------------------------------------------------- */

    async function requestJSON(url, options) {
        var controller = new AbortController();
        var timeout = setTimeout(function () { controller.abort(); }, 15000);
        try {
            var response = await fetch(url, Object.assign({}, options || {}, { signal: controller.signal }));
            var data = await response.json();
            if (!response.ok || data.error) throw new Error(data.error || '请求失败，请重试');
            return data;
        } catch (error) {
            if (error.name === 'AbortError') throw new Error('请求超时，请重试');
            if (error instanceof TypeError || error instanceof SyntaxError) throw new Error('连接失败，请稍后重试');
            throw error;
        } finally {
            clearTimeout(timeout);
        }
    }
    window.requestJSON = requestJSON;   // the admin page's own script uses it

    function postJSON(url, payload) {
        return requestJSON(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
    }

    /* A second click within a few seconds confirms an action that changes stored data. */
    function disarm(button) {
        clearTimeout(button._confirmTimer);
        if (!button.classList.contains('is-confirming')) return;
        var label = button.querySelector('[data-label]') || button;
        label.textContent = button.dataset.idleLabel || '';
        button.classList.remove('is-confirming');
        delete button.dataset.idleLabel;
    }

    function confirmedTwice(button, prompt) {
        if (button.classList.contains('is-confirming')) {
            disarm(button);
            return true;
        }
        var label = button.querySelector('[data-label]') || button;
        button.dataset.idleLabel = label.textContent;
        label.textContent = prompt;
        button.classList.add('is-confirming');
        button._confirmTimer = setTimeout(function () { disarm(button); }, 3500);
        return false;
    }

    /* ---- Exam text ------------------------------------------------------- */

    if (window.CETText) {
        $$('[data-reading-kind]').forEach(function (element) {
            window.CETText.render(element, element.textContent, { kind: element.dataset.readingKind });
        });
    }

    // Matching passages: remember which paragraph carries which letter, so an answer can point to it.
    $$('.group--matching .group__passage .reading-labelled').forEach(function (paragraph) {
        var label = paragraph.querySelector('.reading-label');
        var letter = label ? label.textContent.trim().charAt(0).toUpperCase() : '';
        if (/^[A-Z]$/.test(letter) && !paragraph.parentNode.querySelector('[data-paragraph="' + letter + '"]')) {
            paragraph.dataset.paragraph = letter;
        }
    });

    function showParagraph(question, letter) {
        var group = question.closest('.group');
        var pane = group && group.querySelector('.group__passage');
        var paragraph = pane && pane.querySelector('[data-paragraph="' + letter + '"]');
        if (!paragraph) return;
        var paneBox = pane.getBoundingClientRect();
        if (paneBox.bottom < 80 || paneBox.top > window.innerHeight - 80) {
            pane.scrollIntoView({ behavior: scrollBehavior(), block: 'nearest' });
        }
        var box = paragraph.getBoundingClientRect();
        var top = pane.scrollTop + (box.top - paneBox.top) - Math.max(16, (pane.clientHeight - box.height) / 2);
        pane.scrollTo({ top: Math.max(0, top), behavior: scrollBehavior() });
        highlightOnce(paragraph, 'is-located');
    }

    /* ---- Listening player ------------------------------------------------ */

    (function () {
        var bar = $('#audio-bar');
        var audio = $('#audio-player');
        if (!bar || !audio) return;

        var titleElement = $('#audio-bar-title');
        var groupElement = $('[data-player-group]', bar);
        var toggle = $('[data-player-toggle]', bar);
        var seek = $('[data-player-seek]', bar);
        var currentElement = $('[data-player-current]', bar);
        var durationElement = $('[data-player-duration]', bar);
        var ticks = $('[data-player-ticks]', bar);
        var rateButton = $('[data-player-rate]', bar);
        var rates = [1, 1.25, 1.5, 0.75];
        var rateIndex = 0;
        var activeBox = null;
        var dragging = false;
        var playbackRequest = 0;
        var pendingStart = null;
        var playFailed = '播放失败，请检查音频后重试';

        function clock(seconds) {
            seconds = Math.max(0, Math.floor(seconds || 0));
            return Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0');
        }

        function hint(box, text) {
            var element = box && box.querySelector('.audio-hint');
            if (element) element.textContent = text;
        }

        function startOf(box) { return parseFloat(box.dataset.audioStart) || 0; }
        function duration() { return Number.isFinite(audio.duration) ? audio.duration : 0; }

        /* Groups on this page that belong to the loaded recording and have a known start. */
        function timedBoxes() {
            return $$('.audio-controls').filter(function (box) {
                var play = box.querySelector('.audio-play');
                return play && play.dataset.src === audio.dataset.src && box.dataset.audioStart !== '';
            });
        }

        function drawTicks() {
            ticks.textContent = '';
            var total = duration();
            if (!total) return;
            timedBoxes().forEach(function (box) {
                var start = startOf(box);
                if (start >= total) return;
                var tick = document.createElement('i');
                tick.style.left = (start / total * 100).toFixed(3) + '%';
                if (box === activeBox) tick.className = 'is-active';
                ticks.appendChild(tick);
            });
        }

        function syncPlaying() {
            var playing = !audio.paused && !audio.ended;
            bar.classList.toggle('is-playing', playing);
            toggle.setAttribute('aria-label', playing ? '暂停' : '播放');
            if (activeBox) activeBox.classList.toggle('is-playing', playing);
        }

        function setActive(box) {
            if (activeBox && activeBox !== box) activeBox.classList.remove('is-active', 'is-playing');
            activeBox = box;
            var play = box && box.querySelector('.audio-play');
            groupElement.textContent = play ? (play.dataset.groupTitle || '') : '';
            if (box) box.classList.add('is-active');
            syncPlaying();
            drawTicks();
        }

        /* As the recording runs on, follow it into the group that is now being read. */
        function followPlayhead() {
            if (bar.hidden) return;   // a closed player has no current group
            var time = audio.currentTime + 0.25;
            var best = null;
            var bestStart = -1;
            timedBoxes().forEach(function (box) {
                var start = startOf(box);
                if (start <= time && start > bestStart) { best = box; bestStart = start; }
            });
            if (best && best !== activeBox) setActive(best);
        }

        function paint() {
            var total = duration();
            var time = dragging ? Number(seek.value) : audio.currentTime;
            if (!dragging) seek.value = String(Math.floor(audio.currentTime));
            currentElement.textContent = clock(time);
            seek.style.setProperty('--ratio', total ? Math.min(time / total, 1).toFixed(4) : '0');
        }

        function paintDuration() {
            var total = duration();
            seek.max = String(Math.floor(total));
            durationElement.textContent = clock(total);
            drawTicks();
            paint();
        }

        function cancelPendingStart() {
            playbackRequest += 1;
            if (pendingStart) audio.removeEventListener('loadedmetadata', pendingStart);
            pendingStart = null;
        }

        function play(box) {
            var request = playbackRequest;
            audio.play().catch(function () {
                if (request === playbackRequest && !bar.hidden && activeBox === box) hint(box, playFailed);
            });
        }

        document.addEventListener('click', function (event) {
            var playButton = event.target.closest('.audio-play');
            if (playButton) {
                cancelPendingStart();
                var request = playbackRequest;
                var source = playButton.dataset.src;
                var box = playButton.closest('.audio-controls');
                if (audio.dataset.src !== playButton.dataset.src) {
                    audio.src = playButton.dataset.src;
                    audio.dataset.src = playButton.dataset.src;
                    titleElement.textContent = playButton.dataset.paper || playButton.dataset.title || '';
                    paintDuration();
                }
                bar.hidden = false;
                document.body.classList.add('has-audio-bar');
                setActive(box);
                var start = startOf(box);
                var seekToStart = function () {
                    if (request !== playbackRequest || bar.hidden || activeBox !== box || audio.dataset.src !== source) return;
                    pendingStart = null;
                    if (Number.isFinite(audio.duration) && start >= audio.duration) {
                        hint(box, '起点超过录音时长，请重新标记');
                        return;
                    }
                    audio.currentTime = start;
                    play(box);
                };
                if (audio.readyState >= 1) seekToStart();
                else {
                    pendingStart = seekToStart;
                    audio.addEventListener('loadedmetadata', seekToStart, { once: true });
                    audio.load();
                }
                return;
            }

            var mark = event.target.closest('.audio-mark');
            if (mark) {
                var markBox = mark.closest('.audio-controls');
                var markPlay = markBox.querySelector('.audio-play');
                if (!markPlay || audio.dataset.src !== markPlay.dataset.src || audio.readyState < 1) {
                    hint(markBox, '请先播放这组题对应的录音，再标记起点');
                    return;
                }
                // A start that is already recorded is only replaced on a deliberate second click.
                if (markBox.dataset.audioStart !== '' && !confirmedTwice(mark, '再点一次，改到当前位置')) return;
                var seconds = Math.max(0, audio.currentTime - 1);   // a second of lead-in
                mark.disabled = true;
                postJSON('/set_audio_start', { group_id: markBox.dataset.groupId, seconds: seconds }).then(function () {
                    markBox.dataset.audioStart = seconds;
                    var label = markPlay.querySelector('.audio-start-label');
                    if (!label) {
                        label = span('audio-start-label', '');
                        markPlay.appendChild(label);
                    }
                    label.textContent = '(' + clock(seconds) + ')';
                    hint(markBox, '起点已记为 ' + clock(seconds));
                    drawTicks();
                }).catch(function (error) {
                    hint(markBox, error.message);
                }).finally(function () { mark.disabled = false; });
            }
        });

        toggle.addEventListener('click', function () {
            if (audio.paused) play(activeBox);
            else audio.pause();
        });

        seek.addEventListener('input', function () { dragging = true; paint(); });
        seek.addEventListener('change', function () {
            audio.currentTime = Number(seek.value);
            dragging = false;
            paint();
        });

        $$('[data-player-skip]', bar).forEach(function (button) {
            button.addEventListener('click', function () {
                var target = audio.currentTime + Number(button.dataset.playerSkip);
                audio.currentTime = Math.min(Math.max(0, target), duration() || target);
            });
        });

        $('[data-player-restart]', bar).addEventListener('click', function () {
            if (!activeBox) return;
            audio.currentTime = startOf(activeBox);
            play(activeBox);
        });

        rateButton.addEventListener('click', function () {
            rateIndex = (rateIndex + 1) % rates.length;
            audio.playbackRate = rates[rateIndex];
            var text = String(rates[rateIndex]);
            rateButton.textContent = (text.indexOf('.') < 0 ? text + '.0' : text) + '×';
        });

        $('[data-player-close]', bar).addEventListener('click', function () {
            cancelPendingStart();
            audio.pause();
            bar.hidden = true;
            document.body.classList.remove('has-audio-bar');
            setActive(null);
        });

        audio.addEventListener('timeupdate', function () { paint(); followPlayhead(); });
        audio.addEventListener('seeked', function () { paint(); followPlayhead(); });
        audio.addEventListener('loadedmetadata', paintDuration);
        audio.addEventListener('durationchange', paintDuration);
        audio.addEventListener('emptied', paintDuration);
        ['play', 'playing', 'pause', 'ended'].forEach(function (name) { audio.addEventListener(name, syncPlaying); });
        audio.addEventListener('error', function () { if (audio.dataset.src) hint(activeBox, playFailed); });
    })();

    /* ---- Answering ------------------------------------------------------- */

    var questions = $$('.question-item');
    var cells = {};
    $$('.sheet__cell').forEach(function (cell) { cells[cell.getAttribute('href').slice(1)] = cell; });

    function pickedAnswer(question) {
        var radio = question.querySelector('.matching-input input:checked');
        if (radio) return radio.value;
        var select = question.querySelector('.cloze-select');
        return select ? select.value : '';
    }

    /* Controls follow the question's state: locked once answered, submit only with a choice made. */
    function syncControls(question) {
        var answered = question.classList.contains('answered');
        var pending = question.dataset.pending === 'true';
        $$('.options-container input, .options-container select, .mcq-btn', question).forEach(function (control) {
            control.disabled = answered || pending;
        });
        var submit = question.querySelector('.submit-btn');
        if (submit) submit.disabled = answered || pending || !pickedAnswer(question);
    }

    function clearMarks(question) {
        $$('.is-correct, .is-wrong, .is-picked', question).forEach(function (element) {
            element.classList.remove('is-correct', 'is-wrong', 'is-picked');
        });
    }

    function setVerdict(question, tone, iconName, text, answer) {
        var feedback = question.querySelector('.feedback');
        var verdict = feedback.querySelector('.verdict');
        feedback.hidden = false;
        verdict.className = 'verdict verdict--' + tone;
        verdict.textContent = '';
        if (iconName) verdict.appendChild(icon(iconName));
        if (text) verdict.appendChild(span('verdict__text', text));
        if (!answer) return;
        verdict.appendChild(span('verdict__label', '正确答案'));
        verdict.appendChild(span('verdict__answer', answer));
        // Cloze: say the word, not just its letter.
        var option = question.querySelector('.cloze-select option[value="' + answer + '"]');
        if (option && option.dataset.word) verdict.appendChild(span('verdict__word', option.dataset.word));
    }

    /* Matching: offer to show the paragraph the statement comes from. */
    function offerParagraph(question, letter) {
        var group = question.closest('.group');
        if (question.dataset.kind !== 'matching' || !group || !group.querySelector('[data-paragraph="' + letter + '"]')) return;
        var jump = document.createElement('button');
        jump.type = 'button';
        jump.className = 'verdict__jump';
        jump.dataset.showParagraph = letter;
        jump.appendChild(document.createTextNode('看 ' + letter + ' 段'));
        jump.appendChild(icon('arrow-right'));
        question.querySelector('.verdict').appendChild(jump);
    }

    function showQuestionError(question, message) {
        setVerdict(question, 'warn', 'alert', message);
    }

    /* In a word bank, a word whose blank is settled steps back. */
    function refreshWordBank(question) {
        var group = question.closest('.group');
        if (!group || question.dataset.kind !== 'cloze') return;
        var used = {};
        $$('.question-item', group).forEach(function (item) { if (item.dataset.settled) used[item.dataset.settled] = true; });
        $$('.word-bank__list li', group).forEach(function (word) {
            word.classList.toggle('is-used', Boolean(used[word.dataset.wordLabel]));
        });
    }

    function renderFeedback(question, userAnswer, data, revealed) {
        var correct = typeof data.correct_answer === 'string' ? data.correct_answer.trim().toUpperCase() : '';
        var picked = String(userAnswer || '').toUpperCase();
        var graded = !revealed && !data.unanswered;
        var right = graded && data.is_correct === true;

        clearMarks(question);
        question.classList.add('answered');
        question.classList.toggle('is-revealed', Boolean(revealed));
        question.classList.toggle('is-right', right);
        question.classList.toggle('is-miss', graded && !right);

        $$('.mcq-btn', question).forEach(function (button) {
            if (correct && button.dataset.label === correct) button.classList.add('is-correct');
            else if (graded && !right && button.dataset.label === picked) button.classList.add('is-wrong');
        });
        $$('.matching-input .box', question).forEach(function (box) {
            var value = box.querySelector('input').value;
            if (correct && value === correct) box.classList.add('is-correct');
            else if (graded && !right && value === picked) box.classList.add('is-wrong');
        });
        var select = question.querySelector('.cloze-select');
        if (select && graded) select.classList.add(right ? 'is-correct' : 'is-wrong');

        if (data.unanswered) {
            // No key for this question yet: grading it would mark a correct pick wrong.
            setVerdict(question, 'warn', 'alert', '本题暂无标准答案，未计入成绩。可在后台设置答案。');
        } else if (right) {
            setVerdict(question, 'ok', 'check', '回答正确');
        } else if (revealed) {
            if (correct) setVerdict(question, 'note', 'eye', '', correct);
            else setVerdict(question, 'warn', 'alert', '本题还没有设置标准答案。');
        } else {
            setVerdict(question, 'bad', 'x', '回答错误', correct);
        }

        var explanation = question.querySelector('.explanation-box');
        var explanationText = question.querySelector('.explanation-text');
        if (explanation && explanationText) {
            if (data.explanation && window.CETText) {
                window.CETText.render(explanationText, data.explanation, { kind: 'explanation' });
                explanation.hidden = false;
            } else {
                explanation.hidden = true;
            }
        }

        if (correct && !data.unanswered) {
            question.dataset.settled = correct;
            offerParagraph(question, correct);
        }
        refreshWordBank(question);
    }

    function resetQuestion(question) {
        question.classList.remove('answered', 'is-revealed', 'is-right', 'is-miss');
        delete question.dataset.settled;
        clearMarks(question);
        question.querySelector('.feedback').hidden = true;
        var explanation = question.querySelector('.explanation-box');
        if (explanation) explanation.hidden = true;
        $$('.matching-input input', question).forEach(function (radio) { radio.checked = false; });
        var select = question.querySelector('.cloze-select');
        if (select) select.value = '';
        syncControls(question);
        refreshWordBank(question);
    }

    async function submitAnswer(question, answer, pickedControl) {
        if (question.dataset.pending === 'true' || question.classList.contains('answered')) return;
        question.dataset.pending = 'true';
        if (pickedControl) pickedControl.classList.add('is-picked');
        syncControls(question);
        try {
            var data = await postJSON('/submit_answer', { question_id: question.dataset.qId, answer: answer });
            delete question.dataset.wasAutoShown;
            renderFeedback(question, answer, data, false);
            // A question without a key stays open: there was nothing to grade.
            if (data.unanswered) question.classList.remove('answered');
        } catch (error) {
            question.classList.remove('answered');
            showQuestionError(question, error.message);
        } finally {
            delete question.dataset.pending;
            if (pickedControl) pickedControl.classList.remove('is-picked');
            syncControls(question);
            updateProgress();
        }
    }

    /* ---- Progress and the answer sheet ----------------------------------- */

    var progress = {
        done: $('[data-progress-done]'),
        right: $('[data-count-right]'),
        miss: $('[data-count-miss]'),
        rightBar: $('[data-progress-right]'),
        missBar: $('[data-progress-miss]')
    };

    function stateOf(question) {
        if (question.classList.contains('is-right')) return 'right';
        if (question.classList.contains('is-miss')) return 'miss';
        if (question.classList.contains('is-revealed')) return 'seen';
        return 'todo';
    }

    function updateProgress() {
        var right = 0;
        var miss = 0;
        questions.forEach(function (question) {
            var state = stateOf(question);
            if (state === 'right') right += 1;
            else if (state === 'miss') miss += 1;
            var cell = cells[question.id];
            if (cell) cell.dataset.state = state;
        });
        if (!progress.done) return;
        var total = questions.length || 1;
        progress.done.textContent = String(right + miss);
        progress.right.textContent = String(right);
        progress.miss.textContent = String(miss);
        progress.rightBar.style.width = (right / total * 100).toFixed(2) + '%';
        progress.missBar.style.width = (miss / total * 100).toFixed(2) + '%';
    }

    function jumpTo(question) {
        question.scrollIntoView({ behavior: scrollBehavior(), block: 'start' });
        highlightOnce(question, 'is-flash');
        question.setAttribute('tabindex', '-1');
        question.focus({ preventScroll: true });
    }

    (function () {
        var sheet = $('#answer-sheet');
        var sheetToggle = $('[data-sheet-toggle]');
        if (!sheet || !sheetToggle) return;

        function setOpen(open) {
            sheet.hidden = !open;
            sheetToggle.setAttribute('aria-expanded', String(open));
            sheetToggle.classList.toggle('is-on', open);
        }

        sheetToggle.addEventListener('click', function () { setOpen(sheet.hidden); });
        sheet.addEventListener('click', function (event) {
            var cell = event.target.closest('.sheet__cell');
            if (!cell) return;
            event.preventDefault();
            var question = document.getElementById(cell.getAttribute('href').slice(1));
            setOpen(false);
            if (question) jumpTo(question);
        });
        document.addEventListener('click', function (event) {
            if (!sheet.hidden && !event.target.closest('#answer-sheet, [data-sheet-toggle]')) setOpen(false);
        });
        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape' && !sheet.hidden) { setOpen(false); sheetToggle.focus(); }
        });
    })();

    (function () {
        var dock = $('.dock');
        var sentinel = $('.dock-sentinel');
        if (!dock || !sentinel || !('IntersectionObserver' in window)) return;
        new IntersectionObserver(function (entries) {
            var entry = entries[0];
            dock.classList.toggle('is-stuck', !entry.isIntersecting && entry.boundingClientRect.top < 12);
        }, { rootMargin: '-12px 0px 0px 0px' }).observe(sentinel);
    })();

    /* ---- Show or hide every answer --------------------------------------- */

    var revealButton = $('#show-all-answers');
    var allAnswersShown = false;

    function setAllAnswersShown(shown) {
        allAnswersShown = shown;
        if (revealButton) {
            var label = revealButton.querySelector('[data-label]') || revealButton;
            label.textContent = shown ? '隐藏所有答案' : '显示所有答案';
            revealButton.classList.toggle('is-on', shown);
            revealButton.setAttribute('aria-pressed', String(shown));
        }
        questions.forEach(function (question) {
            if (shown) {
                // Only show if not already answered by hand.
                if (question.classList.contains('answered')) return;
                // Read-only reveal. Must not go through /submit_answer, which records an attempt.
                requestJSON('/answer/' + question.dataset.qId).then(function (data) {
                    if (!allAnswersShown || question.dataset.pending === 'true' || question.classList.contains('answered')) return;
                    renderFeedback(question, '', data, true);
                    question.dataset.wasAutoShown = 'true';
                    syncControls(question);
                    updateProgress();
                }).catch(function (error) {
                    if (allAnswersShown) showQuestionError(question, error.message);
                });
            } else if (question.dataset.wasAutoShown === 'true') {
                // Hide only what the reveal itself opened.
                resetQuestion(question);
                delete question.dataset.wasAutoShown;
            }
        });
        updateProgress();
    }

    if (revealButton) revealButton.addEventListener('click', function () { setAllAnswersShown(!allAnswersShown); });

    /* ---- Clicks inside questions ----------------------------------------- */

    document.addEventListener('click', function (event) {
        var target = event.target;

        var choice = target.closest('.mcq-btn');
        if (choice) {
            var choiceQuestion = choice.closest('.question-item');
            if (choiceQuestion && !choiceQuestion.classList.contains('answered')) {
                submitAnswer(choiceQuestion, choice.dataset.label, choice);
            }
            return;
        }

        var submit = target.closest('.submit-btn');
        if (submit) {
            var submitQuestion = submit.closest('.question-item');
            if (!submitQuestion || submitQuestion.classList.contains('answered')) return;
            var answer = pickedAnswer(submitQuestion);
            if (answer) submitAnswer(submitQuestion, answer);
            return;
        }

        var paragraphLink = target.closest('[data-show-paragraph]');
        if (paragraphLink) {
            showParagraph(paragraphLink.closest('.question-item'), paragraphLink.dataset.showParagraph);
            return;
        }

        var retry = target.closest('.retry-btn');
        if (retry) {
            var retryQuestion = retry.closest('.question-item');
            delete retryQuestion.dataset.wasAutoShown;
            resetQuestion(retryQuestion);
            updateProgress();
            return;
        }

        var remove = target.closest('.remove-btn');
        if (remove) {
            if (!confirmedTwice(remove, '确定移出？')) return;
            var removeQuestion = remove.closest('.question-item');
            remove.disabled = true;
            postJSON('/remove_wrong', { question_id: removeQuestion.dataset.qId }).then(function (data) {
                if (data.status !== 'success') return;
                removeQuestion.classList.remove('is-wrong');
                var tools = remove.closest('.q__tools');
                if (tools) tools.remove();
                var count = $('[data-wrong-count]');
                if (count) count.textContent = String(Math.max(0, (parseInt(count.textContent, 10) || 0) - 1));
            }).catch(function (error) {
                remove.disabled = false;
                showQuestionError(removeQuestion, error.message);
            });
        }
    });

    document.addEventListener('change', function (event) {
        if (!event.target.matches('.cloze-select, .matching-input input')) return;
        var question = event.target.closest('.question-item');
        if (question) syncControls(question);
    });

    // Enter on a chosen paragraph letter submits it.
    document.addEventListener('keydown', function (event) {
        if (event.key !== 'Enter' || !event.target.matches('.matching-input input')) return;
        var submit = event.target.closest('.question-item').querySelector('.submit-btn');
        if (submit && !submit.disabled) submit.click();
    });

    var retryAll = $('#retry-all-wrong');
    if (retryAll) {
        retryAll.addEventListener('click', function () {
            // Hide answers first if they are shown.
            if (allAnswersShown) setAllAnswersShown(false);
            var wrong = $$('.question-item.is-wrong');
            wrong.forEach(function (question) {
                delete question.dataset.wasAutoShown;
                resetQuestion(question);
            });
            updateProgress();
            if (wrong.length) jumpTo(wrong[0]);
        });
    }

    questions.forEach(syncControls);
    updateProgress();

    /* ---- Special practice picker ----------------------------------------- */

    $$('.special-picker').forEach(function (form) {
        var counts = JSON.parse(form.dataset.counts);

        function showStock() {
            var level = form.querySelector('[name="level"]:checked');
            var stock = counts[(level && level.value) || 'all'];
            if (!stock) return;
            $$('[data-special-type]', form).forEach(function (card) {
                var item = stock[card.dataset.specialType];
                if (!item) return;
                card.querySelector('[data-stock-groups]').textContent = item.groups;
                card.querySelector('[data-stock-questions]').textContent = item.questions;
            });
        }

        form.addEventListener('change', function (event) {
            if (event.target.name === 'level') showStock();
        });
        showStock();   // the browser may restore an earlier choice on Back

        var stepper = form.querySelector('[data-stepper]');
        if (!stepper) return;
        var input = stepper.querySelector('input[name="count"]');
        var value = stepper.querySelector('[data-stepper-value]');
        var min = Number(stepper.dataset.min);
        var max = Number(stepper.dataset.max);

        function setCount(count) {
            count = Math.min(max, Math.max(min, count));
            input.value = String(count);
            value.textContent = String(count);
            $$('[data-step]', stepper).forEach(function (button) {
                var next = count + Number(button.dataset.step);
                button.disabled = next < min || next > max;
            });
        }

        stepper.addEventListener('click', function (event) {
            var button = event.target.closest('[data-step]');
            if (button) setCount((parseInt(input.value, 10) || min) + Number(button.dataset.step));
        });
        setCount(parseInt(input.value, 10) || min);
    });

    /* ---- Home: remember which level's papers are open -------------------- */

    (function () {
        var tabs = $$('#level-tabs [data-bs-toggle="tab"]');
        if (!tabs.length || !window.bootstrap) return;
        var wanted = '';
        try { wanted = decodeURIComponent(window.location.hash); } catch (error) { wanted = ''; }
        var match = tabs.filter(function (tab) { return tab.dataset.bsTarget === wanted; })[0];
        if (match) {
            window.bootstrap.Tab.getOrCreateInstance(match).show();
            // Coming back from a paper: land on the list rather than the top of the page.
            var entry = window.performance && performance.getEntriesByType && performance.getEntriesByType('navigation')[0];
            var papers = $('#papers');
            if (papers && (!entry || entry.type === 'navigate')) papers.scrollIntoView();
        }
        tabs.forEach(function (tab) {
            tab.addEventListener('shown.bs.tab', function () {
                if (window.history.replaceState) window.history.replaceState(null, '', tab.dataset.bsTarget);
            });
        });
    })();
})();
