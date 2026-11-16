/* The recording page of EKTU proctoring: records the webcam, the microphone and
   the screen and sends the recording to the server piece by piece. */
(function() {
    'use strict';

    var cfg = JSON.parse(document.getElementById('ektu-proctoring-config').textContent);
    var T = cfg.text;
    var cam = null;
    var screenStream = null;
    var recorders = {};
    var segment = {webcam: cfg.segment, screen: cfg.segment};
    var started = false;
    var stopping = false;
    var finished = false;
    var lastContact = 0;

    function $(id) { return document.getElementById(id); }

    function show(step) {
        document.querySelectorAll('[data-step]').forEach(function(el) { el.hidden = el.dataset.step !== step; });
    }

    function say(id, text, bad) {
        var el = $(id);
        el.textContent = text || '';
        el.classList.toggle('is-bad', Boolean(bad));
        el.hidden = !text;
    }

    function post(name, body, type, query) {
        var headers = {'X-CSRFToken': cfg.csrf};
        if (type) { headers['Content-Type'] = type; }
        return fetch(cfg.urls[name] + (query || ''), {
            method: 'POST', headers: headers, body: body, credentials: 'same-origin'
        }).then(function(response) {
            return response.json().catch(function() { return {}; }).then(function(data) {
                return {ok: response.ok, status: response.status, data: data};
            });
        });
    }

    function report(kind) {
        post('event', JSON.stringify({kind: kind}), 'application/json').catch(function() {});
    }

    function supported() {
        return navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder
            && MediaRecorder.isTypeSupported('video/webm')
            && (!cfg.screen || navigator.mediaDevices.getDisplayMedia);
    }

    function mimeFor(stream) {
        var wanted = stream.getAudioTracks().length
            ? ['video/webm;codecs=vp8,opus', 'video/webm']
            : ['video/webm;codecs=vp8', 'video/webm'];
        return wanted.filter(function(mime) { return MediaRecorder.isTypeSupported(mime); })[0];
    }

    /* Sends the pieces of one recording strictly in order and keeps what the
       network could not take yet. */
    function Uploader(kind, number, mime) {
        var queue = [];
        var seq = 0;
        var busy = false;
        var failures = 0;
        var begun = performance.now();
        this.closed = false;
        this.idle = function() { return !busy && !queue.length; };
        this.push = function(blob) { queue.push(blob); pump(); };

        function retry() {
            failures += 1;
            if (failures === 3) { say('pr-network', T.upload_delayed, true); }
            setTimeout(pump, Math.min(30000, 2000 * failures));
        }

        function pump() {
            if (busy || !queue.length) { return; }
            busy = true;
            var query = '?kind=' + kind + '&segment=' + number + '&seq=' + seq
                + '&elapsed=' + Math.round(performance.now() - begun);
            post('chunk', queue[0], mime, query).then(function(result) {
                busy = false;
                if (result.ok) {
                    // The server names the piece it wants next; anything else is sent again.
                    if (result.data.next > seq) { queue.shift(); }
                    seq = result.data.next;
                    if (failures >= 3) { report('upload_delayed'); say('pr-network', ''); }
                    failures = 0;
                    lastContact = Date.now();
                    if (result.data.stop) { stop(); }
                    pump();
                    done();
                } else if (result.status === 409 || result.status === 400 || result.status === 413) {
                    // The session is closed, or the piece can never be taken.
                    queue.shift();
                    if (result.data.stop) { queue.length = 0; stop(); }
                    pump();
                    done();
                } else {
                    retry();
                }
            }).catch(function() { busy = false; retry(); });
        }
    }

    function record(kind, stream) {
        var mime = mimeFor(stream);
        var uploader = new Uploader(kind, segment[kind], mime);
        var recorder = new MediaRecorder(stream, {
            mimeType: mime, videoBitsPerSecond: cfg.bits[kind], audioBitsPerSecond: cfg.bits.audio
        });
        segment[kind] += 1;
        recorder.ondataavailable = function(event) {
            if (event.data && event.data.size) { uploader.push(event.data); }
        };
        recorder.onstop = function() { uploader.closed = true; done(); };
        recorder.start(cfg.timeslice);
        recorders[kind] = {recorder: recorder, uploader: uploader};
    }

    function halt(kind) {
        var entry = recorders[kind];
        if (entry && entry.recorder.state !== 'inactive') { entry.recorder.stop(); }
    }

    function release(stream) {
        if (stream) { stream.getTracks().forEach(function(track) { track.stop(); }); }
    }

    function watchCamera() {
        cam.getTracks().forEach(function(track) {
            track.onended = function() {
                if (stopping) { return; }
                report(track.kind === 'audio' ? 'microphone_lost' : 'camera_lost');
                halt('webcam');
                release(cam);
                cam = null;
                say('pr-camera-lost', T.camera_lost, true);
                $('pr-restore-camera').hidden = false;
            };
        });
    }

    function watchScreen() {
        screenStream.getVideoTracks()[0].onended = function() {
            if (stopping) { return; }
            report('screen_lost');
            halt('screen');
            screenStream = null;
            say('pr-screen-lost', T.screen_lost, true);
            $('pr-restore-screen').hidden = false;
        };
    }

    function openCamera() {
        return navigator.mediaDevices.getUserMedia({
            video: {width: {ideal: 320}, height: {ideal: 240}, frameRate: {ideal: 15, max: 15}},
            audio: cfg.audio ? {echoCancellation: true, noiseSuppression: true} : false
        }).then(function(stream) {
            cam = stream;
            document.querySelectorAll('.pr-preview').forEach(function(video) { video.srcObject = stream; });
            if (cfg.audio) { meter(stream); }
            return stream;
        });
    }

    function openScreen() {
        return navigator.mediaDevices.getDisplayMedia({
            video: {frameRate: {ideal: 5, max: 5}, width: {max: 1280}}, audio: false
        }).then(function(stream) {
            var surface = stream.getVideoTracks()[0].getSettings().displaySurface;
            // Firefox does not say what was shared; Chrome and Edge do.
            if (surface && surface !== 'monitor') {
                release(stream);
                throw new Error('not-entire');
            }
            screenStream = stream;
            return stream;
        });
    }

    function meter(stream) {
        var Context = window.AudioContext || window.webkitAudioContext;
        if (!Context) { return; }
        var context = new Context();
        var analyser = context.createAnalyser();
        var levels = new Uint8Array(analyser.frequencyBinCount);
        context.createMediaStreamSource(stream).connect(analyser);
        (function draw() {
            if (!cam || started) { context.close(); return; }
            analyser.getByteFrequencyData(levels);
            var peak = Math.max.apply(null, levels);
            $('pr-mic-level').style.width = Math.round(peak / 2.55) + '%';
            setTimeout(draw, 150);
        }());
    }

    function ready() {
        var ok = (!cfg.webcam || cam) && (!cfg.screen || screenStream);
        $('pr-start').disabled = !ok;
    }

    function sendPhoto() {
        var video = document.querySelector('.pr-preview');
        var canvas = document.createElement('canvas');
        canvas.width = video.videoWidth || 320;
        canvas.height = video.videoHeight || 240;
        canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
        canvas.toBlob(function(blob) {
            if (blob) { post('photo', blob, 'image/jpeg').catch(function() {}); }
        }, 'image/jpeg', 0.8);
    }

    function begin() {
        $('pr-start').disabled = true;
        say('pr-start-note', '');
        post('start', '{}', 'application/json').then(function(result) {
            if (!result.ok) {
                say('pr-start-note', result.status === 507 ? T.no_space : T.start_failed, true);
                $('pr-start').disabled = false;
                return;
            }
            started = true;
            lastContact = Date.now();
            if (cfg.webcam) { record('webcam', cam); watchCamera(); sendPhoto(); }
            if (cfg.screen) { record('screen', screenStream); watchScreen(); }
            if (window.screen && window.screen.isExtended) {
                report('extra_display');
                say('pr-display', T.extra_display, true);
            }
            show('recording');
            setInterval(beat, 20000);
        }).catch(function() {
            say('pr-start-note', T.start_failed, true);
            $('pr-start').disabled = false;
        });
    }

    /* Pieces of the recording already tell the server the page is alive; this
       speaks up only when none has gone out for a while. */
    function beat() {
        if (finished || Date.now() - lastContact < 20000) { return; }
        post('heartbeat').then(function(result) {
            lastContact = Date.now();
            if (result.ok && result.data.stop && started) { stop(); }
        }).catch(function() {});
    }

    function stop() {
        if (stopping) { return; }
        stopping = true;
        say('pr-saving', T.saving);
        $('pr-stop').disabled = true;
        halt('webcam');
        halt('screen');
        release(cam);
        release(screenStream);
        done();
    }

    function done() {
        if (!stopping || finished) { return; }
        var pending = Object.keys(recorders).some(function(kind) {
            var entry = recorders[kind];
            return entry.recorder.state !== 'inactive' || !entry.uploader.closed || !entry.uploader.idle();
        });
        if (pending) { return; }
        finished = true;
        post('finish').catch(function() {}).then(function() { show('done'); });
    }

    function init() {
        if (cfg.status === 'finished') { show('done'); return; }
        if (!supported()) { show('unsupported'); return; }

        $('pr-agree').addEventListener('change', function() { $('pr-continue').disabled = !this.checked; });
        $('pr-continue').addEventListener('click', function() { show('check'); });

        $('pr-camera').addEventListener('click', function() {
            openCamera().then(function() { say('pr-camera-note', T.camera_ok); ready(); })
                .catch(function() { say('pr-camera-note', T.camera_failed, true); });
        });
        $('pr-screen').addEventListener('click', function() {
            openScreen().then(function() { say('pr-screen-note', T.screen_ok); ready(); })
                .catch(function(error) {
                    say('pr-screen-note', error.message === 'not-entire' ? T.screen_not_entire : T.screen_failed, true);
                });
        });
        $('pr-start').addEventListener('click', begin);

        $('pr-restore-camera').addEventListener('click', function() {
            openCamera().then(function(stream) {
                record('webcam', stream);
                watchCamera();
                report('camera_restored');
                say('pr-camera-lost', '');
                $('pr-restore-camera').hidden = true;
            }).catch(function() {});
        });
        $('pr-restore-screen').addEventListener('click', function() {
            openScreen().then(function(stream) {
                record('screen', stream);
                watchScreen();
                report('screen_restored');
                say('pr-screen-lost', '');
                $('pr-restore-screen').hidden = true;
            }).catch(function(error) {
                if (error.message === 'not-entire') { say('pr-screen-lost', T.screen_not_entire, true); }
            });
        });
        $('pr-stop').addEventListener('click', function() {
            if (window.confirm(T.confirm_stop)) { stop(); }
        });
        window.addEventListener('beforeunload', function(event) {
            if (started && !finished) { event.preventDefault(); event.returnValue = T.leave; }
        });

        if (!cfg.webcam) { $('pr-camera-box').hidden = true; }
        if (!cfg.screen) { $('pr-screen-box').hidden = true; }
        // Opened again in the middle of a session: the devices have to be turned on once more.
        if (cfg.status === 'created') {
            show('consent');
        } else {
            $('pr-interrupted').hidden = false;
            show('check');
        }
    }

    init();
}());
